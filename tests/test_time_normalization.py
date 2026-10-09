from __future__ import annotations

from personal_intel_loop.ranking import recency_bonus
from personal_intel_loop.schemas import normalize_dt_to_utc_z


def test_normalize_dt_to_utc_z():
    assert normalize_dt_to_utc_z("2026-04-19T09:00:00+09:00") == "2026-04-19T00:00:00Z"


def test_age_based_ranking_uses_utc_normalized_values():
    now_utc = "2026-04-20T00:00:00Z"
    fresh = recency_bonus("2026-04-19T23:00:00Z", now_utc=now_utc)
    older = recency_bonus("2026-04-19T12:00:00Z", now_utc=now_utc)
    assert fresh > older
