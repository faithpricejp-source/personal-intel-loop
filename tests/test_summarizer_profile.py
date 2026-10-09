"""摘要器的 profile 条件与 profile_hit / lane / saturated 解析。不调 LLM。"""
from __future__ import annotations

from personal_intel_loop.summarizer import _coerce_summary, build_prompt

BASE = {"one_liner": "x", "why_for_you": "y", "is_noise": False, "noise_category": None, "topic": "AI与模型"}


def test_prompt_embeds_profile_text():
    p = build_prompt(title="t", source="s", body="b", novelty=0, active=0, enrich=0, trust=0,
                     active_target=None, enrich_target=None, profile="| AI 产业经济 | 饱和 |")
    assert "| AI 产业经济 | 饱和 |" in p and "profile_hit" in p and "saturated" in p


def test_prompt_without_profile_says_so():
    p = build_prompt(title="t", source="s", body="b", novelty=0, active=0, enrich=0, trust=0,
                     active_target=None, enrich_target=None)
    assert "(无 profile)" in p


def test_lane_and_saturated_parsed():
    s = _coerce_summary({**BASE, "profile_hit": " AI 产业经济: 饱和 ", "lane": "material", "saturated": True})
    assert s.profile_hit == "AI 产业经济: 饱和" and s.lane == "material" and s.saturated is True


def test_invalid_lane_falls_to_none_and_saturated_must_be_bool_true():
    s = _coerce_summary({**BASE, "lane": "whatever", "saturated": "true"})
    assert s.lane == "none" and s.saturated is False
    s2 = _coerce_summary(BASE)
    assert s2.profile_hit is None and s2.lane == "none" and s2.saturated is False


def test_warmth_lane_accepted():
    s = _coerce_summary({**BASE, "lane": "warmth", "saturated": False, "profile_hit": "人间温暖"})
    assert s.lane == "warmth" and s.saturated is False
