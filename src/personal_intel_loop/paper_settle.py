"""结算栏(契约第 10.2 节): 到期未结 claims 的 AI 初判 + 作者对错统计。

取到期未结的 claims(≤5 条/期), 对每条交模型做初判: 注入 search_fn 取库内相关新条目
作证据, 输出 {"verdict":"likely_true|likely_false|unclear","basis","links":[...]}, 存
claim_prechecks(同一 claim 只初判一次, 幂等)。模型/search 失败记 verdict='unclear',
不抛。settle 分区输出断言 + 初判 + 作者对错统计(不自动改 author_trust 分数)。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from personal_intel_loop.paper_common import author_key_from_payload, item_ai_payload, source_label_of
from personal_intel_loop.schemas import normalize_dt_to_utc_z
from personal_intel_loop.deepseek_peak import deepseek_peak_until, offpeak_gate_enabled, wait_for_offpeak  # noqa: F401 (测试与 CLI 从这里取)
from personal_intel_loop.store import list_open_claims, segment_for_fts

SETTLE_MAX = 5
# 证据检索窗口(以断言到期日 check_after 为锚; 契约 10.2)。
# 时间轴用条目发布时间 items.ts, 不用 first_ingested_at——库从 2026-04 才开始入库, 但 ts 回溯到 2010,
# 按入库时间锚定 2024 年到期的断言永远查不到当年证据。
# 窗口 = [到期日 - EVIDENCE_BEFORE_DAYS, min(max(到期日, 出处发布日) + EVIDENCE_AFTER_DAYS, 现在)]:
#   - 前 14 天: 事情提前发生/提前公布的报道。
#   - 后 60 天: 结论性证据多在到期之后发布(月度统计滞后 2-4 周, 季报/央行纪要 4-8 周)。
#     年报类(到期后 3-4 个月才出)仍会落空, 模型会给 unclear, 这是已知盲区。
#   - 上界取到期日与出处发布日中较晚者: 真库 80 条已到期有日期断言里 40 条到期日早于出处文章发布日,
#     一部分是回顾性事实(「6 月 23 日初选全胜」写在 9 月的文章里), 一部分是抽取把年份写早一年
#     (2026-09-29 的尼康新品稿, check_after 成了 2025-10-23)。只锚到期日会把后者全打到一年前的无关条目上。
#   - 断言自己的出处条目不算证据(否则回顾性断言会被自己的原文「证实」)。
#   取值依据: 对真实积压只读对比过多组候选窗口。
# 无到期日(check_after 为空)的断言以出处发布日为锚:
#   [出处发布日 - UNDATED_BEFORE_DAYS, min(出处发布日 + UNDATED_AFTER_DAYS, 现在)]。
#   这类断言多是「现状描述」或不带期限的判断, 能核它的是出处前后不久的报道。
# 出处条目不在库/没有发布时间时才退回旧口径: 现在往前 SEARCH_DAYS 天。
SEARCH_DAYS = 30
EVIDENCE_BEFORE_DAYS = 14
EVIDENCE_AFTER_DAYS = 60
UNDATED_BEFORE_DAYS = 14
UNDATED_AFTER_DAYS = 60
# 出版时结算栏名额只给「近期到期」的: check_after 落在 [出版日 - RECENT_DUE_DAYS, 出版日]，新到期的排前。
# 真库近 30 天约 1.2 条/天到期, 7 天窗口约 8 条, 够填每期 5 个名额; 更早的与无到期日的都算积压,
# 不占版面, 走 `pil settle-backlog` 批量初判。
RECENT_DUE_DAYS = 7

SETTLE_PROMPT = """你在为一份个人信息日报的「结算栏」给一条到期的可检验断言做初判。输出严格的 JSON(不要 markdown 代码块, 不要解释)。

## 断言
- 原文: {claim}
- 谁说的: {author}
- 当时出处: {source_url}
- 核验起点: {check_after}

## 库内相关新条目(证据)
{evidence}

## 要求
- verdict: likely_true / likely_false / unclear。
- basis: 一两句中文, 说清依据了哪条证据; 证据不足以支持判断就给 unclear 并明说「没查到足够证据」, 不得编。
- links: 依据的条目链接数组([{{"title", "url"}}]), 取自上面证据; 没有就 []。

