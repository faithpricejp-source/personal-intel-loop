"""AI 学习(契约第 4 节「自动小调」+ 第 7.3 节护栏): 昨天行为汇总 → 调 source/author/topic 旋钮。

代码层硬约束(不信模型):
- key 必须出现在当天(过滤后)汇总里, 且该 key 当天 impression 事件条数 ≥3;
- |delta| ≤ 0.05, 超出截断; 有效值(基础值+offset)夹在 [0.05, 0.95];
  基础值: source=trust_score, author=author_score, topic=0.5;
- 同一 kind+key 一天只调一次; 最多 max_adjustments 条, 超出按 |delta| 降序保留;
- 7.3: 标签 skipped/glanced 且 (novelty.kind=counter 或 section=blind) 的条目先剔除再交模型,
  不计入任何聚合与证据; 负向 delta 只有该 key 当天存在显式负反馈(overall/quality/author
  为 -1 或原因码 not_interested)才生效。
模型输出解析失败: 不调任何旋钮, 返回 {"error": ...}, 不抛。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Callable

from personal_intel_loop.paper_behavior import (
    _aggregate_by,
    as_local_date,
    day_event_rows,
    summarize_day,
)
from personal_intel_loop.paper_common import author_score
from personal_intel_loop.schemas import normalize_dt_to_utc_z

logger = logging.getLogger(__name__)

MAX_DELTA = 0.05
EFFECTIVE_MIN = 0.05
EFFECTIVE_MAX = 0.95
EVIDENCE_MIN_IMPRESSIONS = 3
MAX_PROPOSALS_PER_DAY = 3
KNOB_KINDS = ("source", "author", "topic")
DEFAULT_SOURCE_BASE = 0.35
NOTE_MAX_CHARS = 500

LEARN_PROMPT = """你在为一份个人信息日报做「昨天的阅读行为复盘」, 决定要不要微调推荐旋钮。输出严格的 JSON(不要 markdown 代码块, 不要解释)。

## 读者画像(不含未接受的提案)
{profile}

## 当前旋钮值(有效值 = 基础值 + 偏移, 已夹在 [0.05, 0.95])
{knobs}

## 昨天每一篇的行为(标签: deep_read 深读 / read 读了 / glanced 打开没读 / skipped 看见没点 / unseen 没看到)
{table}

## 用户这一天亲手写的批注(最直接的反馈, 优先级高于一切行为信号; 能落到旋钮就落, 涉及画像措辞的写成 proposals)
{notes}

## 铁律
- 跳过不等于讨厌(可能是早知道、可能是没时间), 一天的证据只够做小调整。
- 与显式评分冲突时, 以显式评分为准。
- 目标是让用户知道更多他不知道的, 不是让他读得更多; 不要因为用户常读印证性内容就给该类加权。
- adjustments: 每条 {{"kind": "source|author|topic", "key": <必须取自上面表里出现过的来源/作者/主题>, "delta": <-0.05..0.05>, "reason": <一句中文, 必须引用具体条目或数字>}}; 没有值得调的就给空数组。
- proposals: 若画像文本本身该改(新兴趣、该降的域、文风偏好), 每条 {{"target": "<画像里的目标>", "basis": "<依据>", "suggestion": "<怎么改>"}}; 最多 3 条, 没有就空数组。
- reading_note: ≤150 字, 第二人称, 说清昨天读了什么、跳过了什么、和画像哪里不一致; 没有可说就空字符串。

