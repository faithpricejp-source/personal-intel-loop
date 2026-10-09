from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

WEIGHTS_V2 = {
    "novelty": 0.35,
    "active": 0.30,
    "enrich": 0.20,
    "trust": 0.10,
    "age": 0.05,
}
WEIGHTS_BACKLOG_V2 = {
    "novelty": 0.35,
    "active": 0.30,
    "enrich": 0.20,
    "trust": 0.10,
}
AGE_PENALTY_SATURATION_HOURS = 168.0

ENRICH_OBJECT_TYPES = ("mechanism", "heuristic", "diagnosis", "diagnostic")
ACTIVE_OBJECT_TYPES = ("judgment", "monitoring", "decision")

# Tier classification:
# different lengths serve different reading modes (morning pulse scan vs deep read).
# Classify by actual body char count, not source — weibo has long posts, xhs has short ones.
TIER_LENGTH_THRESHOLD = 500
TIER_PULSE = "pulse"
TIER_LONGFORM = "longform"


def classify_tier(body: str | None, threshold: int = TIER_LENGTH_THRESHOLD) -> str:
    """Split candidates into pulse (short) vs longform (long) by actual body length.

    Content with empty / missing body falls into pulse (nothing to read anyway —
    title-only items). Threshold is character count, defaults to 500.
    """
    if not body:
        return TIER_PULSE
    return TIER_LONGFORM if len(body) >= threshold else TIER_PULSE


def _parse_utc(ts_utc: str) -> datetime:
    raw = ts_utc.replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("ts_utc must be timezone-aware")
    return dt.astimezone(timezone.utc)


def _age_hours(ts_utc: str, now_utc: str) -> float:
    age_seconds = max(0.0, (_parse_utc(now_utc) - _parse_utc(ts_utc)).total_seconds())
    return age_seconds / 3600.0


def recency_bonus(ts_utc: str, *, now_utc: str) -> float:
    return math.exp(-_age_hours(ts_utc, now_utc) / 72.0)


def score_item_v1(*, source_trust: float, ts_utc: str, now_utc: str) -> float:
    return 0.75 * float(source_trust) + 0.25 * recency_bonus(ts_utc, now_utc=now_utc)


def sort_key_for_item(*, item_id: str, source_trust: float, ts_utc: str, now_utc: str) -> tuple[float, float, str]:
    score = score_item_v1(source_trust=source_trust, ts_utc=ts_utc, now_utc=now_utc)
    return (-score, -_parse_utc(ts_utc).timestamp(), item_id)


@dataclass
class ItemSignalsV2:
    novelty: float
    active_relevance: float
    enrich_score: float
    source_trust: float
    ts_utc: str


def _clamp01(value: float) -> float:
    if value != value:  # NaN guard
        return 0.0
    return max(0.0, min(1.0, float(value)))


def score_item_v2(*, signals: ItemSignalsV2, now_utc: str) -> float:
    novelty = _clamp01(signals.novelty)
    active = _clamp01(signals.active_relevance)
    enrich = _clamp01(signals.enrich_score)
    trust = _clamp01(signals.source_trust)
    age_hours = _age_hours(signals.ts_utc, now_utc)
    age_penalty = min(1.0, age_hours / AGE_PENALTY_SATURATION_HOURS)
    w = WEIGHTS_V2
    return (
        w["novelty"] * novelty
        + w["active"] * active
        + w["enrich"] * enrich
        + w["trust"] * trust
        - w["age"] * age_penalty
    )


def sort_key_for_item_v2(
    *,
    item_id: str,
    signals: ItemSignalsV2,
    now_utc: str,
) -> tuple[float, float, str]:
    score = score_item_v2(signals=signals, now_utc=now_utc)
    return (-score, -_parse_utc(signals.ts_utc).timestamp(), item_id)


def score_backlog_item_v2(*, signals: ItemSignalsV2) -> float:
    """Backlog ranking removes recency penalty for evergreen local corpora."""
    novelty = _clamp01(signals.novelty)
    active = _clamp01(signals.active_relevance)
    enrich = _clamp01(signals.enrich_score)
    trust = _clamp01(signals.source_trust)
    w = WEIGHTS_BACKLOG_V2
    return (
        w["novelty"] * novelty
        + w["active"] * active
        + w["enrich"] * enrich
        + w["trust"] * trust
    )


def sort_key_for_backlog_item_v2(
    *,
    item_id: str,
    signals: ItemSignalsV2,
) -> tuple[float, float, str]:
    score = score_backlog_item_v2(signals=signals)
    return (-score, -_parse_utc(signals.ts_utc).timestamp(), item_id)
