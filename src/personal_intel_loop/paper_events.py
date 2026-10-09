"""行为事件接收(契约第 4 节 POST /api/paper/events)。

前端攒批发来 {"session_id", "events": [Event…]}, sendBeacon 会用 text/plain,
web 层一律按 JSON 解析(与 Content-Type 无关)。逐条校验: kind 必须在契约事件表内
(未知 kind 丢弃并计数), ms 为非负整数或 null, ts 必须是可解析的 ISO 8601。
合法事件一个事务写入; 返回 {"ok", "stored", "dropped"}。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from personal_intel_loop.schemas import normalize_dt_to_utc_z

# 契约第 4 节事件表; open_inbox 来自第 5 节(投递箱点开也算一次行为事件)
EVENT_KINDS = (
    "impression",
    "open_item",
    "open_original",
    "item_dwell",
    "expand_feedback",
    "focus",
    "session_start",
    "session_end",
    "app_active",
    "app_inactive",
    "open_inbox",
)
MAX_EVENTS_PER_REQUEST = 500


def _valid_ms(ms) -> bool:
    if ms is None:
        return True
    if isinstance(ms, bool) or not isinstance(ms, int):
        return False
    return ms >= 0


def _valid_ts(ts) -> bool:
    if not isinstance(ts, str) or not ts.strip():
        return False
    try:
        datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _clean_event(raw) -> tuple | None:
    """校验单条事件, 合法返回入库元组, 不合法返回 None(丢弃计数)。"""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind not in EVENT_KINDS:
        return None
    if not _valid_ts(raw.get("ts")) or not _valid_ms(raw.get("ms")):
        return None
    item_id = raw.get("item_id")
    edition_date = raw.get("edition_date")
    meta = raw.get("meta")
    meta_json = json.dumps(meta if isinstance(meta, dict) else {}, ensure_ascii=False)
    ts = str(raw["ts"]).strip()
    if not ts.endswith("Z"):
        try:
            ts = normalize_dt_to_utc_z(datetime.fromisoformat(ts))  # 10-05 审计 F18：带偏移的 ts 归一成 UTC Z，免得字符串比较错桶
        except ValueError:
            return None  # 不带时区的 ts 无法定位到 UTC，拒收
    return (
        ts,
        kind,
        item_id if isinstance(item_id, str) and item_id else None,
        edition_date if isinstance(edition_date, str) and edition_date else None,
        raw.get("ms"),
        meta_json,
    )


def store_events(conn: sqlite3.Connection, payload, *, now_utc: str) -> dict:
    """校验并写一批行为事件(一个事务)。session_id 非空、events 是列表且 ≤500 条, 否则 ValueError(HTTP 400)。"""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id is required")
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("events must be a list")
    if len(events) > MAX_EVENTS_PER_REQUEST:
        raise ValueError(f"too many events: {len(events)} > {MAX_EVENTS_PER_REQUEST}")

    rows = []
    dropped = 0
    for raw in events:
        row = _clean_event(raw)
        if row is None:
            dropped += 1
        else:
            rows.append((session_id, *row, normalize_dt_to_utc_z(now_utc)))

    if rows:
        with conn:
            conn.executemany(
                "INSERT INTO ui_events (session_id, ts, kind, item_id, edition_date, ms, meta_json, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
    conn.commit()
    return {"ok": True, "stored": len(rows), "dropped": dropped}
