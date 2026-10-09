"""出版「一天一期的报纸」: 选主线、排盲区版、社交综述、新知配额、温暖/风险/机会/结算/
闲与美分区、版面预算, 写 editions + digests, 补全文与 AI 预处理。

主线走 digest 的排序器(可注入 select_fn), 盲区版用画像 blindspots.md + 嵌入相似度。
社交条目(SOCIAL_ADAPTERS)不逐帖上版: 按嵌入贪心聚类(≥0.78), ≥2 条的组交模型写综述
Item(digests 表), 综述分数 = 组内最高 + 0.05×(组大小-1) 上限 +0.2, 与普通条目同版面排序。
同一天重跑: 先删该日再写, 整体一个事务。dry-run 用 plan_edition, 不写库不抓网不调模型。
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from personal_intel_loop.paper_common import (
    NOVELTY_KINDS,
    NOVELTY_NEW_KINDS,
    NOVELTY_OLD_KINDS,
    SOCIAL_ADAPTERS,
    adapter_of,
    author_key_from_payload,
    author_score,
    first_media_url,
    item_ai_payload,
    item_frame,
    source_label_of,
)
from personal_intel_loop import LOCAL_TZ
from personal_intel_loop.schemas import normalize_dt_to_utc_z

logger = logging.getLogger(__name__)

BLIND_LOOKBACK_HOURS = 36
BLIND_MIN_BODY_CHARS = 300
BLIND_JUDGE_POOL = 40
SOCIAL_SINGLETON_MIN_CHARS = 200
BLIND_REPEAT_DAYS = 3
BLIND_PER_SOURCE_CAP = 2
TOP_SECTION_SIZE = 5
CANDIDATE_MULTIPLIER = 2
RESERVE_SHARE = 0.2  # 7.2: 入选阶段多预处理 20% 的备选(备选不上版只存 item_ai)
NEW_SHARE_MIN = 0.4  # 7.2: 主线 new_fact|new_mechanism|counter 合计 ≥40%
MONDAY = 0


def _default_select(conn: sqlite3.Connection, *, date_local: date, top_k: int, now_utc: str) -> list[dict]:
    from personal_intel_loop import digest

    candidates, _version = digest._select_ranked_with_fallback(
        conn, date_local=date_local, top_k=top_k, now_utc=now_utc, summarize=False
    )
    return candidates


def _default_embed(texts: list[str]) -> list[list[float]]:
    from personal_intel_loop import embeddings

    vectors = embeddings.embed_documents(texts)
    return [[float(x) for x in vector] for vector in vectors]


def _load_blindspots(profile_dir) -> list[str]:
    """profile_dir 显式给出时读该目录, 否则走画像接口的调用时解析(PIL_PROFILE_DIR/缺省 <PIL_HOME>/profile)。"""
    from personal_intel_loop.profile import load_blindspots

    if profile_dir is not None:
        return load_blindspots(Path(profile_dir) / "blindspots.md")
    return load_blindspots()


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def _muted_sets(conn: sqlite3.Connection) -> tuple[set[str], set[str]]:
    muted_sources = {row["source"] for row in conn.execute("SELECT source FROM source_overrides WHERE muted=1")}
    muted_authors = {row["author_key"] for row in conn.execute("SELECT author_key FROM author_trust WHERE muted=1")}
    return muted_sources, muted_authors


def _candidate_author_key(conn: sqlite3.Connection, candidate: dict) -> str:
    payload = item_ai_payload(conn, candidate["item_id"])
    source_label = source_label_of(candidate.get("source", ""), candidate.get("source_payload") or {})
    return author_key_from_payload(payload, source_label)


def _collect_main(
    conn: sqlite3.Connection,
    *,
    date_local: date,
    main_n: int,
    now_utc: str,
    select_fn: Callable,
) -> list[dict]:
    """主线候选: 排序器取 main_n*2, 去掉静音源/静音作者/前 3 天上过版的 item。列表顺序即排名。"""
    ranked = select_fn(conn, date_local=date_local, top_k=max(main_n * CANDIDATE_MULTIPLIER, 1), now_utc=now_utc)
    muted_sources, muted_authors = _muted_sets(conn)
    repeat_since = (date_local - timedelta(days=BLIND_REPEAT_DAYS)).isoformat()
    seen_recently = {
        row["item_id"]
        for row in conn.execute(
            "SELECT DISTINCT item_id FROM editions WHERE edition_date >= ? AND edition_date < ?",
            (repeat_since, date_local.isoformat()),
        )
    }
    out = []
    for candidate in ranked:
        if candidate["item_id"] in seen_recently:
            continue
        if candidate.get("source") in muted_sources:
            continue
        if _candidate_author_key(conn, candidate) in muted_authors:
            continue
        out.append(candidate)
    return out


def _knob_offsets(conn: sqlite3.Connection, kind: str) -> dict[str, float]:
    """排序用的有效偏移 = clamp(基础值+累计偏移) - 基础值，契约 4.1 的 [0.05,0.95] 在出版侧同样生效
    （10-05 审计 C3：原先直接加累计值，连续同向调整会无上限放大）。库里仍存原始累计值，撤销按 delta 精确回退。"""
    from personal_intel_loop.paper_learn import _clamp, base_value

    out = {}
    for row in conn.execute("SELECT key, offset FROM knob_offsets WHERE kind=?", (kind,)):
        base = base_value(conn, kind, row["key"])
        out[row["key"]] = _clamp(base + float(row["offset"])) - base
    return out


def _rerank(conn: sqlite3.Connection, candidates: list[dict]) -> list[tuple[dict, float]]:
    """base = 1 - i/len(list); score = base + 0.3*(author_eff-0.5) + source_offset + 0.1(源 boosted)。

    author_eff = author_score + author_offset(无记录 author_score 取 0.5)。稳定排序, 返回 (候选, 分数)。
    """
    boosted = {row["source"] for row in conn.execute("SELECT source FROM source_overrides WHERE boosted=1")}
    source_offsets = _knob_offsets(conn, "source")
    author_offsets = _knob_offsets(conn, "author")
    total = len(candidates)
    scored = []
    for index, candidate in enumerate(candidates):
        author_key = _candidate_author_key(conn, candidate)
        row = conn.execute("SELECT n_up, n_down FROM author_trust WHERE author_key=?", (author_key,)).fetchone()
        trust = author_score(row["n_up"], row["n_down"]) if row else 0.5
        base = 1.0 - index / total if total else 0.0
        score = (
            base
            + 0.3 * (trust + author_offsets.get(author_key, 0.0) - 0.5)
            + source_offsets.get(candidate.get("source"), 0.0)
            + (0.1 if candidate.get("source") in boosted else 0.0)
        )
        scored.append((score, candidate))
    scored.sort(key=lambda pair: -pair[0])
    return [(candidate, score) for score, candidate in scored]


def _select_blind(
    conn: sqlite3.Connection,
    *,
    now_utc: str,
    main_ids: set[str],
    blindspots: list[str],
    blind_n: int,
    embed_fn: Callable[[list[str]], list[list[float]]],
    date_local: date | None = None,
    blind_judge: Callable[[list[dict], list[str]], dict[str, str]] | None = None,
) -> list[tuple[dict, str]]:
    """盲区版: 最近 36 小时入库、未入选主线、未被屏蔽; 与盲区句最大余弦降序, 同源 ≤2。"""
    muted_sources, muted_authors = _muted_sets(conn)
    now_dt = datetime.fromisoformat(normalize_dt_to_utc_z(now_utc).replace("Z", "+00:00"))
    cutoff = normalize_dt_to_utc_z(now_dt - timedelta(hours=BLIND_LOOKBACK_HOURS))
    rows = conn.execute(
        "SELECT item_id, source, title, body, source_payload_json FROM items WHERE first_ingested_at >= ? ORDER BY ts DESC, rowid",
        (cutoff,),
    ).fetchall()
    # 2026-10-04 合并时补：盲区版也不重复前 3 天上过版的条目（原设计规格只对主线写了这条，属规格漏写）
    today_local = date_local or now_dt.astimezone(LOCAL_TZ).date()
    seen_recently = {
        r["item_id"]
        for r in conn.execute(
            "SELECT DISTINCT item_id FROM editions WHERE edition_date >= ? AND edition_date < ?",
            ((today_local - timedelta(days=BLIND_REPEAT_DAYS)).isoformat(), today_local.isoformat()),
        )
    }
    candidates = []
    for row in rows:
        if row["item_id"] in main_ids or row["item_id"] in seen_recently:
            continue
        # 10-04 首期 dry-run：盲区版被微博短帖占满（短文本的嵌入相似度基本是噪音）→ 只收非社交、正文 ≥300 字的条目
        if adapter_of(row["source"]) in SOCIAL_ADAPTERS or len(row["body"] or "") < BLIND_MIN_BODY_CHARS:
            continue
        if row["source"] in muted_sources:
            continue
        try:
            payload_json = json.loads(row["source_payload_json"] or "{}")
        except json.JSONDecodeError:
            payload_json = {}
        payload = payload_json if isinstance(payload_json, dict) else None
        author_key = author_key_from_payload(item_ai_payload(conn, row["item_id"]), source_label_of(row["source"], payload))
        if author_key in muted_authors:
            continue
        candidates.append(dict(row))
    if not candidates:
        return []

    candidate_vectors = embed_fn([f"{c['title'] or ''}\n{(c['body'] or '')[:1000]}" for c in candidates])
    blindspot_vectors = embed_fn(list(blindspots))
    scored = []
    for candidate, vector in zip(candidates, candidate_vectors):
        best_reason, best_sim = None, -1.0
        for sentence, sentence_vector in zip(blindspots, blindspot_vectors):
            similarity = _cosine(vector, sentence_vector)
            if similarity > best_sim:
                best_sim, best_reason = similarity, sentence
        scored.append((best_sim, candidate, best_reason))
    scored.sort(key=lambda triple: -triple[0])
    if blind_judge is not None:
        # 实测嵌入相似度给出的盲区匹配大多牵强（只是沾到关键词），
        # 先取相似度前 BLIND_JUDGE_POOL 条，交模型逐条判「真的属于哪条盲区方向」，判不上的丢掉；宁缺毋滥。
        pool = scored[:BLIND_JUDGE_POOL]
        try:
            verdict = blind_judge([c for _s, c, _r in pool], blindspots)
        except Exception:  # noqa: BLE001
            logger.exception("blind judge failed; fall back to embedding order")
            verdict = None
        if verdict is not None:
            scored = [(s, c, verdict[str(c["item_id"])]) for s, c, _r in pool if str(c["item_id"]) in verdict]

    picked: list[tuple[dict, str]] = []
    per_source: dict[str, int] = {}
    for _similarity, candidate, reason in scored:
        count = per_source.get(candidate["source"], 0)
        if count >= BLIND_PER_SOURCE_CAP:
            continue
        picked.append((candidate, reason))
        per_source[candidate["source"]] = count + 1
        if len(picked) >= blind_n:
            break
    return picked


BLIND_JUDGE_PROMPT = """下面是读者自己写的「盲区方向」（他平时不看的领域），以及一批候选条目。
不知道的东西永远无穷大，值得补的盲区是**补上之后能让读者少犯错、多得利**的那部分——盲区版本身就是一种有偏向的筛选，偏向读者的决策。
逐条判断，两个条件**都满足**才选：
1. 内容的主体确实就是某条盲区方向描述的东西（沾边、借题发挥、只出现关键词的都不算）；
2. 说得出它可能改变读者的**哪一个具体判断或决策**（如产业判断、对某地/某群体的预期、安全、行程、职业），或纠正哪一类他容易犯的错。说不出具体的判断，只是「拓宽视野」「了解一下」的，不选。
宁缺毋滥：一条都不符合就返回空对象。

