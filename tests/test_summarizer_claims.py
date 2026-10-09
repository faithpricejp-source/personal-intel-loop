"""摘要器 JSON → ItemSummary 的 checkable_claim 解析。不调 LLM。"""
from __future__ import annotations

from personal_intel_loop.summarizer import PROMPT_TEMPLATE, _coerce_summary

BASE = {"one_liner": "x", "why_for_you": "y", "is_noise": False, "noise_category": None, "topic": "AI与模型"}


def test_prompt_asks_for_claim_fields():
    assert "checkable_claim" in PROMPT_TEMPLATE and "claim_check_after" in PROMPT_TEMPLATE


def test_claim_and_date_parsed():
    s = _coerce_summary({**BASE, "checkable_claim": " 开源 6 个月内追平前沿 ", "claim_check_after": "2026-10-15"})
    assert s.checkable_claim == "开源 6 个月内追平前沿"
    assert s.claim_check_after == "2026-10-15"


def test_missing_claim_is_none():
    s = _coerce_summary(BASE)
    assert s.checkable_claim is None and s.claim_check_after is None
    s2 = _coerce_summary({**BASE, "checkable_claim": None, "claim_check_after": None})
    assert s2.checkable_claim is None


def test_malformed_date_dropped_and_claim_capped():
    s = _coerce_summary({**BASE, "checkable_claim": "a" * 500, "claim_check_after": "Q4 2026"})
    assert len(s.checkable_claim) == 300
    assert s.claim_check_after is None