## 输出
只返回一个 JSON 对象:
{{"adjustments": [], "proposals": [], "reading_note": ""}}
"""


def _clamp(value: float) -> float:
    return min(EFFECTIVE_MAX, max(EFFECTIVE_MIN, value))


def _call_cloud(prompt: str) -> tuple[str | None, str | None]:
    """缺省实现: 与 paper_ai.py 相同的云端路径(不走本地模型)。"""
    from personal_intel_loop.paper_ai import _call_cloud as paper_ai_cloud

    return paper_ai_cloud(prompt)


def _extract_json(text: str | None) -> dict | None:
    from personal_intel_loop.summarizer import _extract_json as summarizer_extract_json

    parsed = summarizer_extract_json(text or "")
    return parsed if isinstance(parsed, dict) else None


def knob_offsets(conn: sqlite3.Connection, kind: str) -> dict[str, float]:
    """某类旋钮的当前累计偏移 {key: offset}。"""
    return {
        row["key"]: float(row["offset"])
        for row in conn.execute("SELECT key, offset FROM knob_offsets WHERE kind=?", (kind,))
    }


def base_value(conn: sqlite3.Connection, kind: str, key: str) -> float:
    """旋钮基础值: source=trust_score(缺行 0.35), author=author_score(缺行 0.5), topic=0.5。"""
    if kind == "source":
        row = conn.execute("SELECT trust_score FROM source_trust WHERE source=?", (key,)).fetchone()
        return float(row["trust_score"]) if row else DEFAULT_SOURCE_BASE
    if kind == "author":
        row = conn.execute("SELECT n_up, n_down FROM author_trust WHERE author_key=?", (key,)).fetchone()
        return author_score(int(row["n_up"]), int(row["n_down"])) if row else 0.5
    return 0.5


def effective_value(conn: sqlite3.Connection, kind: str, key: str) -> float:
    """有效值 = clamp(基础值 + 累计偏移)。"""
    offset = knob_offsets(conn, kind).get(key, 0.0)
    return _clamp(base_value(conn, kind, key) + offset)


def _novelty_kind(conn: sqlite3.Connection, item_id: str) -> str | None:
    payload = conn.execute("SELECT payload_json FROM item_ai WHERE item_id=?", (item_id,)).fetchone()
    if payload is None or not payload["payload_json"]:
        return None
    try:
        parsed = json.loads(payload["payload_json"])
    except json.JSONDecodeError:
        return None
    novelty = parsed.get("novelty") if isinstance(parsed, dict) else None
    if isinstance(novelty, dict) and isinstance(novelty.get("kind"), str):
        return novelty["kind"]
    return None


def guardrail_filter(conn: sqlite3.Connection, items: list[dict]) -> list[dict]:
    """7.3: skipped/glanced 且 (novelty.kind=counter 或 section=blind) 的条目, 不计入任何聚合(先剔除再交模型)。

    契约第 8 节: 温暖栏同盲区规则——skipped/glanced 的 warmth 条目同样剔除。
    """
    out = []
    for item in items:
        if item["label"] in ("skipped", "glanced"):
            if _novelty_kind(conn, item["item_id"]) == "counter" or item["section"] in ("blind", "warmth"):
                continue
        out.append(item)
    return out


def _day_negative_keys(conn: sqlite3.Connection, items: list[dict], date_obj: date) -> dict[str, set[str]]:
    """当天有显式负反馈(overall/quality/author 为 -1, 或原因码 not_interested, 且发生在当天)的 key 集合。

    条目级标记: 一条内容被显式点了负反馈, 它的来源/作者/主题三个 key 都算拿到负反馈。
    """
    from personal_intel_loop.paper_behavior import local_date_of_ts

    negative: dict[str, set[str]] = {kind: set() for kind in KNOB_KINDS}
    for item in items:
        marked = False
        for row in conn.execute("SELECT dim, value, ts FROM item_ratings WHERE item_id=?", (item["item_id"],)):
            if row["dim"] in ("overall", "quality", "author") and int(row["value"]) == -1 and local_date_of_ts(row["ts"]) == date_obj:
                marked = True
                break
        if not marked:
            for row in conn.execute(
                "SELECT event_ts FROM promotion_events WHERE item_id=? AND event_type='not_interested' AND origin IN ('digest_checkbox','web','cli')",
                (item["item_id"],),
            ):
                if local_date_of_ts(row["event_ts"]) == date_obj:
                    marked = True
                    break
        if marked:
            negative["source"].add(item["source"])
            if item["author_key"]:
                negative["author"].add(item["author_key"])
            if item["topic"]:
                negative["topic"].add(item["topic"])
    return negative


def _knob_table(conn: sqlite3.Connection, by_source: dict, by_author: dict, by_topic: dict) -> str:
    lines = []
    offsets = {kind: knob_offsets(conn, kind) for kind in KNOB_KINDS}
    for key in sorted(by_source):
        base = base_value(conn, "source", key)
        offset = offsets["source"].get(key, 0.0)
        lines.append(f"- 源 {key}: 信任 {base:.2f} + 偏移 {offset:+.2f} = 有效 {_clamp(base + offset):.2f}")
    for key in sorted(by_author):
        base = base_value(conn, "author", key)
        offset = offsets["author"].get(key, 0.0)
        lines.append(f"- 作者 {key}: 分 {base:.2f} + 偏移 {offset:+.2f} = 有效 {_clamp(base + offset):.2f}")
    topic_keys = set(by_topic) | set(offsets["topic"])
    for key in sorted(topic_keys):
        offset = offsets["topic"].get(key, 0.0)
        lines.append(f"- 主题 {key}: 基础 0.50 + 偏移 {offset:+.2f} = 有效 {_clamp(0.5 + offset):.2f}")
    return "\n".join(lines) or "(暂无)"


def _item_line(item: dict) -> str:
    ratings = ",".join(f"{dim}:{value:+d}" for dim, value in sorted(item["ratings"].items())) or "无"
    scroll = item["max_scroll_pct"]
    return (
        f"- [{item['label']}] {item['title']} | 来源 {item['source']} | 作者 {item['author_key']}"
        f" | 主题 {item['topic'] or '未知'} | 文风 {'/'.join(item['style_tags']) or '无'}"
        f" | 版面 {item['section'] or '-'}#{item['rank'] if item['rank'] is not None else '-'}"
        f" | 曝光 {round(item['impression_ms'] / 1000)} 秒(可见 {item['impressions']} 次) | 打开 {'是' if item['opened'] else '否'}"
        f" | 停留 {round(item['dwell_ms'] / 1000)} 秒(按字数预期 {round(item['expected_read_ms'] / 1000)} 秒, 比率 {item['dwell_ratio']})"
        f" | 滚动 {scroll if scroll is not None else '-'}% | 开原文 {'是' if item['opened_original'] else '否'}"
        f" | 评分 {ratings} | 原因码 {item['reason_code'] or '无'}"
    )


def build_learn_prompt(*, profile: str, knob_table: str, items: list[dict], notes: list[dict] | None = None) -> str:
    table = "\n".join(_item_line(item) for item in items) or "(昨天没有可复盘的行为)"
    note_lines = "\n".join(
        f"- 《{n['title']}》| 来源 {n['source']} | 批注: {n['text']}" for n in (notes or [])
    ) or "(无)"
    return LEARN_PROMPT.format(profile=(profile or "").strip() or "(无画像)", knobs=knob_table, table=table, notes=note_lines)


def day_notes(conn: sqlite3.Connection, date_obj: date) -> list[dict]:
    """这一天（东京本地日）新写或改过的批注。"""
    from datetime import datetime, time as dtime, timedelta as td, timezone as tz
    from personal_intel_loop import LOCAL_TZ

    start = datetime.combine(date_obj, dtime.min, tzinfo=LOCAL_TZ).astimezone(tz.utc)
    end = start + td(days=1)
    try:
        rows = conn.execute(
            "SELECT n.text, i.title, i.source FROM item_notes n JOIN items i USING(item_id) WHERE n.updated_at >= ? AND n.updated_at < ? ORDER BY n.updated_at",
            (start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        logger.warning("day_notes unavailable, notes not reviewed: %s", exc)  # 10-05 审计 C2：别静默
        return []
    return [{"text": r["text"], "title": r["title"], "source": r["source"]} for r in rows]


def _prepare(conn: sqlite3.Connection, date_obj: date, profile_path) -> tuple[dict, list[dict], dict, dict, dict, str, str]:
    """汇总 + 护栏过滤 + 提示词。返回 (summary, filtered, by_source, by_author, by_topic, profile_text, prompt)。"""
    from personal_intel_loop.profile import profile_body_for_prompt

    summary = summarize_day(conn, date_local=date_obj)
    filtered = guardrail_filter(conn, summary["items"])
    by_source = _aggregate_by(filtered, "source")
    by_author = _aggregate_by(filtered, "author_key")
    by_topic = _aggregate_by(filtered, "topic")
    profile_text = profile_body_for_prompt(profile_path)
    knob_table = _knob_table(conn, by_source, by_author, by_topic)
    prompt = build_learn_prompt(profile=profile_text, knob_table=knob_table, items=filtered, notes=day_notes(conn, date_obj))
    return summary, filtered, by_source, by_author, by_topic, profile_text, prompt


def _resolve_profile_path(profile_path):
    from personal_intel_loop.profile import resolve_profile_path

    return profile_path if profile_path is not None else resolve_profile_path()


def _call_model(prompt: str, llm_call: Callable[[str], str | None] | None) -> tuple[str | None, str | None]:
    if llm_call is not None:
        return llm_call(prompt), None
    return _call_cloud(prompt)


def _impressions_for_key(filtered: list[dict], kind: str, key: str) -> int:
    return sum(item["impressions"] for item in filtered if _key_of(item, kind) == key)


def _key_of(item: dict, kind: str) -> str | None:
    if kind == "source":
        return item["source"]
    if kind == "author":
        return item["author_key"]
    return item["topic"]


def _collect_candidates(
    conn: sqlite3.Connection,
    parsed: dict,
    *,
    for_date: str,
    filtered: list[dict],
    by_source: dict,
    by_author: dict,
    by_topic: dict,
    negatives: dict[str, set[str]],
    max_adjustments: int,
) -> tuple[list[dict], int]:
    """模型建议 → 硬约束筛选。返回 (候选(已按 |delta| 降序、截断上限; clipped 标记在候选上), rejected 数)。"""
    by_kind = {"source": by_source, "author": by_author, "topic": by_topic}
    seen: set[tuple[str, str]] = set()
    rejected = 0
    candidates: list[dict] = []
    raw_list = parsed.get("adjustments")
    if not isinstance(raw_list, list):
        raw_list = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            rejected += 1
            continue
        kind = raw.get("kind")
        key = raw.get("key")
        delta = raw.get("delta")
        if kind not in KNOB_KINDS or not isinstance(key, str) or not key.strip():
            rejected += 1
            continue
        key = key.strip()
        if isinstance(delta, bool) or not isinstance(delta, (int, float)):
            rejected += 1
            continue
        delta = float(delta)
        if (kind, key) in seen:
            rejected += 1  # 同一响应里同 key 重复
            continue
        if conn.execute("SELECT 1 FROM ai_adjustments WHERE for_date=? AND kind=? AND key=? LIMIT 1", (for_date, kind, key)).fetchone() is not None:
            rejected += 1  # 同一 kind+key 一天只调一次(含已撤销的记录)
            continue
        if key not in by_kind[kind]:
            rejected += 1  # key 没出现在当天汇总里
            continue
        if _impressions_for_key(filtered, kind, key) < EVIDENCE_MIN_IMPRESSIONS:
            rejected += 1  # 证据不足
            continue
        if abs(delta) > MAX_DELTA:
            delta = MAX_DELTA if delta > 0 else -MAX_DELTA
            was_clipped = True
        else:
            was_clipped = False
        if delta < 0 and key not in negatives[kind]:
            rejected += 1  # 7.3: 负向 delta 需要当天显式负反馈
            continue
        seen.add((kind, key))
        candidates.append(
            {
                "kind": kind,
                "key": key,
                "delta": delta,
                "reason": str(raw.get("reason") or "")[:500],
                "clipped": was_clipped,
            }
        )
    candidates.sort(key=lambda adj: -abs(adj["delta"]))
    if len(candidates) > max_adjustments:
        rejected += len(candidates) - max_adjustments
        candidates = candidates[:max_adjustments]
    return candidates, rejected


def _apply_adjustments(conn: sqlite3.Connection, candidates: list[dict], *, for_date: str, now_utc: str, model: str | None) -> tuple[int, int]:
    """写 ai_adjustments + 更新 knob_offsets(一个事务)。返回 (applied, clipped)。

    clipped = 被截断(|delta|>0.05)或有效值被夹紧([0.05,0.95])的条数。
    """
    applied = 0
    clipped = 0
    rows = []
    knob_rows = []
    for adj in candidates:
        kind, key, delta = adj["kind"], adj["key"], adj["delta"]
        base = base_value(conn, kind, key)
        current = knob_offsets(conn, kind).get(key, 0.0)
        before_eff = _clamp(base + current)
        after_raw = base + current + delta
        after_eff = _clamp(after_raw)
        if after_eff != after_raw:
            adj["clipped"] = True  # 有效值被夹紧
        if kind == "source":
            label = f"此源信任 {before_eff:.2f}→{after_eff:.2f}"
        elif kind == "author":
            label = f"此作者 {before_eff:.2f}→{after_eff:.2f}"
        else:
            label = f"话题权重 {before_eff:.2f}→{after_eff:.2f}"
        rows.append((now_utc, for_date, kind, key, label, delta, before_eff, after_eff, adj["reason"], model))
        knob_rows.append((kind, key, current + delta, now_utc))
        applied += 1
        if adj["clipped"]:
            clipped += 1
    if rows:
        with conn:
            conn.executemany(
                "INSERT INTO ai_adjustments (ts, for_date, kind, key, label, delta, before, after, reason, reverted, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                rows,
            )
            conn.executemany(
                "INSERT OR REPLACE INTO knob_offsets (kind, key, offset, updated_at) VALUES (?, ?, ?, ?)",
                knob_rows,
            )
    conn.commit()
    return applied, clipped


def _append_proposals(parsed: dict, *, date_iso: str, profile_path) -> int:
    from personal_intel_loop.profile import append_proposal, pending_proposals

    proposals = parsed.get("proposals")
    if not isinstance(proposals, list):
        proposals = []
    prefix = f"[{date_iso} · behavior"
    already = sum(1 for line in pending_proposals(profile_path) if line.startswith(prefix))
    existing_targets = {line.split("]", 1)[0] for line in pending_proposals(profile_path) if line.startswith(prefix)}
    appended = 0
    for prop in proposals:
        if appended >= MAX_PROPOSALS_PER_DAY - already:
            break
        if not isinstance(prop, dict):
            continue
        target = str(prop.get("target") or "").strip()
        if not target:
            continue
        if f"{prefix} · {target}" in existing_targets:
            continue  # 同日重跑不重复追加同一 target 的提案（10-05 审计 C1）
        basis = str(prop.get("basis") or "").strip()
        suggestion = str(prop.get("suggestion") or "").strip()
        append_proposal(f"[{date_iso} · behavior · {target}] 依据: {basis} → 目标: {target} → 建议: {suggestion}", profile_path)
        appended += 1
    return appended


def learn(
    conn: sqlite3.Connection,
    *,
    date_local,
    now_utc,
    llm_call: Callable[[str], str | None] | None = None,
    profile_path=None,
    max_adjustments: int = 15,
) -> dict:
    """复盘一天的行为并调旋钮。返回 {"applied","clipped","rejected","proposals","note"};
    当天无任何 impression → {"skipped": "no_behavior"}(不调模型); 模型输出解析失败 → {"error": ...}(不抛)。"""
    try:
        date_obj = as_local_date(date_local)
        date_iso = date_obj.isoformat()
        rows = day_event_rows(conn, date_obj)
        if not any(row["kind"] == "impression" for row in rows) and not day_notes(conn, date_obj):
            return {"skipped": "no_behavior"}  # 有批注时即使没有曝光也要复盘（批注是最直接的反馈）

        profile_path = _resolve_profile_path(profile_path)
        summary, filtered, by_source, by_author, by_topic, _profile_text, prompt = _prepare(conn, date_obj, profile_path)

        model = None
        try:
            raw, model = _call_model(prompt, llm_call)
        except Exception as exc:
            return {"error": f"llm failed: {exc}"}
        parsed = _extract_json(raw)
        if parsed is None:
            return {"error": f"invalid json: {(raw or '')[:200]}"}

        negatives = _day_negative_keys(conn, summary["items"], date_obj)
        candidates, rejected = _collect_candidates(
            conn, parsed, for_date=date_iso, filtered=filtered, by_source=by_source, by_author=by_author,
            by_topic=by_topic, negatives=negatives, max_adjustments=max_adjustments,
        )
        # 提案(画像文件写)先于调整(SQLite 事务提交)。文件写失败时调整尚未落库,
        # 当日重跑可完整重放(proposals 有同日去重保护, ai_adjustments 未写不会判重);
        # 反过来先调整后提案, 提案失败会留下已提交的半成品, 且调整被同日判重永久挡住重试。
        proposals = _append_proposals(parsed, date_iso=date_iso, profile_path=profile_path)
        applied, clipped = _apply_adjustments(conn, candidates, for_date=date_iso, now_utc=normalize_dt_to_utc_z(now_utc), model=model)

        note = parsed.get("reading_note")
        note_written = False
        if isinstance(note, str) and note.strip():
            edition_date = (date_obj + timedelta(days=1)).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO edition_notes (edition_date, reading_note, model, created_at) VALUES (?, ?, ?, ?)",
                (edition_date, note.strip()[:NOTE_MAX_CHARS], model, normalize_dt_to_utc_z(now_utc)),
            )
            conn.commit()
            note_written = True

        return {"applied": applied, "clipped": clipped, "rejected": rejected, "proposals": proposals, "note": note_written}
    except Exception as exc:  # learn 失败不许影响出版(build_edition learn_first)
        return {"error": f"{type(exc).__name__}: {exc}"}


def learn_preview(
    conn: sqlite3.Connection,
    *,
    date_local,
    now_utc,
    llm_call: Callable[[str], str | None] | None = None,
    profile_path=None,
) -> dict:
    """dry-run: 打印汇总与模型原始输出, 不写任何表。"""
    date_obj = as_local_date(date_local)
    rows = day_event_rows(conn, date_obj)
    summary = summarize_day(conn, date_local=date_obj)
    if not any(row["kind"] == "impression" for row in rows):
        return {"date": date_obj.isoformat(), "skipped": "no_behavior", "summary": summary}
    profile_path = _resolve_profile_path(profile_path)
    _summary, _filtered, _b1, _b2, _b3, _profile_text, prompt = _prepare(conn, date_obj, profile_path)
    try:
        raw, _model = _call_model(prompt, llm_call)
    except Exception as exc:
        return {"date": date_obj.isoformat(), "summary": summary, "prompt": prompt, "error": f"llm failed: {exc}"}
    parsed = _extract_json(raw)
    result: dict[str, Any] = {"date": date_obj.isoformat(), "summary": summary, "prompt": prompt, "raw": raw}
    if parsed is None:
        result["error"] = f"invalid json: {(raw or '')[:200]}"
    else:
        result["parsed"] = parsed
    return result
