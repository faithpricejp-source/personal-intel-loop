"""推荐关注(契约第 6.3 节): 被动发现, 零额外抓取。

候选 = 最近 N 天已抓内容里「被关注者转发/引用/点赞但用户没关注」的账号, 来自
items.source_payload_json 的 mentioned_accounts / endorsed_by 字段(元素为账号 id 字符串,
或 {"id","name","url"} 形状); 排除已出现在用户关注流 source 里的账号(已有
weibo_home:<id> / zhihu_moments:<id> 来源)。每周一出版时 suggest() 交模型挑 ≤5 个写理由。
"""
from __future__ import annotations

import json
import sqlite3
import re
from datetime import datetime, timedelta, timezone

from personal_intel_loop.paper_common import adapter_of
from personal_intel_loop.schemas import normalize_dt_to_utc_z

FOLLOW_MAX = 5
EVIDENCE_MAX = 3
# 平台 → 用户关注流 source 前缀(出现即视为已关注)
FOLLOW_HOME_PREFIX = {"weibo": "weibo_home", "zhihu": "zhihu_moments"}
# 候选没带主页链接时按平台补(10-05: 知乎候选 url 全空, 前端「去关注」退成 href="#" 跳回报纸本身)
PROFILE_URL = {"zhihu": "https://www.zhihu.com/people/{id}", "weibo": "https://weibo.com/u/{id}"}
_PROFILE_ID_OK = {"zhihu": re.compile(r"^[A-Za-z0-9_-]+$"), "weibo": re.compile(r"^\d+$")}
# 知乎关注动态的 action_text(「某某赞同了回答」)混在 endorsed_by 里, 不是人名
_ZHIHU_ACTION = re.compile(r"(赞同了|发布了|关注了|收藏了|回答了|喜欢了|点赞了|提出了|写了|参与了)")

FOLLOW_PROMPT = """你在为一份个人信息日报挑选「推荐关注」的社交账号。输出严格的 JSON(不要 markdown 代码块, 不要解释)。

## 读者画像
{profile}

## 候选账号(用户还没关注; count=最近 7 天在他关注流里出现该账号的条数)
{candidates}

## 要求
- 只从上面候选里挑, 最多 {max_n} 个; 没有值得推荐的就给空数组。
- reason 必须引用至少一条具体证据(候选 evidence 里的标题原文), 说清为什么值得用户关注。
- 不要推荐营销号/标题党; 优先信息增量大的源。

## 输出
只返回一个 JSON 对象:
{{"picks": [{{"platform": "weibo", "account_id": "...", "reason": "..."}}]}}
"""


def _account_entries(payload: dict, key: str) -> list[dict]:
    """mentioned_accounts / endorsed_by 的元素归一: 字符串 → {"id": s}; dict → 取 id/name/url/platform。"""
    raw = payload.get(key)
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            out.append({"id": entry.strip(), "name": entry.strip(), "url": None, "platform": None})
        elif isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"].strip():
            name = entry.get("name") or entry.get("label") or entry["id"]
            platform = entry.get("platform") if isinstance(entry.get("platform"), str) and entry["platform"].strip() else None
            out.append({"id": entry["id"].strip(), "name": str(name), "url": entry.get("url") if isinstance(entry.get("url"), str) else None, "platform": platform})
    return out


def _platform_of(source: str) -> str:
    adapter = adapter_of(source)
    if adapter.startswith("weibo"):
        return "weibo"
    if adapter.startswith("zhihu"):
        return "zhihu"
    return adapter


def _profile_url(platform: str, account_id: str, url: str | None) -> str | None:
    if url:
        return url
    template, pattern = PROFILE_URL.get(platform), _PROFILE_ID_OK.get(platform)
    if template and pattern and pattern.match(account_id):
        return template.format(id=account_id)
    return None


