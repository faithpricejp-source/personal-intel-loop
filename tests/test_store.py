from __future__ import annotations

from personal_intel_loop.schemas import FEEDBACK_EVENT_WEIGHTS, FeedbackEvent, compute_feedback_event_id
from personal_intel_loop.store import (
    fetch_item,
    record_feedback_event,
    recompute_source_trust,
    replace_digest_inclusions,
    upsert_item,
)
from tests.conftest import make_item


def test_item_upsert_bootstraps_source_trust(db_conn):
    item = make_item()
    with db_conn:
        assert upsert_item(db_conn, item, adapter_name="rss_briefing", source_payload_json="{}") is True
    row = db_conn.execute("SELECT * FROM source_trust WHERE source=?", (item.source,)).fetchone()
    assert row is not None
    assert row["prior_score"] == 0.35


def test_store_item_upsert_preserves_first_ingested_at(db_conn):
    item = make_item()
    with db_conn:
        assert upsert_item(db_conn, item, adapter_name="rss_briefing", source_payload_json="{}") is True
    first = fetch_item(db_conn, item.id)["first_ingested_at"]
    updated = make_item(title="Updated title")
    with db_conn:
        assert upsert_item(db_conn, updated, adapter_name="rss_briefing", source_payload_json="{}") is False
    row = fetch_item(db_conn, item.id)
    assert row["first_ingested_at"] == first
    assert row["title"] == "Updated title"


def test_trust_recompute_uses_digest_inclusions_and_stored_event_weight(db_conn):
    item = make_item()
    with db_conn:
        upsert_item(db_conn, item, adapter_name="rss_briefing", source_payload_json="{}")
        replace_digest_inclusions(
            db_conn,
            digest_kind="daily",
            digest_date="2026-04-19",
            digest_path="/tmp/intel_loop_digest_2026-04-19.md",
            item_rows=[(item.id, item.source)],
            included_at_utc="2026-04-19T00:00:00Z",
        )
        event = FeedbackEvent(
            event_id=compute_feedback_event_id(
                origin="cli",
                item_id=item.id,
                action="promote_to_src",
                event_ts_utc_iso="2026-04-19T00:00:01Z",
            ),
            item_id=item.id,
            action="promote_to_src",
            origin="cli",
            event_ts="2026-04-19T00:00:01Z",
        )
        assert record_feedback_event(db_conn, event) is True
        recompute_source_trust(db_conn, now_utc="2026-04-20T00:00:00Z")
    before = db_conn.execute("SELECT trust_score FROM source_trust WHERE source=?", (item.source,)).fetchone()["trust_score"]
    original = FEEDBACK_EVENT_WEIGHTS["promote_to_src"]
    FEEDBACK_EVENT_WEIGHTS["promote_to_src"] = 999.0
    try:
        with db_conn:
            recompute_source_trust(db_conn, now_utc="2026-04-20T00:00:00Z")
    finally:
        FEEDBACK_EVENT_WEIGHTS["promote_to_src"] = original
    after = db_conn.execute("SELECT trust_score FROM source_trust WHERE source=?", (item.source,)).fetchone()["trust_score"]
    row = db_conn.execute("SELECT digested_items_seen_90d FROM source_trust WHERE source=?", (item.source,)).fetchone()
    assert row["digested_items_seen_90d"] == 1
    assert before == after


def test_duplicate_feedback_event_is_idempotent(db_conn):
    item = make_item()
    with db_conn:
        upsert_item(db_conn, item, adapter_name="rss_briefing", source_payload_json="{}")
        event = FeedbackEvent(
            event_id=compute_feedback_event_id(
                origin="cli",
                item_id=item.id,
                action="promote_to_src",
                event_ts_utc_iso="2026-04-19T00:00:01Z",
            ),
            item_id=item.id,
            action="promote_to_src",
            origin="cli",
            event_ts="2026-04-19T00:00:01Z",
        )
        assert record_feedback_event(db_conn, event) is True
        assert record_feedback_event(db_conn, event) is False
