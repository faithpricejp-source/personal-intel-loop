from __future__ import annotations

from personal_intel_loop.ranking import score_item_v1, sort_key_for_item


def test_higher_trust_outranks_lower_trust_at_equal_age():
    now_utc = "2026-04-20T00:00:00Z"
    high = score_item_v1(source_trust=0.8, ts_utc="2026-04-19T00:00:00Z", now_utc=now_utc)
    low = score_item_v1(source_trust=0.2, ts_utc="2026-04-19T00:00:00Z", now_utc=now_utc)
    assert high > low


def test_fresher_item_outranks_older_item_at_equal_trust():
    now_utc = "2026-04-20T00:00:00Z"
    fresh = score_item_v1(source_trust=0.4, ts_utc="2026-04-19T20:00:00Z", now_utc=now_utc)
    old = score_item_v1(source_trust=0.4, ts_utc="2026-04-18T00:00:00Z", now_utc=now_utc)
    assert fresh > old


def test_deterministic_ordering_tie_break():
    now_utc = "2026-04-20T00:00:00Z"
    a = sort_key_for_item(item_id="a", source_trust=0.4, ts_utc="2026-04-19T00:00:00Z", now_utc=now_utc)
    b = sort_key_for_item(item_id="b", source_trust=0.4, ts_utc="2026-04-19T00:00:00Z", now_utc=now_utc)
    assert a < b
