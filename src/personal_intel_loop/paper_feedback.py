"""评分 → 信任旋钮(paper 页的多维反馈)。

契约 docs/paper_v2_contract.md:
- overall/quality 当场折进 source_trust(写 promotion_events, recompute_source_trust 自动吃到);
- author 折进 author_trust(分数 = (2+n_up)/(4+n_up+n_down));
- style/topic 只记 item_ratings, 排队等「编辑部来信」蒸馏;
- 同一 (item_id, dim) 只保留一条: 改主意=覆盖, value=0=删除(撤销要回退旧的副作用)。
"""
from __future__ import annotations

import json
import sqlite3

from personal_intel_loop.paper_common import author_key_from_payload, author_score, item_ai_payload, source_label_of
from personal_intel_loop.schemas import FeedbackEvent, normalize_dt_to_utc_z, sha1_hex
from personal_intel_loop.store import fetch_item, record_feedback_event, recompute_source_trust

RATING_DIMENSIONS = ("overall", "quality", "author", "style", "topic")
TRUST_DIMENSIONS = ("overall", "quality")

# 事件类型按维度查表(维度先过白名单, 再取常量)
RATE_EVENT_UP = {"overall": "rate_overall_up", "quality": "rate_quality_up"}
RATE_EVENT_DOWN = {"overall": "rate_overall_down", "quality": "rate_quality_down"}
QUEUED_EFFECT = {
    "kind": "queued",
    "label": "已记，明早编辑部来信里给出修订提案",
    "before": None,
    "after": None,
}

# 输入容错: web 层传来的 value 可能是字符串
_VALUE_MAP = {"1": 1, "-1": -1, "0": 0, 1: 1, -1: -1, 0: 0}


def _source_trust(conn: sqlite3.Connection, source: str) -> float:
    row = conn.execute("SELECT trust_score FROM source_trust WHERE source=?", (source,)).fetchone()
    return float(row["trust_score"]) if row else 0.35


def _author_key_for_item(conn: sqlite3.Connection, item: sqlite3.Row) -> str:
    payload = item_ai_payload(conn, item["item_id"])
    payload_dict = payload if isinstance(payload, dict) else None
    try:
        raw_payload = json.loads(item["source_payload_json"] or "{}")
    except json.JSONDecodeError:
        raw_payload = {}
    source_label = source_label_of(item["source"], raw_payload if isinstance(raw_payload, dict) else None)
    return author_key_from_payload(payload_dict, source_label)


