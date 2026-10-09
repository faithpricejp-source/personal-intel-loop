"""render_digest: 有断言时出现 ## 可检验断言 段与逐条断言行; 无断言时不出现。"""
from __future__ import annotations

from datetime import date

from personal_intel_loop.digest import RANKING_V2, render_digest


def _cand(**over):
    base = {
        "item_id": "rss:c1",
        "source": "rss_briefing:dwarkesh_podcast",
        "title": "Can distillation be stopped",
        "trust_score": 0.35,
        "ts_utc": "2026-08-26T00:00:00Z",
        "tags": [],
        "url": "https://example.com/a",
        "summary": "summary text",
        "media_manifest_relpath": None,
        "tier": "longform",
        "score_v2": 0.5,
    }
    base.update(over)
    return base


def test_claim_section_and_item_line_rendered():
    cand = _cand(claim="开源 6 个月内追平前沿", claim_check_after="2026-10-15", claim_id="abc123")
    md = render_digest(date_local=date(2026, 8, 26), candidates=[cand], ranking_version=RANKING_V2)
    assert "## 可检验断言" in md
    assert "<!-- pil_claim=abc123 -->" in md
    assert "(核验起点: 2026-10-15)" in md
    assert "- 可检验断言: 开源 6 个月内追平前沿" in md
    # 新原因码 checkbox 到位, 旧三档不再渲染
    for action in ("already_known", "unclear", "keep", "deep_discuss"):
        assert f"pil_action={action}" in md
    assert "pil_action=useful" not in md and "pil_action=less_like_this" not in md


def test_no_claim_no_section():
    md = render_digest(date_local=date(2026, 8, 26), candidates=[_cand()], ranking_version=RANKING_V2)
    assert "## 可检验断言" not in md and "- 可检验断言:" not in md


def test_profile_line_rendered_from_llm_summary():
    cand = _cand(llm_summary={"one_liner": "s", "why_for_you": "w", "topic": "AI与模型",
                              "profile_hit": "AI 产业经济: 饱和", "lane": "material", "saturated": True})
    md = render_digest(date_local=date(2026, 8, 26), candidates=[cand], ranking_version=RANKING_V2)
    assert "- profile: [外脑原料] 命中「AI 产业经济: 饱和」 · 饱和域" in md
    assert "- profile: reading_profile.md" in md or "- profile: (缺失" in md
