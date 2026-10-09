"""TASK2 行为测试共享的造库辅助(文件名不带 test_ 前缀, pytest 不收集)。"""
from __future__ import annotations

import json

from personal_intel_loop.store import upsert_item
from tests.conftest import make_item

# 行为本地日期 2026-10-04; 事件用 00:30-06:00Z 的 Z 时间戳(JST 下仍是 10-04 本地日)
BEHAVIOR_DATE = "2026-10-04"
NOW = "2026-10-04T02:00:00Z"
TS_A = "2026-10-04T00:30:00Z"
TS_B = "2026-10-04T01:30:00Z"
TS_C = "2026-10-04T03:30:00Z"


def seed_item(
    conn,
    item_id: str,
    source: str,
    label: str,
    *,
    byline: str | None = None,
    topic: str | None = "科学",
    style_tags: tuple = ("数据密集",),
    body: str = "正文",
    title: str | None = None,
    payload_extra: dict | None = None,
) -> None:
    upsert_item(
        conn,
        make_item(item_id=item_id, source=source, url=f"https://example.com/{item_id}", title=title or f"标题 {item_id}", body=body),
        adapter_name="rss_briefing",
        source_payload_json=json.dumps({"feed_name": label}, ensure_ascii=False),
    )
    payload = {"byline": byline, "topic": topic, "style_tags": list(style_tags), "one_liner": "一句话。"}
    if payload_extra:
        payload.update(payload_extra)
    conn.execute(
        "INSERT OR REPLACE INTO item_ai (item_id, payload_json, model, error, created_at) VALUES (?, ?, 'm', NULL, ?)",
        (item_id, json.dumps(payload, ensure_ascii=False), NOW),
    )


def add_edition(conn, edition_date: str, item_id: str, section: str, rank: int, blind_reason: str | None = None) -> None:
    conn.execute(
        "INSERT INTO editions (edition_date, item_id, section, rank, blind_reason, built_at) VALUES (?, ?, ?, ?, ?, ?)",
        (edition_date, item_id, section, rank, blind_reason, NOW),
    )


def add_events(conn, rows: list[tuple[str, str | None, int | None, dict]], *, ts: str = TS_A, edition_date: str = BEHAVIOR_DATE, session_id: str = "s-1") -> None:
    """rows: [(kind, item_id, ms, meta), …] 直插 ui_events。"""
    for kind, item_id, ms, meta in rows:
        conn.execute(
            "INSERT INTO ui_events (session_id, ts, kind, item_id, edition_date, ms, meta_json, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, ts, kind, item_id, edition_date if item_id else None, ms, json.dumps(meta, ensure_ascii=False), NOW),
        )


def ai_learn_json(*adjustments: dict, proposals: list[dict] | None = None, note: str = "昨天你读了……") -> str:
    return json.dumps(
        {"adjustments": list(adjustments), "proposals": proposals or [], "reading_note": note},
        ensure_ascii=False,
    )


def clear_knob_tables(conn) -> None:
    conn.execute("DELETE FROM knob_offsets")
    conn.execute("DELETE FROM ai_adjustments")
    conn.execute("DELETE FROM edition_notes")
