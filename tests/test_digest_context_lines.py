"""来龙去脉两行: 有则逐行渲染, 两者皆无则显式标"缺", 且 web 解析能取回。

用户 2026-08-26 定: 只报"发生了 A"、不讲 A 之前是什么状态、也不讲 A 往下改变什么的孤立新闻不要看。
"缺"必须可见而不是静默省略——否则没脉络的条目看起来和有脉络的一样。
"""
from __future__ import annotations

from datetime import date

from personal_intel_loop.digest import RANKING_V2, render_digest
from personal_intel_loop.web_digest import parse_digest


def _cand(**over):
    base = {
        "item_id": "rss:c1",
        "source": "rss_briefing:dwarkesh_podcast",
        "title": "HBM 挤占消费级内存产能",
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


def _llm(**over):
    base = {"one_liner": "s", "why_for_you": "w", "topic": "AI与模型",
            "profile_hit": "单位是因果链或可检验断言", "lane": "material", "saturated": False}
    base.update(over)
    return base


def test_context_lines_rendered_when_present():
    cand = _cand(llm_summary=_llm(backstory="此前 DDR5 现货价横盘 11 个月",
                                  so_what="消费级 DIY 与整机厂先吃涨价, 看下季度模组厂报价"))
    md = render_digest(date_local=date(2026, 8, 26), candidates=[cand], ranking_version=RANKING_V2)
    assert "- 来龙: 此前 DDR5 现货价横盘 11 个月" in md
    assert "- 去脉: 消费级 DIY 与整机厂先吃涨价, 看下季度模组厂报价" in md
    assert "- 来龙去脉: 缺" not in md


def test_missing_context_is_flagged_not_silently_dropped():
    md = render_digest(date_local=date(2026, 8, 26),
                       candidates=[_cand(llm_summary=_llm(backstory=None, so_what=None))],
                       ranking_version=RANKING_V2)
    assert "- 来龙去脉: 缺(只报了事件本体)" in md
    assert "- 来龙:" not in md and "- 去脉:" not in md


def test_one_sided_context_renders_only_that_side():
    md = render_digest(date_local=date(2026, 8, 26),
                       candidates=[_cand(llm_summary=_llm(backstory="此前禁令只覆盖 A100", so_what=None))],
                       ranking_version=RANKING_V2)
    assert "- 来龙: 此前禁令只覆盖 A100" in md
    assert "- 去脉:" not in md
    assert "- 来龙去脉: 缺" not in md


def test_web_digest_parses_context_fields():
    cand = _cand(llm_summary=_llm(backstory="此前 DDR5 现货价横盘 11 个月", so_what="模组厂下季报价"))
    md = render_digest(date_local=date(2026, 8, 26), candidates=[cand], ranking_version=RANKING_V2)
    view = parse_digest(md)
    assert view.items, "解析不到卡片"
    item = view.items[0]
    assert item["backstory"] == "此前 DDR5 现货价横盘 11 个月"
    assert item["so_what"] == "模组厂下季报价"
    assert item["context_missing"] is False


def test_web_digest_marks_missing_context():
    md = render_digest(date_local=date(2026, 8, 26),
                       candidates=[_cand(llm_summary=_llm(backstory=None, so_what=None))],
                       ranking_version=RANKING_V2)
    item = parse_digest(md).items[0]
    assert item["backstory"] is None and item["so_what"] is None
    assert item["context_missing"] is True
