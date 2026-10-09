from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_intel_loop.legacy_import import import_weibo_feedback_log
from personal_intel_loop.schemas import Item, compute_item_id
from personal_intel_loop.store import upsert_item


def _ingest_weibo_item(conn, *, uid: str, post_id: str) -> None:
    source = f"weibo_timeline:{uid}"
    item = Item(
        id=compute_item_id(source, uid=uid, post_id=post_id),
        source=source,
        url=f"https://m.weibo.cn/status/{post_id}",
        title=f"post {post_id}",
        body="body",
        author="alpha",
        ts="2026-04-01T00:00:00Z",
        lang="zh",
        tags=[],
    )
    with conn:
        upsert_item(conn, item, adapter_name="weibo_timeline", source_payload_json="{}")


def _write_log(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "feedback_log.jsonl"
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", "utf-8")
    return path


def test_import_creates_promotion_events_for_known_items(db_conn, tmp_path):
    _ingest_weibo_item(db_conn, uid="111", post_id="p1")
    _ingest_weibo_item(db_conn, uid="222", post_id="q1")
    log = _write_log(
        tmp_path,
        [
            {"uid": "111", "post_id": "p1", "action": "useful", "logged_at": "2026-04-10T10:00:00+00:00"},
            {"uid": "222", "post_id": "q1", "action": "less_like_this", "logged_at": "2026-04-11T10:00:00+00:00"},
        ],
    )
    with db_conn:
        result = import_weibo_feedback_log(db_conn, feedback_log_path=log)
    assert result.inserted_rows == 2
    assert result.skipped_missing_item == 0
    rows = db_conn.execute("SELECT event_type, event_weight FROM promotion_events ORDER BY event_type").fetchall()
    assert {(r["event_type"], round(r["event_weight"], 2)) for r in rows} == {
        ("less_like_this", -1.0),
        ("useful", 1.5),
    }


def test_import_skips_missing_items_and_unknown_actions(db_conn, tmp_path):
    _ingest_weibo_item(db_conn, uid="111", post_id="p1")
    log = _write_log(
        tmp_path,
        [
            {"uid": "111", "post_id": "missing", "action": "useful", "logged_at": "2026-04-10T10:00:00+00:00"},
            {"uid": "111", "post_id": "p1", "action": "totally_fake", "logged_at": "2026-04-10T10:00:00+00:00"},
            {"uid": "", "post_id": "", "action": "useful", "logged_at": "2026-04-10T10:00:00+00:00"},
            {"uid": "111", "post_id": "p1", "action": "light", "logged_at": "2026-04-10T10:00:00+00:00"},
        ],
    )
    with db_conn:
        result = import_weibo_feedback_log(db_conn, feedback_log_path=log)
    assert result.scanned_rows == 4
    assert result.inserted_rows == 1
    assert result.skipped_missing_item == 1
    assert result.skipped_unsupported_action == 1
    assert result.skipped_bad_rows == 1


def test_import_is_idempotent(db_conn, tmp_path):
    _ingest_weibo_item(db_conn, uid="111", post_id="p1")
    log = _write_log(
        tmp_path,
        [
            {"uid": "111", "post_id": "p1", "action": "useful", "logged_at": "2026-04-10T10:00:00+00:00"},
        ],
    )
    with db_conn:
        first = import_weibo_feedback_log(db_conn, feedback_log_path=log)
    with db_conn:
        second = import_weibo_feedback_log(db_conn, feedback_log_path=log)
    assert first.inserted_rows == 1
    assert second.inserted_rows == 0
    count = db_conn.execute("SELECT COUNT(*) FROM promotion_events").fetchone()[0]
    assert count == 1
