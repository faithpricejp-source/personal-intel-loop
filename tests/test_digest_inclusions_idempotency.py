from __future__ import annotations

from datetime import date

import numpy as np

from personal_intel_loop.digest import select_digest_candidates, select_digest_candidates_v2
from personal_intel_loop.schemas import FeedbackEvent, compute_feedback_event_id
from personal_intel_loop.store import (
    recompute_source_trust,
    record_feedback_event,
    replace_digest_inclusions,
    upsert_item,
)
from tests.conftest import make_item


def test_same_day_force_rewrite_replaces_digest_inclusions_without_double_counting(db_conn):
    item_a = make_item(item_id="rss:item-a", url="https://example.com/a")
    item_b = make_item(item_id="rss:item-b", url="https://example.com/b")
    with db_conn:
        upsert_item(db_conn, item_a, adapter_name="rss_briefing", source_payload_json="{}")
        upsert_item(db_conn, item_b, adapter_name="rss_briefing", source_payload_json="{}")
        replace_digest_inclusions(
            db_conn,
            digest_kind="daily",
            digest_date="2026-04-19",
            digest_path="/tmp/intel_loop_digest_2026-04-19.md",
            item_rows=[(item_a.id, item_a.source)],
            included_at_utc="2026-04-19T00:00:00Z",
        )
        replace_digest_inclusions(
            db_conn,
            digest_kind="daily",
            digest_date="2026-04-19",
            digest_path="/tmp/intel_loop_digest_2026-04-19.md",
            item_rows=[(item_a.id, item_a.source), (item_b.id, item_b.source)],
            included_at_utc="2026-04-19T00:00:01Z",
        )
        recompute_source_trust(db_conn, now_utc="2026-04-20T00:00:00Z")
    row_count = db_conn.execute("SELECT COUNT(*) FROM digest_inclusions WHERE digest_date='2026-04-19'").fetchone()[0]
    trust_row = db_conn.execute(
        "SELECT digested_items_seen_90d FROM source_trust WHERE source=?",
        (item_a.source,),
    ).fetchone()
    assert row_count == 2
    assert trust_row["digested_items_seen_90d"] == 2


def test_live_feed_cooldown_skips_previous_digest_day_but_keeps_same_day_rewrite(db_conn):
    item_a = make_item(
        item_id="rss:item-a",
        url="https://example.com/a",
        ts="2026-04-20T08:00:00Z",
    )
    item_b = make_item(
        item_id="rss:item-b",
        url="https://example.com/b",
        ts="2026-04-20T09:00:00Z",
    )
    with db_conn:
        upsert_item(db_conn, item_a, adapter_name="rss_briefing", source_payload_json="{}")
        upsert_item(db_conn, item_b, adapter_name="rss_briefing", source_payload_json="{}")
        replace_digest_inclusions(
            db_conn,
            digest_kind="daily",
            digest_date="2026-04-20",
            digest_path="/tmp/intel_loop_digest_2026-04-20.md",
            item_rows=[(item_a.id, item_a.source)],
            included_at_utc="2026-04-20T10:00:00Z",
        )
        recompute_source_trust(db_conn, now_utc="2026-04-21T00:00:00Z")

    same_day_ids = {
        item["item_id"]
        for item in select_digest_candidates(
            db_conn,
            date_local=date(2026, 4, 20),
            top_k=10,
            now_utc="2026-04-20T11:00:00Z",
            live_cooldown_days=1,
        )
    }
    next_day_ids = {
        item["item_id"]
        for item in select_digest_candidates(
            db_conn,
            date_local=date(2026, 4, 21),
            top_k=10,
            now_utc="2026-04-21T11:00:00Z",
            live_cooldown_days=1,
        )
    }

    assert "rss:item-a" in same_day_ids
    assert "rss:item-a" not in next_day_ids
    assert "rss:item-b" in next_day_ids


def test_live_feed_cooldown_applies_to_v2_path(db_conn, monkeypatch):
    import personal_intel_loop.active_corpus as ac
    import personal_intel_loop.embeddings as emb
    import personal_intel_loop.vault_corpus as vc

    item_a = make_item(
        item_id="rss:item-a",
        url="https://example.com/a",
        ts="2026-04-20T08:00:00Z",
    )
    item_b = make_item(
        item_id="rss:item-b",
        url="https://example.com/b",
        ts="2026-04-20T09:00:00Z",
    )
    with db_conn:
        upsert_item(db_conn, item_a, adapter_name="rss_briefing", source_payload_json="{}")
        upsert_item(db_conn, item_b, adapter_name="rss_briefing", source_payload_json="{}")
        replace_digest_inclusions(
            db_conn,
            digest_kind="daily",
            digest_date="2026-04-20",
            digest_path="/tmp/intel_loop_digest_2026-04-20.md",
            item_rows=[(item_a.id, item_a.source)],
            included_at_utc="2026-04-20T10:00:00Z",
        )

    monkeypatch.setattr(vc, "assert_fresh", lambda min_count=400: 500)
    monkeypatch.setattr(vc, "max_similarity", lambda *args, **kwargs: 0.2)
    monkeypatch.setattr(ac, "build_active_references", lambda *args, **kwargs: [])
    monkeypatch.setattr(ac, "max_similarity_to_active", lambda vec, refs: (0.0, None))
    monkeypatch.setattr(
        emb,
        "embed_queries",
        lambda texts: np.zeros((len(list(texts)), 4), dtype=np.float32),
    )

    ids = {
        item["item_id"]
        for item in select_digest_candidates_v2(
            db_conn,
            date_local=date(2026, 4, 21),
            top_k=10,
            now_utc="2026-04-21T11:00:00Z",
            candidate_pool=10,
            summarize=False,
            live_cooldown_days=1,
        )
    }

    assert "rss:item-a" not in ids
    assert "rss:item-b" in ids


def test_reviewed_items_are_excluded_from_digest_even_after_cooldown(db_conn):
    item_a = make_item(
        item_id="rss:item-a",
        url="https://example.com/a",
        ts="2026-04-20T08:00:00Z",
    )
    item_b = make_item(
        item_id="rss:item-b",
        url="https://example.com/b",
        ts="2026-04-20T09:00:00Z",
    )
    with db_conn:
        upsert_item(db_conn, item_a, adapter_name="rss_briefing", source_payload_json="{}")
        upsert_item(db_conn, item_b, adapter_name="rss_briefing", source_payload_json="{}")
        record_feedback_event(
            db_conn,
            FeedbackEvent(
                event_id=compute_feedback_event_id(
                    origin="cli",
                    item_id=item_a.id,
                    action="useful",
                    event_ts_utc_iso="2026-04-21T00:00:00Z",
                ),
                item_id=item_a.id,
                action="useful",
                origin="cli",
                event_ts="2026-04-21T00:00:00Z",
            ),
        )

    ids = {
        item["item_id"]
        for item in select_digest_candidates(
            db_conn,
            date_local=date(2026, 4, 27),
            top_k=10,
            now_utc="2026-04-27T11:00:00Z",
            live_cooldown_days=1,
        )
    }

    assert "rss:item-a" not in ids
    assert "rss:item-b" in ids
