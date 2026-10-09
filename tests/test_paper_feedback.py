"""paper_feedback.rate: 覆盖不叠加/撤销回退/queued; record_read。所有时间走注入的 now_utc。"""
from __future__ import annotations

import json

import pytest

from personal_intel_loop.paper_feedback import QUEUED_EFFECT, rate, record_read
from personal_intel_loop.store import connect_db, ensure_schema, upsert_item
from tests.conftest import make_item

NOW = "2026-10-04T00:00:00Z"
SOURCE = "rss_briefing:feed_one"


@pytest.fixture
def conn(tmp_path):
    c = connect_db(tmp_path / "db.sqlite")
    ensure_schema(c)
    try:
        yield c
    finally:
        c.close()


def _add_item(conn, item_id="rss:p1", payload="{}"):
    upsert_item(conn, make_item(item_id=item_id, source=SOURCE), adapter_name="rss_briefing", source_payload_json=payload)
    conn.commit()


def _trust(conn, source=SOURCE):
    row = conn.execute("SELECT trust_score FROM source_trust WHERE source=?", (source,)).fetchone()
    return float(row["trust_score"])


def _events(conn, item_id):
    return [row[0] for row in conn.execute("SELECT event_type FROM promotion_events WHERE item_id=?", (item_id,))]


def _insert_ai_payload(conn, item_id, payload):
    conn.execute(
        "INSERT OR REPLACE INTO item_ai (item_id, payload_json, model, error, created_at) VALUES (?, ?, 'test-model', NULL, ?)",
        (item_id, json.dumps(payload, ensure_ascii=False), NOW),
    )
    conn.commit()


def test_rate_result_shape_and_source_trust_effect(conn):
    _add_item(conn)
    result = rate(conn, "rss:p1", "overall", 1, now_utc=NOW)
    assert set(result) == {"ok", "item_id", "dim", "value", "effect"}
    assert result["ok"] is True and result["value"] == 1 and result["dim"] == "overall"
    effect = result["effect"]
    assert set(effect) == {"kind", "label", "before", "after"}
    assert effect["kind"] == "source_trust"
    assert effect["before"] == pytest.approx(0.35)
    # prior 12 * 0.35 + 0.5(w=rate_overall_up), 无 digest_inclusions → 分母 12
    assert effect["after"] == pytest.approx((12 * 0.35 + 0.5) / 12)
    assert effect["label"] == f"此源信任 {effect['before']:.2f}→{effect['after']:.2f}"
    assert _events(conn, "rss:p1") == ["rate_overall_up"]
    row = conn.execute(
        "SELECT origin, event_weight FROM promotion_events WHERE item_id=?", ("rss:p1",)
    ).fetchone()
    assert row["origin"] == "paper" and row["event_weight"] == 0.5


def test_rate_overall_change_of_mind_overwrites_not_stacks(conn):
    _add_item(conn)
    rate(conn, "rss:p1", "overall", 1, now_utc=NOW)
    rate(conn, "rss:p1", "overall", -1, now_utc=NOW)
    assert _events(conn, "rss:p1") == ["rate_overall_down"]
    row = conn.execute("SELECT value FROM item_ratings WHERE item_id=? AND dim='overall'", ("rss:p1",)).fetchone()
    assert row["value"] == -1
    assert _trust(conn) == pytest.approx((12 * 0.35 - 0.5) / 12)


def test_rate_overall_undo_restores_trust(conn):
    _add_item(conn)
    base = _trust(conn)
    rate(conn, "rss:p1", "overall", 1, now_utc=NOW)
    undone = rate(conn, "rss:p1", "overall", 0, now_utc=NOW)
    assert _events(conn, "rss:p1") == []
    assert conn.execute("SELECT COUNT(*) FROM item_ratings WHERE item_id='rss:p1'").fetchone()[0] == 0
    assert _trust(conn) == pytest.approx(base)
    assert undone["effect"]["after"] == pytest.approx(base)


def test_rate_quality_uses_quality_event_and_weight(conn):
    _add_item(conn)
    rate(conn, "rss:p1", "quality", 1, now_utc=NOW)
    assert _events(conn, "rss:p1") == ["rate_quality_up"]
    row = conn.execute("SELECT event_weight FROM promotion_events WHERE item_id=?", ("rss:p1",)).fetchone()
    assert row["event_weight"] == 0.25