def _zhihu_followed_names(conn: sqlite3.Connection) -> set[str]:
    """知乎关注动态里做动作的人(endorsed_by 里的人名)就是用户已关注的人。

    知乎 items.source 是 zhihu_moments:<作者 url_token>, 作者是被动作的对象, 不一定已关注;
    所以知乎不能像微博那样拿 source 后缀当已关注集合(否则推荐关注全是已关注的人)。
    """
    names: set[str] = set()
    for row in conn.execute("SELECT source_payload_json FROM items WHERE source LIKE 'zhihu_moments:%'"):
        try:
            payload = json.loads(row["source_payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        for entry in payload.get("endorsed_by") or []:
            if isinstance(entry, str) and entry.strip() and not _ZHIHU_ACTION.search(entry):
                names.add(entry.strip())
    return names


def _already_followed_ids(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """微博关注流里已有的账号: items.source 形如 weibo_home:<id>(知乎见 _zhihu_followed_names)。

    存 (platform, id)。纯数字微博 uid 与知乎 url_token 同形时不能跨平台误杀。
    """
    followed: set[tuple[str, str]] = set()
    for prefix in (FOLLOW_HOME_PREFIX["weibo"],):
        for row in conn.execute("SELECT DISTINCT source FROM items WHERE source LIKE ?", (f"{prefix}:%",)):
            followed.add(("weibo", str(row["source"]).split(":", 1)[1]))  # fix-1007-M-1
    return followed


def collect_candidates(conn: sqlite3.Connection, *, since_days: int = 7, now_utc: str | None = None) -> list[dict]:
    """最近 since_days 天入库内容里的被提及/被背书账号, 排除已关注; count 降序。"""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    cutoff = normalize_dt_to_utc_z(datetime.fromisoformat(str(now_utc).replace("Z", "+00:00")) - timedelta(days=since_days))
    followed = _already_followed_ids(conn)
    zhihu_followed = _zhihu_followed_names(conn)
    candidates: dict[tuple[str, str], dict] = {}
    for row in conn.execute(
        "SELECT item_id, source, title, url, source_payload_json FROM items WHERE first_ingested_at >= ? ORDER BY ts DESC",
        (cutoff,),
    ):
        try:
            payload = json.loads(row["source_payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(row["source"]).startswith("zhihu_moments:"):
            # 知乎关注动态: endorsed_by 是已关注的人, 候选是被他们赞同/转发的作者
            author_key = str(payload.get("author_key") or "").strip()
            author_name = str(payload.get("author_name") or "").strip()
            if not author_key or not author_name or author_name in zhihu_followed:
                continue
            entries = [{"id": author_key, "name": author_name, "url": None, "platform": "zhihu"}]
        else:
            entries = _account_entries(payload, "mentioned_accounts") + _account_entries(payload, "endorsed_by")
        if not entries:
            continue
        for entry in entries:
            # 账号自带 platform 时以它为准(转发/引用源可能是别的 adapter), 否则按本条来源的 adapter 推
            platform = entry["platform"] or _platform_of(row["source"])
            account_id = entry["id"]
            key = (platform, account_id)
            if key not in candidates and key in followed:  # fix-1007-M-1
                continue  # 已出现在用户关注流 source 里
            candidate = candidates.setdefault(
                key,
                {"platform": platform, "account_id": account_id, "label": entry["name"], "url": _profile_url(platform, account_id, entry["url"]), "count": 0, "evidence": []},
            )
            candidate["count"] += 1
            if len(candidate["evidence"]) < EVIDENCE_MAX:
                candidate["evidence"].append({"title": row["title"] or "", "url": row["url"]})
    return sorted(candidates.values(), key=lambda c: (-c["count"], c["platform"], c["account_id"]))


def refresh_statuses(conn: sqlite3.Connection, *, now_utc: str | None = None) -> int:
    """状态自动更新: new 的候选账号之后出现在用户关注流 source 里 → followed。"""
    followed = _already_followed_ids(conn)
    zhihu_followed = _zhihu_followed_names(conn)
    updated = 0
    now_z = normalize_dt_to_utc_z(now_utc or datetime.now(timezone.utc))
    for row in conn.execute("SELECT id, platform, account_id, label FROM follow_suggestions WHERE status='new'"):
        if (row["platform"] == "weibo" and ("weibo", row["account_id"]) in followed) or (  # fix-1007-M-1
            row["platform"] == "zhihu" and row["label"] in zhihu_followed
        ):
            conn.execute(
                "UPDATE follow_suggestions SET status='followed', updated_at=? WHERE id=?",
                (now_z, row["id"]),
            )
            updated += 1
    conn.commit()
    return updated


def _call_cloud(prompt: str):
    from personal_intel_loop.paper_ai import _call_cloud as paper_ai_cloud

    return paper_ai_cloud(prompt)


def _extract_json(text):
    from personal_intel_loop.summarizer import _extract_json as summarizer_extract_json

    parsed = summarizer_extract_json(text or "")
    return parsed if isinstance(parsed, dict) else None


def suggest(
    conn: sqlite3.Connection,
    *,
    llm_call=None,
    profile_path=None,
    max_n: int = FOLLOW_MAX,
    now_utc: str | None = None,
) -> dict:
    """每周一出版时跑: 收集候选 → 模型挑选写理由 → 写 follow_suggestions(status=new)。

    已在表里的账号(任何状态)不再重复推荐; dismissed 不复活。模型输出解析失败返回
    {"error": ...} 不抛。返回 {"suggested": n, "candidates": m}。
    """
    from personal_intel_loop.profile import profile_body_for_prompt, resolve_profile_path

    refresh_statuses(conn, now_utc=now_utc)
    candidates = collect_candidates(conn, now_utc=now_utc)
    if not candidates:
        return {"suggested": 0, "candidates": 0}
    known = {row["id"] for row in conn.execute("SELECT id FROM follow_suggestions")}
    candidates = [c for c in candidates if f"{c['platform']}:{c['account_id']}" not in known and c["account_id"] not in known]
    if not candidates:
        return {"suggested": 0, "candidates": 0}

    path = profile_path if profile_path is not None else resolve_profile_path()
    profile_text = profile_body_for_prompt(path)
    lines = []
    for index, candidate in enumerate(candidates):
        evidence = "; ".join(f"「{e['title']}」({e['url']})" for e in candidate["evidence"] if e["title"]) or "无"
        lines.append(
            f"{index + 1}. platform={candidate['platform']} account_id={candidate['account_id']}"
            f" label={candidate['label']} count={candidate['count']} 证据: {evidence}"
        )
    prompt = FOLLOW_PROMPT.format(
        profile=profile_text.strip() or "(无画像)",
        candidates="\n".join(lines),
        max_n=max_n,
    )
    try:
        if llm_call is not None:
            raw = llm_call(prompt)
            model = None
        else:
            raw, model = _call_cloud(prompt)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"llm failed: {exc}"}
    parsed = _extract_json(raw)
    if parsed is None:
        return {"error": f"invalid json: {(raw or '')[:200]}"}

    by_key = {(c["platform"], c["account_id"]): c for c in candidates}
    by_id = {c["account_id"]: c for c in candidates}
    now_z = normalize_dt_to_utc_z(now_utc or datetime.now(timezone.utc))
    created = 0
    picks = parsed.get("picks")
    if not isinstance(picks, list):
        picks = []
    for pick in picks:
        if created >= max_n:
            break
        if not isinstance(pick, dict):
            continue
        reason = str(pick.get("reason") or "").strip()
        if not reason:
            continue  # 理由必须引用证据, 空理由直接弃
        candidate = by_key.get((pick.get("platform"), pick.get("account_id"))) or by_id.get(pick.get("account_id"))
        if candidate is None:
            continue  # 只能从候选里挑
        suggestion_id = f"{candidate['platform']}:{candidate['account_id']}"
        cursor = conn.execute(
            "INSERT OR IGNORE INTO follow_suggestions (id, platform, account_id, label, url, reason, evidence_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)",
            (
                suggestion_id,
                candidate["platform"],
                candidate["account_id"],
                candidate["label"],
                candidate["url"],
                reason[:500],
                json.dumps(candidate["evidence"], ensure_ascii=False),
                now_z,
                now_z,
            ),
        )
        if cursor.rowcount > 0:  # 10-05 审计 F10: IGNORE 掉的重复 pick 不得计入 suggested
            created += 1
    conn.commit()
    return {"suggested": created, "candidates": len(candidates)}