def _upsert_author_trust(conn: sqlite3.Connection, key: str, up: int, down: int, now_utc: str) -> None:
    row = conn.execute("SELECT muted FROM author_trust WHERE author_key=?", (key,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO author_trust (author_key, label, n_up, n_down, muted, updated_at) VALUES (?, ?, ?, ?, 0, ?)",
            (key, key, up, down, now_utc),
        )
    else:
        conn.execute(
            "UPDATE author_trust SET n_up=?, n_down=?, updated_at=? WHERE author_key=?",
            (up, down, now_utc, key),
        )


def rate(conn: sqlite3.Connection, item_id: str, dim: str, value, *, now_utc: str) -> dict:
    """记一条多维评分并当场调整信任旋钮, 返回契约的 {"ok","item_id","dim","value","effect"}。"""
    if dim not in RATING_DIMENSIONS:
        raise ValueError(f"unsupported rating dim: {dim}")
    try:
        value = _VALUE_MAP[value]
    except (KeyError, TypeError):
        raise ValueError(f"unsupported rating value: {value!r}") from None
    if value not in (1, -1, 0):
        raise ValueError(f"unsupported rating value: {value!r}")

    item = fetch_item(conn, item_id)
    if item is None:
        raise KeyError(f"unknown item_id: {item_id}")

    source = item["source"]
    source_before = _source_trust(conn, source)
    old = conn.execute(
        "SELECT value, author_key FROM item_ratings WHERE item_id=? AND dim=?",
        (item_id, dim),
    ).fetchone()
    old_value = int(old["value"]) if old else None

    if dim == "author":
        # 撤销时回退到**当时那条评分**的作者; 新评分落在当前署名归一的作者上
        undo_key = (old["author_key"] if old and old["author_key"] else None) or _author_key_for_item(conn, item)
        rate_key = _author_key_for_item(conn, item)

        def _counts(key: str) -> tuple[int, int]:
            row = conn.execute("SELECT n_up, n_down FROM author_trust WHERE author_key=?", (key,)).fetchone()
            return (int(row["n_up"]), int(row["n_down"])) if row else (0, 0)

        before_up, before_down = _counts(undo_key if value == 0 else rate_key)
        author_before = author_score(before_up, before_down)
    else:
        author_before = None

    with conn:
        if value == 0:
            conn.execute("DELETE FROM item_ratings WHERE item_id=? AND dim=?", (item_id, dim))
        else:
            # 同 (item_id, dim) 只留一条: 改主意=整行覆盖(distilled_at 归位, 新评分要重新蒸馏)
            conn.execute(
                "INSERT OR REPLACE INTO item_ratings (item_id, dim, value, author_key, ts, distilled_at) VALUES (?, ?, ?, ?, ?, NULL)",
                (item_id, dim, value, rate_key if dim == "author" else None, now_utc),
            )

        if dim in TRUST_DIMENSIONS:
            # 覆盖/撤销: 该 item 该维度的旧事件先删再写, trust 由 recompute 全量重算
            if dim == "overall":
                conn.execute("DELETE FROM promotion_events WHERE item_id=? AND event_type IN ('rate_overall_up','rate_overall_down')", (item_id,))
            else:
                conn.execute("DELETE FROM promotion_events WHERE item_id=? AND event_type IN ('rate_quality_up','rate_quality_down')", (item_id,))
            if value != 0:
                event_type = RATE_EVENT_UP[dim] if value == 1 else RATE_EVENT_DOWN[dim]
                record_feedback_event(
                    conn,
                    FeedbackEvent(
                        event_id=sha1_hex(f"paper|{item_id}|{dim}"),
                        item_id=item_id,
                        action=event_type,
                        origin="paper",
                        digest_path=None,
                        event_ts=now_utc,
                    ),
                )
            recompute_source_trust(conn, now_utc=normalize_dt_to_utc_z(now_utc))
            after = _source_trust(conn, source)
            effect = {
                "kind": "source_trust",
                "label": f"此源信任 {source_before:.2f}→{after:.2f}",
                "before": source_before,
                "after": after,
            }
        elif dim == "author":
            if value != 0 and old is not None and undo_key != rate_key:
                # 10-05 审计 FB-2: 覆盖改评时旧评分落在旧作者键上(署名归一键已变)——
                # 旧键回退旧计数, 新键只加新计数; 否则旧键的幽灵计数永久压低信任分
                old_up, old_down = _counts(undo_key)
                if old_value == 1:
                    old_up -= 1
                elif old_value == -1:
                    old_down -= 1
                _upsert_author_trust(conn, undo_key, max(0, old_up), max(0, old_down), now_utc)
                up, down = _counts(rate_key)
                if value == 1:
                    up += 1
                elif value == -1:
                    down += 1
            else:
                up, down = _counts(undo_key if value == 0 else rate_key)
                if old_value == 1:
                    up -= 1
                elif old_value == -1:
                    down -= 1
                if value == 1:
                    up += 1
                elif value == -1:
                    down += 1
            up = max(0, up)
            down = max(0, down)
            if value == 0 and old is None:
                pass  # 10-05 审计 FB-1: 撤销一条从未存在的评分, 无计数可回退, 也不插 (0,0) 幽灵作者行
            else:
                _upsert_author_trust(conn, undo_key if value == 0 else rate_key, up, down, now_utc)
            author_after = author_score(up, down)
            effect = {
                "kind": "author_trust",
                "label": f"此作者 {author_before:.2f}→{author_after:.2f}",
                "before": author_before,
                "after": author_after,
            }
        else:
            effect = dict(QUEUED_EFFECT)

    return {"ok": True, "item_id": item_id, "dim": dim, "value": value, "effect": effect}


def record_read(conn: sqlite3.Connection, item_id: str, dwell_ms, *, now_utc: str) -> dict:
    """记录一次阅读停留(v1 只记录不入排序)。"""
    item = fetch_item(conn, item_id)
    if item is None:
        raise KeyError(f"unknown item_id: {item_id}")
    try:
        dwell = int(dwell_ms)
    except (TypeError, ValueError):
        raise ValueError(f"unsupported dwell_ms: {dwell_ms!r}") from None
    if dwell < 0:
        raise ValueError(f"unsupported dwell_ms: {dwell_ms!r}")
    conn.execute("INSERT INTO read_events (item_id, dwell_ms, ts) VALUES (?, ?, ?)", (item_id, dwell, normalize_dt_to_utc_z(now_utc)))
    conn.commit()
    return {"ok": True}


NOTE_MAX_CHARS = 4000


def save_note(conn: sqlite3.Connection, item_id: str, text, *, now_utc: str) -> dict:
    """批注（用户亲写的详细反馈）：一条一份可改，空文本删除。明早出版前的行为复盘会读到它。"""
    item_id = str(item_id or "")
    if conn.execute("SELECT 1 FROM items WHERE item_id=?", (item_id,)).fetchone() is None:
        raise KeyError(f"unknown item_id: {item_id}")
    body = str(text or "").strip()[:NOTE_MAX_CHARS]
    with conn:
        if not body:
            conn.execute("DELETE FROM item_notes WHERE item_id=?", (item_id,))
            label = "批注已删除"
        else:
            conn.execute(
                "INSERT INTO item_notes (item_id, text, created_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(item_id) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
                (item_id, body, now_utc, now_utc),
            )
            label = "批注已存，明早编辑部复盘时会读"
    return {"ok": True, "item_id": item_id, "note": body or None, "saved_at": now_utc,
            "effect": {"kind": "queued", "label": label, "before": None, "after": None}}