## 盲区方向
{spots}

## 候选条目
{items}

只返回 JSON：{{"<条目编号>": {{"spot": "<命中的那条盲区方向原文>", "stakes": "<可能改变的那个判断，≤30 字>"}}, ...}}
"""


def default_blind_judge(candidates: list[dict], blindspots: list[str]) -> dict[str, str] | None:
    """生产用：走判定通道（DeepSeek 开思考，OpenRouter 兜底）判盲区归属。返回 item_id -> 方向；模型不可用返回 None（回落嵌入顺序）。"""
    from personal_intel_loop.paper_ai import _call_judge, _extract_json

    lines = [f"[{i}] {c.get('title') or ''}｜{(c.get('body') or '')[:300]}" for i, c in enumerate(candidates)]
    prompt = BLIND_JUDGE_PROMPT.format(spots="\n".join(f"- {s}" for s in blindspots), items="\n".join(lines))
    raw, _model = _call_judge(prompt)
    parsed = _extract_json(raw) if raw else None
    if parsed is None:
        return None
    out = {}
    for key, value in parsed.items():
        try:
            idx = int(str(key).strip("[] "))
        except ValueError:
            continue
        # 新格式 {"spot","stakes"}；旧格式直接是方向原文（兼容）。没写 stakes 的按条件 2 不满足丢掉
        if isinstance(value, dict):
            spot, stakes = value.get("spot"), str(value.get("stakes") or "").strip()
            if not stakes:
                continue
        else:
            spot, stakes = value, ""
        if 0 <= idx < len(candidates) and spot in blindspots:
            out[str(candidates[idx]["item_id"])] = f"{spot}｜可能影响：{stakes[:40]}" if stakes else spot
    return out


def _assign_sections(main: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """lead 取第 1 条; 若排序前 3 条里有带图的, 取最靠前那条带图当 lead; 其后 5 条 = top, 其余 = briefs。"""
    if not main:
        return [], [], []
    lead_index = 0
    for index in range(min(3, len(main))):
        if first_media_url(main[index].get("source_payload")):
            lead_index = index
            break
    lead = [main[lead_index]]
    rest = [candidate for index, candidate in enumerate(main) if index != lead_index]
    return lead, rest[:TOP_SECTION_SIZE], rest[TOP_SECTION_SIZE:]


def _compute_edition(
    conn: sqlite3.Connection,
    *,
    date_local: date,
    n: int,
    blind_share: float,
    now_utc: str,
    select_fn: Callable,
    embed_fn: Callable,
    blindspots: list[str],
):
    blind_n = round(n * blind_share)
    main_n = n - blind_n
    ranked = _collect_main(conn, date_local=date_local, main_n=main_n, now_utc=now_utc, select_fn=select_fn)
    scored_main = _rerank(conn, ranked)[:main_n]
    lead, top, briefs = _assign_sections([candidate for candidate, _score in scored_main])
    blind: list[tuple[dict, str]] = []
    if blindspots and blind_n > 0:
        blind = _select_blind(
            conn,
            now_utc=now_utc,
            main_ids={candidate["item_id"] for candidate, _score in scored_main},
            blindspots=blindspots,
            blind_n=blind_n,
            embed_fn=embed_fn,
            date_local=date_local,
        )
    return main_n, scored_main, lead, top, briefs, blind


def _edition_rows(date_iso: str, built_at: str, lead, top, briefs, blind) -> list[tuple]:
    rows = _main_rows(date_iso, built_at, lead, top, briefs)
    for rank, (candidate, reason) in enumerate(blind):
        rows.append((date_iso, candidate["item_id"], "blind", rank, reason, built_at))
    return rows


def _main_rows(date_iso: str, built_at: str, lead, top, briefs) -> list[tuple]:
    rows = []
    for section, entries in (("lead", lead), ("top", top), ("briefs", briefs)):
        for rank, candidate in enumerate(entries):
            rows.append((date_iso, candidate["item_id"], section, rank, None, built_at))
    return rows


def _topic_rerank(conn: sqlite3.Connection, scored_main: list[tuple[dict, float]]) -> list[tuple[dict, float]] | None:
    """topic_offset 版面重排: final = score + topic_offset(item_ai.topic)。

    入选后跑完预处理才调用; 没有任何非零偏移时返回 None(不必重写)。盲区版不在此列。
    """
    offsets = _knob_offsets(conn, "topic")
    if not offsets:
        return None
    rescored = []
    changed = False
    for candidate, score in scored_main:
        topic = (item_ai_payload(conn, candidate["item_id"]) or {}).get("topic")
        offset = offsets.get(topic, 0.0) if topic else 0.0
        if offset:
            changed = True
        rescored.append((candidate, score + offset))
    if not changed:
        return None
    rescored.sort(key=lambda pair: -pair[1])
    return rescored


# --- TASK4: 新知判定 / 配额 / 分区抽取(契约 7.2、8、9、10) ---


def _novelty_kind(conn: sqlite3.Connection, item_id: str) -> str | None:
    payload = item_ai_payload(conn, item_id) or {}
    novelty = payload.get("novelty")
    if isinstance(novelty, dict) and novelty.get("kind") in NOVELTY_KINDS:
        return novelty["kind"]
    return None


def _lane_tags_of(conn: sqlite3.Connection, item_id: str) -> list[str]:
    payload = item_ai_payload(conn, item_id) or {}
    tags = payload.get("lane_tags")
    return [str(tag) for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []


def _source_is_warm(source: str | None, warmth_sources: list[str]) -> bool:
    if not source or not warmth_sources:
        return False
    return source in warmth_sources or adapter_of(source) in warmth_sources


def _apply_novelty_quota(
    conn: sqlite3.Connection,
    sections: dict[str, list[str]],
    reserve: list[tuple[dict, float]],
    score_by_id: dict[str, float],
) -> int:
    """契约 7.2 版面配额(原地改 sections 的 id 列表)。

    1) 主线 new_fact|new_mechanism|counter 合计 <40% 时, 用备选池里已预处理、未入选的
       new_*/counter 条目替换主线里分数最低的 known/confirming。
    2) lead+top 中 known/confirming 超过 1 条 → 与 briefs 中分数最高的 new_*/counter 交换。
    返回替换+交换的总次数。
    """
    main_ids = sections["lead"] + sections["top"] + sections["briefs"]
    total = len(main_ids)
    if total == 0:
        return 0

    def _kind(item_id: str) -> str | None:
        return _novelty_kind(conn, item_id)

    new_count = sum(1 for item_id in main_ids if _kind(item_id) in NOVELTY_NEW_KINDS)
    target = math.ceil(total * NEW_SHARE_MIN)
    swaps = 0
    if new_count < target:
        victims = sorted(
            (item_id for item_id in main_ids if _kind(item_id) in NOVELTY_OLD_KINDS),
            key=lambda item_id: score_by_id.get(item_id, 0.0),
        )
        additions = [
            (candidate["item_id"], score)
            for candidate, score in reserve
            if _kind(candidate["item_id"]) in NOVELTY_NEW_KINDS
        ]
        additions.sort(key=lambda pair: -pair[1])
        used_reserves: set[str] = set()
        while new_count < target and victims and additions:
            victim = victims.pop(0)
            addition, _score = additions.pop(0)
            if addition in used_reserves:
                continue
            replaced = False
            for section in ("lead", "top", "briefs"):
                ids = sections[section]
                if victim in ids:
                    ids[ids.index(victim)] = addition
                    replaced = True
                    break
            if not replaced:
                continue
            used_reserves.add(addition)
            new_count += 1
            swaps += 1

    # lead+top 里 known/confirming 最多 1 条
    while True:
        lt_old = [item_id for item_id in sections["lead"] + sections["top"] if _kind(item_id) in NOVELTY_OLD_KINDS]
        if len(lt_old) <= 1:
            break
        briefs_new = sorted(
            (item_id for item_id in sections["briefs"] if _kind(item_id) in NOVELTY_NEW_KINDS),
            key=lambda item_id: -score_by_id.get(item_id, 0.0),
        )
        if not briefs_new:
            break
        victim = min(lt_old, key=lambda item_id: score_by_id.get(item_id, 0.0))
        addition = briefs_new[0]
        for section in ("lead", "top"):
            ids = sections[section]
            if victim in ids:
                ids[ids.index(victim)] = addition
                break
        sections["briefs"][sections["briefs"].index(addition)] = victim
        swaps += 1
    return swaps


def _extract_lane_section(
    conn: sqlite3.Connection,
    sections: dict[str, list[str]],
    *,
    score_by_id: dict[str, float],
    cap: int | None,
    predicate: Callable[[str], bool],
) -> list[str]:
    """把 lead/top/briefs 里命中谓词的条目移出, 单独成栏(分数降序, cap 截断)。"""
    extracted: list[str] = []
    for section in ("lead", "top", "briefs"):
        kept = []
        for item_id in sections[section]:
            if predicate(item_id):
                extracted.append(item_id)
            else:
                kept.append(item_id)
        sections[section] = kept
    extracted.sort(key=lambda item_id: -score_by_id.get(item_id, 0.0))
    if cap is not None:
        overflow = extracted[cap:]
        extracted = extracted[:cap]
        # 超出上限的反方/机会条目回 briefs, 不丢条目
        sections["briefs"].extend(overflow)
    return extracted


WARM_ADAPTERS = ("thepaper_warm", "html_columns")
WARM_LOOKBACK_HOURS = 48
WARM_PER_SOURCE_CAP = 2
WARM_CANDIDATES_PER_SOURCE = 4


def _warm_candidates(conn: sqlite3.Connection, *, now_utc: str, exclude: set[str], warmth_sources: list[str],
                     limit: int) -> list[tuple[dict, float]]:
    """近 48 小时入库、来自温暖兜底源或人情味采集器的条目，按发布时间新到旧，兜底源优先。"""
    cutoff = (datetime.fromisoformat(str(now_utc).replace("Z", "+00:00")) - timedelta(hours=WARM_LOOKBACK_HOURS))
    cutoff_z = cutoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT item_id, source, adapter_name, title, ts FROM items WHERE first_ingested_at >= ? ORDER BY ts DESC, rowid",
        (cutoff_z,),
    ).fetchall()
    fallback, adapters, per_source = [], [], {}
    for row in rows:
        item_id = str(row["item_id"])
        if item_id in exclude or per_source.get(row["source"], 0) >= WARM_CANDIDATES_PER_SOURCE:
            continue
        entry = {"item_id": item_id, "source": row["source"], "title": row["title"]}
        if _source_is_warm(row["source"], warmth_sources):
            fallback.append((entry, 0.0))
        elif row["adapter_name"] in WARM_ADAPTERS:
            adapters.append((entry, 0.0))
        else:
            continue
        per_source[row["source"]] = per_source.get(row["source"], 0) + 1
    # 人情味采集器的条目先送预处理（具名真人真事多），兜底源补后；单源最多 WARM_CANDIDATES_PER_SOURCE 条
    return (adapters + fallback)[: max(0, limit)]


def _select_warmth(
    conn: sqlite3.Connection,
    pool: list[tuple[dict, float]],
    *,
    placed_ids: set[str],
    warmth_sources: list[str],
    cap: int | None,
) -> list[str]:
    """温暖栏(契约第 8 节): 先放预处理判 lane_tags 含 warmth 的（具名真人真事），温暖兜底源只补空位；
    每个源最多 WARM_PER_SOURCE_CAP 条。10-05 修：兜底源原先与 AI 判定同权，一期 5 条全是 Good News Network 趣闻。"""
    ordered = sorted(pool, key=lambda pair: -pair[1])
    judged = [c for c, _s in ordered if "warmth" in _lane_tags_of(conn, str(c["item_id"]))]
    fallback = [c for c, _s in ordered if _source_is_warm(c.get("source"), warmth_sources)]
    picked, per_source = [], {}
    for candidate in judged + fallback:
        item_id = str(candidate["item_id"])
        if item_id in placed_ids or item_id in picked:
            continue
        source = str(candidate.get("source") or "")
        if per_source.get(source, 0) >= WARM_PER_SOURCE_CAP:
            continue
        picked.append(item_id)
        per_source[source] = per_source.get(source, 0) + 1
        if cap is not None and len(picked) >= cap:
            break
    return picked


def _monday_extras(conn: sqlite3.Connection, *, date_local: date, now_utc: str, llm_call, profile_dir, follow_cap: int) -> None:
    """周一(东京)出版时: 推荐关注(自动跑一次) + 每周校准来信/砍栏提案。失败不挡出版。"""
    profile_path = (Path(profile_dir) / "reading_profile.md") if profile_dir is not None else None
    try:
        from personal_intel_loop import paper_follow

        paper_follow.suggest(conn, llm_call=llm_call, profile_path=profile_path, max_n=follow_cap, now_utc=now_utc)
    except Exception:  # noqa: BLE001
        logger.exception("monday: follow suggest failed")
    try:
        from personal_intel_loop import paper_calibration

        paper_calibration.weekly_letters(conn, monday=date_local, now_utc=now_utc, profile_path=profile_path)
    except Exception:  # noqa: BLE001
        logger.exception("monday: calibration letters failed")


def build_edition(
    conn: sqlite3.Connection,
    *,
    date_local: date,
    n: int = 60,
    blind_share: float = 0.15,
    now_utc: str,
    select_fn=None,
    embed_fn=None,
    fetcher=None,
    llm_call=None,
    profile_dir=None,
    learn_first: bool = True,
    spool_dir=None,
    vault_lookup: Callable[[str], list[dict]] | None = None,
    search_fn=None,
    layout_path=None,
    home_cinema_db=None,
    leisure_authors_path=None,
) -> dict:
    """出版一期报纸: 补收 spool → (可选先 learn) → 选条目(社交条目拉出来聚类写综述)
    → 写 editions(同日重跑先删后写) → 全文 → AI 预处理(含 20% 备选) → topic 重排
    → 新知配额 → counter/opportunity/warmth/settle/risk/leisure 分区 → 版面预算 → 定稿。"""
    from personal_intel_loop.fulltext import ensure_fulltext
    from personal_intel_loop.paper_ai import preprocess
    from personal_intel_loop.paper_layout import cap_of, load_layout

    # 契约第 5 节: 出版前自动补收投递箱。补收失败不挡出版(文件留在 spool, 下次出版再收)。
    try:
        from personal_intel_loop.paper_inbox import drain_spool
        from personal_intel_loop.paper_inbox import spool_dir as default_spool_dir

        drain_spool(conn, spool_dir if spool_dir is not None else default_spool_dir(), now_utc=now_utc)
    except (OSError, sqlite3.Error) as exc:
        # 10-05 审计 A2: sqlite3.Error(库锁/磁盘满)不是 OSError 子类, 不补上会炸掉整期出版
        logging.getLogger(__name__).warning("spool drain failed, will retry next build: %s", exc)

    if learn_first:
        try:
            from personal_intel_loop.paper_learn import learn

            learn_result = learn(conn, date_local=date_local - timedelta(days=1), now_utc=now_utc, llm_call=llm_call)
            logging.getLogger(__name__).info("learn: %s", learn_result)
            if isinstance(learn_result, dict) and learn_result.get("error"):
                logging.getLogger(__name__).warning("learn failed: %s", learn_result.get("error"))
        except Exception:
            logging.getLogger(__name__).exception("learn crashed; edition continues")  # 学习失败不影响出版，但要留痕（10-05 审计 A1）

    if select_fn is None:
        select_fn = _default_select
    if embed_fn is None:
        embed_fn = _default_embed

    layout = load_layout(layout_path)
    caps = layout["caps"]
    warmth_sources = layout["warmth_sources"]
    date_iso = date_local.isoformat()
    built_at = normalize_dt_to_utc_z(now_utc)
    blindspots = _load_blindspots(profile_dir)

    blind_n = round(n * blind_share)
    main_n = n - blind_n
    ranked = _collect_main(conn, date_local=date_local, main_n=main_n, now_utc=now_utc, select_fn=select_fn)
    scored_all = _rerank(conn, ranked)

    # 6.4: 社交条目拉出主线, 聚类写综述; 单条照普通 Item(仍标 kind)。
    social = [(c, s) for c, s in scored_all if adapter_of(c.get("source")) in SOCIAL_ADAPTERS]
    others = [(c, s) for c, s in scored_all if adapter_of(c.get("source")) not in SOCIAL_ADAPTERS]
    digest_entries: list[tuple[dict, float]] = []
    consumed: set[str] = set()
    if social:
        try:
            from personal_intel_loop.paper_social import build_digests

            digest_entries, consumed = build_digests(
                conn, date_iso=date_iso, social_scored=social, embed_fn=embed_fn, llm_call=llm_call, now_utc=built_at
            )
        except Exception:  # noqa: BLE001 — 综述失败不挡出版
            logger.exception("social digests failed")
    # 10-04 首期 dry-run：单条微博回复/短帖进了主线 → 没归进综述的社交单帖，正文 <200 字的不上版（仍在「全部来源」里）
    singletons = [(c, s) for c, s in social if str(c["item_id"]) not in consumed and len(c.get("body") or "") >= SOCIAL_SINGLETON_MIN_CHARS]
    pool = sorted(others + singletons + digest_entries, key=lambda pair: -pair[1])

    selected = pool[:main_n]
    reserve_n = math.ceil(main_n * RESERVE_SHARE)
    reserve = pool[main_n : main_n + reserve_n]

    lead, top, briefs = _assign_sections([entry for entry, _score in selected])
    blind: list[tuple[dict, str]] = []
    if blindspots and blind_n > 0:
        blind = _select_blind(
            conn,
            now_utc=now_utc,
            main_ids={str(entry["item_id"]) for entry, _score in selected} | set(consumed),
            blindspots=blindspots,
            blind_n=blind_n,
            embed_fn=embed_fn,
            date_local=date_local,
            blind_judge=default_blind_judge if llm_call is None else None,  # 测试注入 llm_call 时不走真模型
        )

    sections: dict[str, list[str]] = {
        "lead": [str(entry["item_id"]) for entry in lead],
        "top": [str(entry["item_id"]) for entry in top],
        "briefs": [str(entry["item_id"]) for entry in briefs],
    }
    score_by_id = {str(entry["item_id"]): score for entry, score in pool}

    # 预处理集合: 主线 + 盲区 + 7.2 备选(20%) + 温暖源备选
    pre_ids = list(dict.fromkeys(
        sections["lead"] + sections["top"] + sections["briefs"]
        + [str(candidate["item_id"]) for candidate, _reason in blind]
        + [str(entry["item_id"]) for entry, _score in reserve]
    ))
    # 温暖栏候选不走兴趣排序（契约 §8）：直接从库里取近 48 小时温暖兜底源与人情味采集器的条目，
    # 交预处理判 lane_tags。10-05 修：原先只在兴趣排序池里捡，温暖条目几乎进不了池，10-05 期温暖栏为 0。
    warm_extras = _warm_candidates(conn, now_utc=built_at, exclude=set(pre_ids), warmth_sources=warmth_sources,
                                   limit=(cap_of(caps, "warmth") or 5) * 4)
    pre_ids.extend(str(entry["item_id"]) for entry, _score in warm_extras)

    # 9 风险栏的官方真实条目要在预处理**之前**选出来: 它们是 items 表里的真实条目, 得照常走
    # 全文与 AI 预处理, 否则版面上一行导语都没有(行前简报是合成条目, 不走这条)。
    # 这里先不带 cap 取全部候选, 截断与「已在别区上版」的剔除等到所有分区都算完再做。
    risk_real_candidates: list[str] = []
    try:
        from personal_intel_loop import paper_risk

        risk_real_candidates = paper_risk.select_real_risk_items(
            conn, date_local=date_local, now_utc=built_at, cap=None
        )
        pre_ids.extend(risk_real_candidates)
    except Exception:  # noqa: BLE001
        logger.exception("real risk item selection failed")
    pre_ids = list(dict.fromkeys(pre_ids))

    if fetcher is None:
        ensure_fulltext(conn, pre_ids)
    else:
        ensure_fulltext(conn, pre_ids, fetcher=fetcher)
    preprocess(conn, pre_ids, llm_call=llm_call, vault_lookup=vault_lookup)

    # topic_offset 版面重排: 只动主线, 盲区版不受任何 offset 影响
    reranked = _topic_rerank(conn, selected)
    if reranked is not None:
        selected = reranked
        lead, top, briefs = _assign_sections([entry for entry, _score in selected])
        sections = {
            "lead": [str(entry["item_id"]) for entry in lead],
            "top": [str(entry["item_id"]) for entry in top],
            "briefs": [str(entry["item_id"]) for entry in briefs],
        }
        score_by_id.update({str(entry["item_id"]): score for entry, score in selected})

    # 7.2 配额 + counter/opportunity 分区
    _apply_novelty_quota(conn, sections, reserve, score_by_id)
    counter = _extract_lane_section(
        conn, sections, score_by_id=score_by_id, cap=cap_of(caps, "counter"),
        predicate=lambda item_id: _novelty_kind(conn, item_id) == "counter",
    )
    opportunity = _extract_lane_section(
        conn, sections, score_by_id=score_by_id, cap=cap_of(caps, "opportunity"),
        predicate=lambda item_id: "opportunity" in _lane_tags_of(conn, item_id),
    )

    # 8 温暖栏: 未上版 + (lane_tags 含 warmth 或 温暖源)
    warmth = _select_warmth(
        conn,
        list(reserve) + warm_extras,
        placed_ids=set(sections["lead"] + sections["top"] + sections["briefs"] + counter + opportunity),
        warmth_sources=warmth_sources,
        cap=cap_of(caps, "warmth"),
    )

    # 10.2 结算栏: 到期未结断言初判(幂等, 已判跳过)
    settle_ids: list[str] = []
    try:
        from personal_intel_loop import paper_settle

        for prepared in paper_settle.run_prechecks(conn, date_local=date_local, now_utc=built_at, llm_call=llm_call, search_fn=search_fn):
            settle_ids.append(str(prepared["claim_row"]["item_id"]))
        settle_ids = list(dict.fromkeys(settle_ids))[: cap_of(caps, "settle") or 0]
        for item_id in settle_ids:  # 断言条目若已在主线, 移入结算栏
            for section in ("lead", "top", "briefs"):
                if item_id in sections[section]:
                    sections[section].remove(item_id)
    except Exception:  # noqa: BLE001
        logger.exception("settle prechecks failed")

    # 9 风险栏(一): 行前风险简报(合成条目, 存 digests)
    risk_payloads: list[dict] = []
    risk_members: dict[str, list[str]] = {}
    try:
        from personal_intel_loop import paper_risk

        risk_payloads, risk_members = paper_risk.generate(
            conn, date_local=date_local, now_utc=built_at, llm_call=llm_call, search_fn=search_fn
        )
    except Exception:  # noqa: BLE001
        logger.exception("risk generation failed")
    risk_ids = [str(payload["item_id"]) for payload in risk_payloads]

    # 本期已在别的分区上版的条目(同一期同一条只落一个分区, 见下面 editions 写入的 placed 去重)。
    placed_elsewhere = (
        set(sections["lead"]) | set(sections["top"]) | set(sections["briefs"])
        | set(counter) | set(opportunity) | set(warmth) | set(settle_ids)
        | {str(candidate["item_id"]) for candidate, _reason in blind}
    )

    # 9 风险栏(二): 采集器入库的官方真实条目(home_alerts / mofa_anzen / who_don / cn_consular /
    # enso_status, source_payload.kind=="risk")。契约第 9 节「当期所有 kind:"risk" 条目」——
    # 以前这里只发行前简报, 采集器入库的一条都上不了版, 常驻东京的读者风险栏永远空。
    # 这些是**真实条目**(在 items 表里, 预处理集合里已含), 照常走版面, 不写 digests。
    # 上面的预处理阶段已选出 risk_real_candidates; 这里剔除已在本期别的分区上版的, 再按上限截断。
    risk_real_ids = [
        item_id
        for item_id in risk_real_candidates
        if item_id not in placed_elsewhere
    ]
    risk_real_cap = cap_of(caps, "risk_real")
    if risk_real_cap is not None:
        risk_real_ids = risk_real_ids[:risk_real_cap]
    risk_ids.extend(risk_real_ids)

    # 10.3 闲与美栏: Home Cinema 新增(合成) + 作者新书(本库条目)
    leisure_ids: list[str] = []
    leisure_payloads: list[dict] = []
    try:
        from personal_intel_loop import paper_leisure, paper_social

        found = paper_leisure.collect(
            conn, date_local=date_local, now_utc=built_at,
            home_cinema_db=home_cinema_db, authors_path=leisure_authors_path,
        )
        for index, entry in enumerate(found["home_cinema"]):
            item_id = f"leisure:hc:{date_iso}:{index}"
            payload = item_frame()
            payload.update(
                {
                    "item_id": item_id,
                    "title": entry["title"],
                    "url": None,
                    "source": "leisure:home_cinema",
                    "source_label": "闲与美",
                    "author_key": "leisure:home_cinema",
                    "author_label": "闲与美",
                    "published_at": built_at,
                    "lede": f"Home Cinema 近 7 天新增({entry.get('added_on') or '?'} 入库)。",
                    "one_liner": entry["title"],
                    "topic": "闲与美",
                    "kind": "leisure",
                    "why_here": {"profile_hit": None, "lane": "leisure", "blind_reason": None},
                }
            )
            leisure_payloads.append(payload)
        leisure_ids = [str(payload["item_id"]) for payload in leisure_payloads]
        leisure_ids.extend(str(entry["item_id"]) for entry in found["books"])
        leisure_ids = leisure_ids[: cap_of(caps, "leisure") or 0]
    except Exception:  # noqa: BLE001
        logger.exception("leisure collect failed")

    # 版面预算: 超出的不上版(进全部来源); warmth 与主线各栏截断
    for section in ("lead", "top", "briefs"):
        cap = cap_of(caps, section)
        if cap is not None:
            sections[section] = sections[section][:cap]
    warmth_cap = cap_of(caps, "warmth")
    if warmth_cap is not None:
        warmth = warmth[:warmth_cap]

    # 合成条目落 digests(同日重跑先删); editions 同日重跑先删后写
    synthetic_payloads = [payload for payload, _score in digest_entries] + risk_payloads + leisure_payloads
    synthetic_members: dict[str, list[str]] = {}
    for payload, _score in digest_entries:
        synthetic_members[str(payload["item_id"])] = [str(member_id) for member_id in payload.get("members") or []]
    synthetic_members.update(risk_members)
    digest_rows = [
        (
            str(payload["item_id"]),
            date_iso,
            json.dumps(payload, ensure_ascii=False),
            json.dumps(synthetic_members.get(str(payload["item_id"]), []), ensure_ascii=False),
            built_at,
        )
        for payload in synthetic_payloads
    ]
    with conn:
        conn.execute("DELETE FROM digests WHERE edition_date=?", (date_iso,))
        if digest_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO digests (digest_id, edition_date, payload_json, member_ids_json, created_at) VALUES (?, ?, ?, ?, ?)",
                digest_rows,
            )
        conn.execute("DELETE FROM editions WHERE edition_date=?", (date_iso,))
        rows: list[tuple] = []
        placed: set[str] = set()
        for section, ids in (
            ("lead", sections["lead"]),
            ("top", sections["top"]),
            ("counter", counter),
            ("briefs", sections["briefs"]),
            ("blind", [str(candidate["item_id"]) for candidate, _reason in blind]),
            ("warmth", warmth),
            ("risk", risk_ids),
            ("opportunity", opportunity),
            ("settle", settle_ids),
            ("leisure", leisure_ids),
        ):
            # 同一条只落第一个分区（盲区与配额替换/温暖/机会可能选中同一条，editions 主键是 (日期, item)）
            ids = [i for i in ids if i not in placed]
            placed.update(ids)
            for rank, item_id in enumerate(ids):
                reason = next((r for candidate, r in blind if str(candidate["item_id"]) == item_id), None) if section == "blind" else None
                rows.append((date_iso, item_id, section, rank, reason, built_at))
        conn.executemany(
            "INSERT INTO editions (edition_date, item_id, section, rank, blind_reason, built_at) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
    conn.commit()

    if date_local.weekday() == MONDAY:
        _monday_extras(conn, date_local=date_local, now_utc=now_utc, llm_call=llm_call, profile_dir=profile_dir, follow_cap=caps["follow"])

    # 统计: fulltext/ai 只统计真实条目(合成条目不在 items/item_fulltext 里)
    placed_real = [row[1] for row in rows if not str(row[1]).startswith(("digest:", "risk:", "leisure:"))]
    fulltext_ok = 0
    ai_ok = 0
    for item_id in placed_real:
        status_row = conn.execute("SELECT status FROM item_fulltext WHERE item_id=?", (item_id,)).fetchone()
        if status_row is not None and status_row["status"] == "ok":
            fulltext_ok += 1
        ai_row = conn.execute("SELECT error FROM item_ai WHERE item_id=?", (item_id,)).fetchone()
        if ai_row is not None and ai_row["error"] is None:
            ai_ok += 1

    def _count(section: str) -> int:
        return sum(1 for row in rows if row[2] == section)

    return {
        "date": date_iso,
        "picked": _count("lead") + _count("top") + _count("counter") + _count("briefs") + _count("opportunity") + _count("settle"),
        "blind": _count("blind"),
        "fulltext_ok": fulltext_ok,
        "ai_ok": ai_ok,
        "digests": sum(1 for row in rows if str(row[1]).startswith("digest:")),
        "warmth": _count("warmth"),
        "risk": _count("risk"),
        "leisure": _count("leisure"),
    }


def plan_edition(
    conn: sqlite3.Connection,
    *,
    date_local: date,
    n: int = 60,
    blind_share: float = 0.15,
    now_utc: str,
    select_fn=None,
    embed_fn=None,
    profile_dir=None,
) -> dict:
    """dry-run: 只算入选清单与分区, 不写库、不抓网、不调模型(短路点在第一个副作用之前)。

    综述/配额等需要预处理与模型的步骤不在 dry-run 里, 分区只含 lead/top/briefs/blind。
    """
    if select_fn is None:
        select_fn = _default_select
    if embed_fn is None:
        embed_fn = _default_embed
    blindspots = _load_blindspots(profile_dir)
    _main_n, _scored_main, lead, top, briefs, blind = _compute_edition(
        conn, date_local=date_local, n=n, blind_share=blind_share, now_utc=now_utc,
        select_fn=select_fn, embed_fn=embed_fn, blindspots=blindspots,
    )

    def _entry(candidate: dict) -> dict:
        return {"item_id": candidate["item_id"], "title": candidate.get("title") or "", "source": candidate.get("source") or ""}

    return {
        "date": date_local.isoformat(),
        "picked": len(lead) + len(top) + len(briefs),
        "blind": len(blind),
        "sections": {
            "lead": [_entry(c) for c in lead],
            "top": [_entry(c) for c in top],
            "briefs": [_entry(c) for c in briefs],
            "blind": [_entry(c) | {"blind_reason": reason} for c, reason in blind],
        },
    }