def test_rate_author_uses_byline_from_item_ai(conn):
    _add_item(conn, item_id="rss:a1")
    _insert_ai_payload(conn, "rss:a1", {"byline": "张三"})
    result = rate(conn, "rss:a1", "author", 1, now_utc=NOW)
    assert result["effect"]["kind"] == "author_trust"
    assert result["effect"]["before"] == pytest.approx(0.5)  # 无记录 → 0.5
    assert result["effect"]["after"] == pytest.approx((2 + 1) / (4 + 1 + 0))
    assert result["effect"]["label"] == "此作者 0.50→0.60"
    row = conn.execute("SELECT n_up, n_down, label FROM author_trust WHERE author_key='张三'").fetchone()
    assert (row["n_up"], row["n_down"]) == (1, 0)
    assert row["label"] == "张三"


def test_rate_author_falls_back_to_source_label(conn):
    _add_item(conn, item_id="rss:a2", payload=json.dumps({"feed_name": "Feed Two"}))
    rate(conn, "rss:a2", "author", 1, now_utc=NOW)
    row = conn.execute("SELECT author_key FROM item_ratings WHERE item_id='rss:a2' AND dim='author'").fetchone()
    assert row["author_key"] == "Feed Two"
    assert conn.execute("SELECT COUNT(*) FROM author_trust WHERE author_key='Feed Two'").fetchone()[0] == 1


def test_rate_author_overwrite_and_undo_rollback_counts(conn):
    _add_item(conn, item_id="rss:a3")
    rate(conn, "rss:a3", "author", 1, now_utc=NOW)
    rate(conn, "rss:a3", "author", -1, now_utc=NOW)
    row = conn.execute("SELECT n_up, n_down FROM author_trust").fetchone()
    assert (row["n_up"], row["n_down"]) == (0, 1)
    undone = rate(conn, "rss:a3", "author", 0, now_utc=NOW)
    row = conn.execute("SELECT n_up, n_down FROM author_trust").fetchone()
    assert (row["n_up"], row["n_down"]) == (0, 0)
    assert undone["effect"]["after"] == pytest.approx(0.5)
    assert undone["effect"]["label"] == "此作者 0.40→0.50"


def test_rate_style_topic_only_writes_ratings(conn):
    _add_item(conn)
    for dim in ("style", "topic"):
        result = rate(conn, "rss:p1", dim, 1, now_utc=NOW)
        assert result["effect"] == QUEUED_EFFECT
        assert result["effect"]["kind"] == "queued"
        assert result["effect"]["before"] is None and result["effect"]["after"] is None
    assert _events(conn, "rss:p1") == []
    rows = {(r[0], r[1]) for r in conn.execute("SELECT dim, value FROM item_ratings WHERE item_id='rss:p1'")}
    assert rows == {("style", 1), ("topic", 1)}


def test_rate_accepts_string_values(conn):
    _add_item(conn)
    result = rate(conn, "rss:p1", "style", "-1", now_utc=NOW)
    assert result["value"] == -1


def test_rate_validates_input(conn):
    _add_item(conn)
    with pytest.raises(ValueError):
        rate(conn, "rss:p1", "bogus", 1, now_utc=NOW)
    with pytest.raises(ValueError):
        rate(conn, "rss:p1", "overall", 2, now_utc=NOW)
    with pytest.raises(ValueError):
        rate(conn, "rss:p1", "overall", "x", now_utc=NOW)
    with pytest.raises(KeyError):
        rate(conn, "rss:missing", "overall", 1, now_utc=NOW)


def test_record_read(conn):
    _add_item(conn)
    assert record_read(conn, "rss:p1", 1234, now_utc=NOW) == {"ok": True}
    row = conn.execute("SELECT item_id, dwell_ms FROM read_events").fetchone()
    assert (row["item_id"], row["dwell_ms"]) == ("rss:p1", 1234)
    with pytest.raises(KeyError):
        record_read(conn, "rss:missing", 1, now_utc=NOW)
    with pytest.raises(ValueError):
        record_read(conn, "rss:p1", "abc", now_utc=NOW)
