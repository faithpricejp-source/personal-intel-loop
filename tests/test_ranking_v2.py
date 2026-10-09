from __future__ import annotations

import pytest

from personal_intel_loop.ranking import (
    ACTIVE_OBJECT_TYPES,
    ENRICH_OBJECT_TYPES,
    ItemSignalsV2,
    WEIGHTS_BACKLOG_V2,
    WEIGHTS_V2,
    score_backlog_item_v2,
    score_item_v2,
    sort_key_for_backlog_item_v2,
    sort_key_for_item_v2,
)


def _sig(**kwargs) -> ItemSignalsV2:
    defaults = dict(
        novelty=0.5,
        active_relevance=0.5,
        enrich_score=0.5,
        source_trust=0.5,
        ts_utc="2026-04-20T00:00:00Z",
    )
    defaults.update(kwargs)
    return ItemSignalsV2(**defaults)


NOW = "2026-04-20T00:00:00Z"


def test_all_zero_signals_gives_zero():
    s = _sig(novelty=0, active_relevance=0, enrich_score=0, source_trust=0)
    assert score_item_v2(signals=s, now_utc=NOW) == pytest.approx(0.0, abs=1e-6)


def test_all_max_signals_and_fresh_gives_sum_of_positive_weights():
    s = _sig(novelty=1, active_relevance=1, enrich_score=1, source_trust=1, ts_utc=NOW)
    expected = WEIGHTS_V2["novelty"] + WEIGHTS_V2["active"] + WEIGHTS_V2["enrich"] + WEIGHTS_V2["trust"]
    assert score_item_v2(signals=s, now_utc=NOW) == pytest.approx(expected)


def test_stale_item_incurs_age_penalty_capped_at_one_week():
    s_fresh = _sig(novelty=0, active_relevance=0, enrich_score=0, source_trust=0, ts_utc=NOW)
    s_week_old = _sig(novelty=0, active_relevance=0, enrich_score=0, source_trust=0, ts_utc="2026-04-13T00:00:00Z")
    s_month_old = _sig(novelty=0, active_relevance=0, enrich_score=0, source_trust=0, ts_utc="2026-03-20T00:00:00Z")
    fresh = score_item_v2(signals=s_fresh, now_utc=NOW)
    week = score_item_v2(signals=s_week_old, now_utc=NOW)
    month = score_item_v2(signals=s_month_old, now_utc=NOW)
    assert fresh == pytest.approx(0.0)
    assert week == pytest.approx(-WEIGHTS_V2["age"])
    assert month == pytest.approx(-WEIGHTS_V2["age"])


def test_high_novelty_outranks_high_trust_at_equal_age():
    novel = _sig(novelty=0.9, active_relevance=0.1, enrich_score=0.1, source_trust=0.0, ts_utc=NOW)
    trusted = _sig(novelty=0.1, active_relevance=0.1, enrich_score=0.1, source_trust=1.0, ts_utc=NOW)
    assert score_item_v2(signals=novel, now_utc=NOW) > score_item_v2(signals=trusted, now_utc=NOW)


def test_active_relevance_beats_enrich_at_equal_novelty():
    active_heavy = _sig(novelty=0.4, active_relevance=0.9, enrich_score=0.1)
    enrich_heavy = _sig(novelty=0.4, active_relevance=0.1, enrich_score=0.9)
    assert score_item_v2(signals=active_heavy, now_utc=NOW) > score_item_v2(signals=enrich_heavy, now_utc=NOW)


def test_nan_clamp_to_zero():
    s = _sig(novelty=float("nan"), active_relevance=0.5, enrich_score=0.5, source_trust=0.5)
    assert score_item_v2(signals=s, now_utc=NOW) == pytest.approx(
        WEIGHTS_V2["active"] * 0.5 + WEIGHTS_V2["enrich"] * 0.5 + WEIGHTS_V2["trust"] * 0.5
    )


def test_out_of_range_inputs_are_clamped():
    s = _sig(novelty=2.0, active_relevance=-0.5, enrich_score=1.5, source_trust=-0.1)
    score = score_item_v2(signals=s, now_utc=NOW)
    expected = WEIGHTS_V2["novelty"] * 1.0 + WEIGHTS_V2["enrich"] * 1.0
    assert score == pytest.approx(expected)


def test_sort_key_breaks_ties_by_recency_then_item_id():
    fresh = _sig(novelty=0.5, ts_utc="2026-04-19T12:00:00Z")
    older = _sig(novelty=0.5, ts_utc="2026-04-18T12:00:00Z")
    k_fresh = sort_key_for_item_v2(item_id="b", signals=fresh, now_utc=NOW)
    k_older = sort_key_for_item_v2(item_id="a", signals=older, now_utc=NOW)
    # fresh should sort first despite 'b' > 'a' lexicographically
    assert k_fresh < k_older


def test_backlog_score_drops_age_penalty_entirely():
    fresh = _sig(novelty=0.5, active_relevance=0.5, enrich_score=0.5, source_trust=0.5, ts_utc=NOW)
    old = _sig(
        novelty=0.5,
        active_relevance=0.5,
        enrich_score=0.5,
        source_trust=0.5,
        ts_utc="2024-01-01T00:00:00Z",
    )
    expected = (
        WEIGHTS_BACKLOG_V2["novelty"] * 0.5
        + WEIGHTS_BACKLOG_V2["active"] * 0.5
        + WEIGHTS_BACKLOG_V2["enrich"] * 0.5
        + WEIGHTS_BACKLOG_V2["trust"] * 0.5
    )
    assert score_backlog_item_v2(signals=fresh) == pytest.approx(expected)
    assert score_backlog_item_v2(signals=old) == pytest.approx(expected)


def test_backlog_sort_still_uses_recency_only_as_tiebreak():
    fresh = _sig(novelty=0.5, ts_utc="2026-04-19T12:00:00Z")
    older = _sig(novelty=0.5, ts_utc="2026-04-18T12:00:00Z")
    k_fresh = sort_key_for_backlog_item_v2(item_id="b", signals=fresh)
    k_older = sort_key_for_backlog_item_v2(item_id="a", signals=older)
    assert k_fresh < k_older


def test_enum_constants_match_vault():
    assert set(ENRICH_OBJECT_TYPES) == {"mechanism", "heuristic", "diagnosis", "diagnostic"}
    assert set(ACTIVE_OBJECT_TYPES) == {"judgment", "monitoring", "decision"}