## 输出
只返回一个 JSON 对象:
{{"verdict": "unclear", "basis": "...", "links": []}}
"""

# 二元组里的虚词。单字和这些词几乎每篇都有，拿来 OR 会把无关条目拉进来。
_STOP_TERMS = frozenset({
    "我们", "他们", "你们", "自己", "这个", "那个", "这些", "那些", "一个", "一些",
    "没有", "已经", "可以", "可能", "不是", "不会", "不能", "不可", "因为", "所以",
    "如果", "虽然", "但是", "然而", "并且", "而且", "以及", "或者", "对于", "关于",
    "通过", "进行", "其中", "之后", "之前", "以来", "目前", "今天", "昨日", "为了",
    "因此", "于是", "然后", "还是", "就是", "只是", "什么", "怎么", "这样", "那样",
    "一定", "一直", "正在", "将在", "将要", "表示", "认为", "指出",
})
_MAX_CONTENT_TERMS = 80
_FTS_CANDIDATES = 20


def _settle_query_terms(query: str) -> tuple[list[str], list[str]]:
    """与 items_fts 入库同一套二元分词。纯数字留给加分，不作为命中条件。"""
    content: list[str] = []
    dates: list[str] = []
    seen: set[str] = set()
    for token in segment_for_fts(query).split():
        if token in seen or len(token) < 2 or token in _STOP_TERMS:
            continue
        seen.add(token)
        if token.isdigit():
            dates.append(token)
        else:
            content.append(token)
    if len(content) > _MAX_CONTENT_TERMS:
        head = _MAX_CONTENT_TERMS // 2
        content = content[:head] + content[-(_MAX_CONTENT_TERMS - head):]
    return content, dates


def _fts_or(terms: list[str]) -> str:
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _extract_json(text):
    from personal_intel_loop.summarizer import _extract_json as summarizer_extract_json

    parsed = summarizer_extract_json(text or "")
    return parsed if isinstance(parsed, dict) else None


def _neg_ts(ts: str) -> tuple:
    """升序排序里让较新的 ts 排前(同分时)。"""
    return tuple(-ord(ch) for ch in ts)


def _parse_day(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def evidence_window(*, now_utc, anchor_date: str | None, source_date: str | None = None) -> tuple[str, str]:
    """证据检索窗口 [lo, hi](UTC Z 字符串, 比 items.ts)。有到期日以它为锚, 否则现在往前 SEARCH_DAYS 天。"""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    now_dt = datetime.fromisoformat(str(now_utc).replace("Z", "+00:00"))
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    anchor = _parse_day(anchor_date)
    source = _parse_day(source_date)
    if anchor is None:
        if source is None:
            return normalize_dt_to_utc_z(now_dt - timedelta(days=SEARCH_DAYS)), normalize_dt_to_utc_z(now_dt)
        lo = source - timedelta(days=UNDATED_BEFORE_DAYS)
        hi = min(source + timedelta(days=UNDATED_AFTER_DAYS + 1), now_dt)
        return normalize_dt_to_utc_z(lo), normalize_dt_to_utc_z(hi)
    lo = anchor - timedelta(days=EVIDENCE_BEFORE_DAYS)
    upper_anchor = max(anchor, source) if source is not None else anchor
    hi = min(upper_anchor + timedelta(days=EVIDENCE_AFTER_DAYS + 1), now_dt)  # +1: 含第 60 天整天
    return normalize_dt_to_utc_z(lo), normalize_dt_to_utc_z(hi)


def default_search_fn(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 5,
    now_utc: str | None = None,
    anchor_date: str | None = None,
    source_date: str | None = None,
    exclude_item_id: str | None = None,
) -> list[dict]:
    """库内相关条目, 窗口见 evidence_window(以到期日为锚)。走 items_fts 的中文二元分词；日期/纯数字只加分。"""
    if limit <= 0:
        return []
    lo, hi = evidence_window(now_utc=now_utc, anchor_date=anchor_date, source_date=source_date)
    content_terms, date_terms = _settle_query_terms(query)
    if not content_terms:
        return []
    # fix-1007-S-1: 建表 tokenizer 是 unicode61，本身不切中文；入库 segment_for_fts 已切成二元组。
    # 整句 LIKE / 全词 AND 对不上证据原文。内容词 OR 才入选，日期纯数字不能单独命中。
    match = _fts_or(content_terms)
    rows = conn.execute(
        """
        SELECT i.item_id, i.title, i.url, i.source, i.ts, i.body, bm25(items_fts) AS score
        FROM items i
        JOIN items_fts ON items_fts.item_id = i.item_id
        WHERE i.ts >= ? AND i.ts <= ? AND items_fts MATCH ?
        ORDER BY bm25(items_fts), i.ts DESC
        LIMIT ?
        """,
        (lo, hi, match, limit * _FTS_CANDIDATES),
    ).fetchall()
    # 验收方改: 排序用 bm25(按文档长度归一), 不用命中二元组个数——后者偏向长转写稿,
    # 真库 84 条到期断言里同一期财新转写被 17 条断言选中。日期命中只按比例加分(bm25 越负越相关)。
    ranked: list[tuple[float, str, sqlite3.Row]] = []
    date_set = set(date_terms)
    for row in rows:
        if exclude_item_id is not None and row["item_id"] == exclude_item_id:
            continue
        haystack = set(segment_for_fts(row["title"]).split()) | set(segment_for_fts(row["body"]).split())
        content_hits = sum(1 for term in content_terms if term in haystack)
        if content_hits <= 0:
            continue
        date_hits = sum(1 for term in date_set if term in haystack)
        ranked.append((float(row["score"]) * (1 + 0.1 * date_hits), row["ts"] or "", row))
    ranked.sort(key=lambda item: (item[0], _neg_ts(item[1])))
    return [
        {"item_id": row["item_id"], "title": row["title"], "url": row["url"], "source": row["source"], "date": row["ts"]}
        for _score, _ts, row in ranked[:limit]
    ]


def _call_cloud(prompt: str):
    from personal_intel_loop.paper_ai import _call_cloud as paper_ai_cloud

    return paper_ai_cloud(prompt)


def _author_label(conn: sqlite3.Connection, item_id: str) -> str:
    payload = item_ai_payload(conn, item_id) or {}
    item_row = conn.execute("SELECT source, source_payload_json FROM items WHERE item_id=?", (item_id,)).fetchone()
    if item_row is None:
        return "(条目已不在库)"
    try:
        source_payload = json.loads(item_row["source_payload_json"] or "{}")
    except json.JSONDecodeError:
        source_payload = {}
    return author_key_from_payload(payload, source_label_of(item_row["source"], source_payload if isinstance(source_payload, dict) else None))


def author_record(conn: sqlite3.Connection, author_label: str) -> dict:
    """同作者已结算断言的对错数(claims.outcome 统计, 不改信任分)。"""
    counts = {"n_true": 0, "n_false": 0, "n_unresolvable": 0}
    for row in conn.execute(
        "SELECT c.outcome, c.item_id FROM claims c WHERE c.outcome IS NOT NULL AND c.item_id IS NOT NULL"
    ):
        if _author_label(conn, row["item_id"]) == author_label:
            if row["outcome"] == "true":
                counts["n_true"] += 1
            elif row["outcome"] == "false":
                counts["n_false"] += 1
            else:
                counts["n_unresolvable"] += 1
    counts["label"] = author_label
    return counts


def _search_evidence(conn, claim_row, *, item_row, now_utc, search_fn) -> list[dict]:
    if search_fn is not None:
        return list(search_fn(claim_row["claim"]))
    return default_search_fn(
        conn,
        claim_row["claim"],
        now_utc=now_utc,
        anchor_date=claim_row["check_after"],
        source_date=item_row["ts"] if item_row is not None else None,
        exclude_item_id=claim_row["item_id"],
    )


def precheck_claim(
    conn: sqlite3.Connection,
    claim_row: sqlite3.Row,
    *,
    now_utc: str,
    llm_call=None,
    search_fn=None,
) -> dict:
    """对一条断言做初判并写 claim_prechecks。返回 {"verdict","basis","links","model"}。"""
    item_row = conn.execute("SELECT url, ts FROM items WHERE item_id=?", (claim_row["item_id"],)).fetchone()
    evidence = _search_evidence(conn, claim_row, item_row=item_row, now_utc=now_utc, search_fn=search_fn)
    evidence_lines = [
        f"- {entry.get('title') or '(无标题)'} ({entry.get('date') or '?'}): {entry.get('url') or '无链接'}"
        for entry in evidence
    ]
    prompt = SETTLE_PROMPT.format(
        claim=claim_row["claim"],
        author=_author_label(conn, claim_row["item_id"]),
        source_url=(item_row["url"] if item_row else None) or "无",
        check_after=claim_row["check_after"] or "未定",
        evidence="\n".join(evidence_lines) if evidence_lines else "(无)",
    )
    model = None
    verdict, basis, links = "unclear", "", []
    try:
        if llm_call is not None:
            raw = llm_call(prompt)
        else:
            raw, model = _call_cloud(prompt)
    except Exception as exc:  # noqa: BLE001 — 初判失败不挡出版
        basis = f"llm failed: {exc}"
    else:
        parsed = _extract_json(raw)
        if parsed is None:
            basis = f"invalid json: {(raw or '')[:200]}"
        else:
            candidate = parsed.get("verdict")
            if candidate in ("likely_true", "likely_false", "unclear"):
                verdict = candidate
            basis = str(parsed.get("basis") or "").strip()[:500]
            raw_links = parsed.get("links")
            if isinstance(raw_links, list):
                for link in raw_links:
                    if isinstance(link, dict) and (link.get("url") or link.get("title")):
                        links.append({"title": str(link.get("title") or "")[:200], "url": str(link.get("url") or "")[:500] or None})
            if not isinstance(basis, str) or not basis:
                basis = "(模型未给依据)"
    conn.execute(
        "INSERT OR REPLACE INTO claim_prechecks (claim_id, verdict, basis, links_json, model, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (claim_row["claim_id"], verdict, basis, json.dumps(links, ensure_ascii=False), model, normalize_dt_to_utc_z(now_utc)),
    )
    conn.commit()
    return {"verdict": verdict, "basis": basis, "links": links, "model": model}


def _precheck_failed(basis: str | None) -> bool:
    """失败或没给依据的初判不算数，下次出版重做（10-05 审计 ST-1；10-04 回归留下 5 条「模型未给依据」）。"""
    text = (basis or "").strip()
    return not text or text.startswith(("llm failed:", "invalid json:")) or text == "(模型未给依据)"


def run_prechecks(
    conn: sqlite3.Connection,
    *,
    date_local,
    now_utc: str,
    llm_call=None,
    search_fn=None,
    max_n: int = SETTLE_MAX,
) -> list[dict]:
    """近期到期(RECENT_DUE_DAYS 内)的未结断言逐条初判(已有 precheck 的跳过)。返回 [{"claim_row","precheck"}]。"""
    # fix-1007-S-2: 先丢掉所引条目已不在库的断言，再按 max_n 截断。先截断会让孤儿占满结算栏名额。
    cap = max(0, max_n)
    due = []
    for claim_row in recent_due_claims(conn, date_local=date_local):
        if len(due) >= cap:
            break
        if conn.execute("SELECT 1 FROM items WHERE item_id=?", (claim_row["item_id"],)).fetchone() is None:
            continue
        due.append(claim_row)
    out = []
    for claim_row in due:
        existing = conn.execute(
            "SELECT verdict, basis, links_json, model FROM claim_prechecks WHERE claim_id=?",
            (claim_row["claim_id"],),
        ).fetchone()
        if existing is not None and not _precheck_failed(existing["basis"]):
            try:
                links = json.loads(existing["links_json"] or "[]")
            except json.JSONDecodeError:
                links = []
            precheck = {"verdict": existing["verdict"], "basis": existing["basis"], "links": links, "model": existing["model"]}
        else:
            precheck = precheck_claim(conn, claim_row, now_utc=now_utc, llm_call=llm_call, search_fn=search_fn)
        if conn.execute("SELECT 1 FROM items WHERE item_id=?", (claim_row["item_id"],)).fetchone() is not None:
            out.append({"claim_row": claim_row, "precheck": precheck})
    return out


def recent_due_claims(conn: sqlite3.Connection, *, date_local) -> list[sqlite3.Row]:
    """出版名额候选: check_after 在 [出版日 - RECENT_DUE_DAYS, 出版日] 的未结断言, 新到期的在前。"""
    lo = (date_local - timedelta(days=RECENT_DUE_DAYS)).isoformat()
    rows = [
        row for row in list_open_claims(conn, due_before=date_local.isoformat())
        if row["check_after"] and str(row["check_after"])[:10] >= lo
    ]
    rows.sort(key=lambda row: str(row["check_after"])[:10], reverse=True)  # 稳定排序: 同日保留 extracted_at 序
    return rows


def backlog_claims(conn: sqlite3.Connection, *, date_local) -> list[sqlite3.Row]:
    """结算积压: 已到期但不在近期窗口(含无到期日)、所引条目仍在库、还没有有效初判的未结断言。"""
    lo = (date_local - timedelta(days=RECENT_DUE_DAYS)).isoformat()
    out = []
    for row in list_open_claims(conn, due_before=date_local.isoformat()):
        if row["check_after"] and str(row["check_after"])[:10] >= lo:
            continue  # 近期到期的归出版
        if conn.execute("SELECT 1 FROM items WHERE item_id=?", (row["item_id"],)).fetchone() is None:
            continue
        existing = conn.execute("SELECT basis FROM claim_prechecks WHERE claim_id=?", (row["claim_id"],)).fetchone()
        if existing is not None and not _precheck_failed(existing["basis"]):
            continue
        out.append(row)
    return out


def rejudge_claims(conn: sqlite3.Connection, *, date_local, before: str) -> list[sqlite3.Row]:
    """重判候选: 已到期(含无到期日)未结、所引条目仍在库、初判 created_at 早于 before(YYYY-MM-DD 或 UTC Z)的断言。
    用于 10-07 以前按「现在往前 30 天」旧窗口做的初判。重判后 created_at 刷新, 再跑即不再入选(可续跑)。"""
    out = []
    for row in list_open_claims(conn, due_before=date_local.isoformat()):
        existing = conn.execute("SELECT created_at FROM claim_prechecks WHERE claim_id=?", (row["claim_id"],)).fetchone()
        if existing is None or str(existing["created_at"] or "") >= str(before):
            continue
        if conn.execute("SELECT 1 FROM items WHERE item_id=?", (row["item_id"],)).fetchone() is None:
            continue
        out.append(row)
    return out


def settle_backlog(
    conn: sqlite3.Connection,
    *,
    date_local,
    now_utc: str,
    limit: int = 20,
    dry_run: bool = True,
    llm_call=None,
    search_fn=None,
    rejudge_before: str | None = None,
    clock=None,
    sleep=None,
) -> dict:
    """批量初判结算积压(rejudge_before 给定时改为重判旧初判)。dry_run(缺省)只检索证据报数: 不调模型、不写库;
    否则逐条先过 DeepSeek 错峰闸门(高峰就睡到空闲)再 precheck_claim, 每条初判立即 commit, 中断后重跑从未完成的继续。"""
    if rejudge_before:
        backlog = rejudge_claims(conn, date_local=date_local, before=rejudge_before)
    else:
        backlog = backlog_claims(conn, date_local=date_local)
    picked = backlog[: max(0, limit)]
    rows = []
    for claim_row in picked:
        entry = {
            "claim_id": claim_row["claim_id"],
            "item_id": claim_row["item_id"],
            "check_after": claim_row["check_after"],
            "claim": claim_row["claim"],
        }
        if dry_run:
            item_row = conn.execute("SELECT url, ts FROM items WHERE item_id=?", (claim_row["item_id"],)).fetchone()
            evidence = _search_evidence(conn, claim_row, item_row=item_row, now_utc=now_utc, search_fn=search_fn)
            entry["evidence_n"] = len(evidence)
        else:
            if offpeak_gate_enabled():
                wait_for_offpeak(clock=clock, sleep=sleep, label="settle-backlog")
            precheck = precheck_claim(conn, claim_row, now_utc=now_utc, llm_call=llm_call, search_fn=search_fn)
            entry["verdict"] = precheck["verdict"]
            entry["basis"] = precheck["basis"]
        rows.append(entry)
    return {
        "dry_run": dry_run,
        "mode": "rejudge" if rejudge_before else "backlog",
        "backlog_total": len(backlog),
        "processed": 0 if dry_run else len(rows),
        "rows": rows,
    }


def entry_block(conn: sqlite3.Connection, item_id: str) -> dict | None:
    """settle 分区条目上的 settle 块: 断言 + 初判 + 作者对错统计。无到期断言返回 None。"""
    claim_row = conn.execute(
        "SELECT * FROM claims WHERE item_id=? AND outcome IS NULL ORDER BY check_after IS NULL, check_after LIMIT 1",
        (item_id,),
    ).fetchone()
    if claim_row is None:
        return None
    precheck_row = conn.execute(
        "SELECT verdict, basis, links_json, model FROM claim_prechecks WHERE claim_id=?",
        (claim_row["claim_id"],),
    ).fetchone()
    if precheck_row is not None:
        try:
            links = json.loads(precheck_row["links_json"] or "[]")
        except json.JSONDecodeError:
            links = []
        precheck = {"verdict": precheck_row["verdict"], "basis": precheck_row["basis"], "links": links}
    else:
        precheck = None
    author_label = _author_label(conn, item_id)
    return {
        "claim_id": claim_row["claim_id"],
        "claim": claim_row["claim"],
        "check_after": claim_row["check_after"],
        "precheck": precheck,
        "author": author_record(conn, author_label),
    }
