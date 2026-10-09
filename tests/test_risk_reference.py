"""risk_reference.load_reference: 离线官方参考资料块(设计规格第二节第 3 项)。

fixture `tests/fixtures/risk_ref/` 是从真实 `ref/` 拷的一小段(逐字未改):
`official/{thailand,southkorea,unitedstates}.md` 各含 危険レベル / 犯罪発生状況 /
FCDO Safety and security 三个节, `official/district_time_lines.md` 含泰/韩/美/新的行,
`hotspots/hotspots_nyc.json` 是纽约前 10 条辖区×时段统计。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from personal_intel_loop import DATA_DIR
from personal_intel_loop import risk_reference
from personal_intel_loop.risk_reference import DEFAULT_MAX_CHARS, load_home_reference, load_reference

FIXTURE_REF = Path(__file__).parent / "fixtures" / "risk_ref"


def test_fixture_ref_dir_layout():
    """fixture 目录结构要跟真实 ref/ 一致, 否则测的不是同一套东西。"""
    assert (FIXTURE_REF / "official" / "thailand.md").is_file()
    assert (FIXTURE_REF / "official" / "district_time_lines.md").is_file()
    assert (FIXTURE_REF / "hotspots" / "hotspots_nyc.json").is_file()


# --------------------------------------------------------------------------- #
# 目录/文件缺失 → 空串
# --------------------------------------------------------------------------- #

def test_missing_ref_dir_returns_empty_string(tmp_path):
    assert load_reference("thailand", "bangkok", ref_dir=tmp_path / "nope") == ""


def test_empty_ref_dir_returns_empty_string(tmp_path):
    assert load_reference("thailand", "bangkok", ref_dir=tmp_path) == ""


def test_missing_country_file_returns_empty_string(tmp_path):
    (tmp_path / "official").mkdir()
    assert load_reference("thailand", "bangkok", ref_dir=tmp_path) == ""


def test_unknown_country_returns_empty_string():
    assert load_reference("atlantis", "bangkok", ref_dir=FIXTURE_REF) == ""


def test_none_country_returns_empty_string():
    assert load_reference(None, "bangkok", ref_dir=FIXTURE_REF) == ""


def test_corrupt_hotspots_file_is_skipped_not_raised(tmp_path):
    root = tmp_path / "ref"
    (root / "official").mkdir(parents=True)
    (root / "hotspots").mkdir(parents=True)
    (root / "official" / "unitedstates.md").write_text(
        "# x\n### 1. 危険レベル（当前）\n- 来源 URL: https://example.com/a\n\n原文:\n\nAAA\n",
        encoding="utf-8",
    )
    (root / "hotspots" / "hotspots_nyc.json").write_text("{ broken", encoding="utf-8")
    out = load_reference("unitedstates", "newyork", ref_dir=root)
    assert "AAA" in out
    assert "辖区×时段" not in out


def test_no_hotspots_file_for_city_is_fine():
    """曼谷没有 hotspots 文件, 参考块照样出, 只是没有统计那节。"""
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    assert out
    assert "辖区×时段" not in out


# --------------------------------------------------------------------------- #
# 内容与来源
# --------------------------------------------------------------------------- #

def test_thailand_bangkok_block_has_all_expected_sections():
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    assert "thailand" in out and "bangkok" in out
    assert "district_time_lines.md" in out, "本城/本国的街区时段行应在最前"
    assert "危険レベル" in out
    assert "犯罪発生状況、防犯対策" in out
    assert "Safety and security" in out


def test_every_block_carries_source_url_and_date():
    """每节都要带来源 URL 与日期 —— 提示词要求模型引用时注明机构与日期。"""
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    assert out.count("来源 URL:") >= 3
    assert "官方标注更新日期" in out or "抓取时间" in out
    assert "摘录日期(ISO): 2026" in out, "要有可机读的 ISO 摘录日期"


def test_verbatim_source_sentence_survives_extraction():
    """逐字原文不能被改写: 挑一句 fixture 里确实有的原句断言整句在。"""
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    assert "バンコクでは、特に旅行者が集まる観光スポットや繁華街" in out


def test_verbatim_fcdo_english_survives():
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    assert "Attacks are most common during full moon parties" in out


def test_district_lines_prefer_city_rows_first():
    """本城(曼谷)的行排在别城(清迈)与"タイ国内"行前面。"""
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    head = out[: out.index("### 1. 危険レベル")]
    bangkok_at = head.index("バンコクでは")
    chiangmai_at = head.index("チェンマイ市内でも")
    nationwide_at = head.index("[タイ国内／")
    assert bangkok_at < chiangmai_at < nationwide_at, "本城行 → 别城行 → 国家级行"


def test_district_lines_for_other_city_are_included_after():
    """只给曼谷时清迈的行仍会出现(同国, 只是排在本城之后)—— 漏掉官方原文才是损失。"""
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    assert "チェンマイ市内でも" in out


def test_district_lines_for_other_country_excluded():
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    district = out[: out.index("### 1. 危険レベル")]
    assert "首席" not in district and "/springfield" not in district
    # 韩国的行(ソウル/仁川)不该出现在泰国块里
    assert "仁川広域市" not in district
    assert " gang" not in district


def test_country_only_still_returns_block():
    out = load_reference("thailand", None, ref_dir=FIXTURE_REF)
    assert "thailand" in out
    assert "危険レベル" in out


def test_south_korea_block():
    out = load_reference("southkorea", "seoul", ref_dir=FIXTURE_REF)
    assert "犯罪発生状況" in out
    assert "Safety and security" in out
    assert "thailand" not in out


# --------------------------------------------------------------------------- #
# hotspots
# --------------------------------------------------------------------------- #

def test_nyc_hotspots_top10_included():
    out = load_reference("unitedstates", "newyork", ref_dir=FIXTURE_REF)
    assert "辖区×时段犯罪统计" in out
    assert "NYPD / NYC Open Data" in out, "要带发布机构"
    assert "data.cityofnewyork.us" in out, "要带来源 URL"
    assert "2026-04-02 ~ 2026-06-30" in out, "要带统计窗口"
    assert out.count(" 件（") == risk_reference.HOTSPOT_LIMIT


def test_nyc_hotspots_rows_match_source_json():
    data = json.loads((FIXTURE_REF / "hotspots" / "hotspots_nyc.json").read_text("utf-8"))
    out = load_reference("unitedstates", "newyork", ref_dir=FIXTURE_REF)
    first = data["hotspots"][0]
    band = first.get("hour_band_zh") or first.get("hour_band")
    assert f"{first['count']} 件" in out
    assert str(band) in out


def test_hotspots_limit_is_ten():
    assert risk_reference.HOTSPOT_LIMIT == 10


def test_hotspots_skipped_when_no_hotspot_mapping():
    # 首尔在表里没有 hotspot 段名 → 不该去找 hotspots_seoul.json
    out = load_reference("southkorea", "seoul", ref_dir=FIXTURE_REF)
    assert "辖区×时段" not in out


# --------------------------------------------------------------------------- #
# 长度上限
# --------------------------------------------------------------------------- #

def test_default_max_chars_is_9000():
    """设计规格第二节第 1 项: 四类新块加进来后, 总长上限缺省从 6000 提到 9000。"""
    assert DEFAULT_MAX_CHARS == 9000


def test_respects_max_chars():
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF, max_chars=1200)
    assert len(out) <= 1200


def test_max_chars_truncates_marks_and_keeps_header():
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF, max_chars=900)
    assert len(out) <= 900
    assert out.startswith("# 官方参考资料摘录")
    assert "已截断" in out


def test_max_chars_zero_returns_only_header():
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF, max_chars=0)
    assert out.startswith("# 官方参考资料摘录")
    assert "危険レベル" not in out


def test_generous_max_chars_keeps_everything():
    """上限放宽 → 总长上限不再截块(节内 SECTION_MAX_CHARS 的截断是另一层, 仍在)。"""
    default = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    big = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF, max_chars=200000)
    assert len(big) > len(default)
    assert "本块超出总长上限" not in big
    assert "危険レベル" in big and "Safety and security" in big


def test_truncation_does_not_drop_lower_priority_block_entirely():
    """超长时按块内截断, 不是整块随机丢 —— 低优先级的 FCDO 也要留个位置。"""
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF, max_chars=1400)
    assert "district_time_lines" in out, "最高优先级的街区时段行必须保留"
    assert "危険レベル" in out


# --------------------------------------------------------------------------- #
# 缺省 ref_dir
# --------------------------------------------------------------------------- #

def test_default_ref_dir_is_under_data_dir():
    assert risk_reference.default_ref_dir() == DATA_DIR / "risk_reference"
    assert risk_reference.default_ref_dir().name == "risk_reference"


def test_default_ref_dir_missing_returns_empty_string():
    """生产上 DATA_DIR/risk_reference 还没落地时必须空串而不是抛。"""
    if risk_reference.default_ref_dir().is_dir():
        pytest.skip("DATA_DIR/risk_reference 已存在, 跳过缺目录用例")
    assert load_reference("thailand", "bangkok") == ""


def test_load_reference_accepts_str_ref_dir():
    out = load_reference("thailand", "bangkok", ref_dir=str(FIXTURE_REF))
    assert "危険レベル" in out


# =========================================================================== #
# 设计规格第二节第 1/2 项：健康 / 美国国务院 / 气候 / 常驻地
#
# fixture `tests/fixtures/risk_ref/{health,climate,advisory_us,home}/` 是从真实
# `ref/` 逐行截取的小段(逐字未改)：health/ 有 thailand.md 与 south-korea.md(文件名
# 带连字符, 用来测映射)、climate/ 有 travel_lines/sea_enso_impacts/enso_status,
# advisory_us/ 有 Thailand 与 South Korea 两节, home/ 是两张 action_lines 表。
# =========================================================================== #

def test_fixture_ref_dir_has_new_subdirs():
    for sub in ("health", "climate", "advisory_us", "home"):
        assert (FIXTURE_REF / sub).is_dir(), f"fixture 缺 {sub}/"


# --------------------------------------------------------------------------- #
# 健康：文件名映射
# --------------------------------------------------------------------------- #

def test_health_filename_map_covers_hyphenated_names():
    """`ref/health/` 用 `south-korea.md`, 别名 key 是 `southkorea` —— 靠常量表映射。"""
    assert risk_reference.HEALTH_FILENAME_BY_COUNTRY["southkorea"] == "south-korea.md"
    assert risk_reference.HEALTH_FILENAME_BY_COUNTRY["unitedstates"] == "united-states.md"
    assert risk_reference.HEALTH_FILENAME_BY_COUNTRY["unitedkingdom"] == "united-kingdom.md"


def test_health_file_resolves_mapped_and_plain_names():
    """映射表里的走连字符名, 表里没有的(thailand)直接拼 `<key>.md`。"""
    mapped = risk_reference._health_file(FIXTURE_REF, "southkorea")
    assert mapped is not None and mapped.name == "south-korea.md"
    plain = risk_reference._health_file(FIXTURE_REF, "thailand")
    assert plain is not None and plain.name == "thailand.md"


def test_health_file_missing_country_returns_none():
    assert risk_reference._health_file(FIXTURE_REF, "atlantis") is None
    assert risk_reference._health_file(FIXTURE_REF, None) is None


def test_health_block_appears_for_hyphenated_country_key():
    """country_key 写连字符找不到文件, 但别名 key(southkorea)要能出健康块。"""
    out = load_reference("southkorea", "seoul", ref_dir=FIXTURE_REF)
    assert "健康风险原文" in out
    assert "health/south-korea.md" in out, "块头要写明实际读了哪个文件"


def test_health_block_verbatim_notice_sentence_survives():
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    assert "Be aware of current health issues in Thailand." in out


def test_health_block_carries_source_url_and_date():
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    assert "wwwnc.cdc.gov/travel/destinations/traveler/none/thailand" in out
    assert "Page last updated: August 25, 2026" in out


def test_health_block_not_included_for_country_without_health_file(tmp_path):
    (tmp_path / "official").mkdir()
    (tmp_path / "official" / "thailand.md").write_text(
        "# x\n### 1. 危険レベル（当前）\n- 来源 URL: https://example.com/a\n\n原文:\n\nAAA\n",
        encoding="utf-8",
    )
    out = load_reference("thailand", "bangkok", ref_dir=tmp_path)
    assert "AAA" in out
    assert "健康风险原文" not in out


# --------------------------------------------------------------------------- #
# 美国国务院：按国切节
# --------------------------------------------------------------------------- #

def test_advisory_us_block_includes_level_and_publish_date():
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    assert "美国国务院 Travel Advisory（Thailand）" in out
    assert "Level 1: Exercise Normal Precautions" in out
    assert "pubDate：Tue, 07 Jul 2026" in out
    assert "traveladvisories/thailand-travel-advisory.html" in out


def test_advisory_us_region_restriction_sentences_come_first():
    """这个节的价值在「哪一带要额外小心」, 地区限制句必须排在通用准备事项之前。"""
    out = load_reference(
        "thailand", "bangkok", ref_dir=FIXTURE_REF, advisory_us_max_chars=4000, max_chars=60000
    )
    seg = out[out.index("### 美国国务院 Travel Advisory（Thailand）"):]
    seg = seg[: seg.index("\n## ")] if "\n## " in seg else seg
    caution = seg.index("Exercise Increased Caution near the Thai-Cambodian border")
    generic = seg.index("Enroll in the Smart Traveler Enrollment Program")
    assert caution < generic, "地区限制句要排在通用的行前准备事项之前"


def test_advisory_us_south_korea_section_only():
    """按国切节: 韩国块里不该出现泰国的原文, 反之亦然。"""
    out = load_reference("southkorea", "seoul", ref_dir=FIXTURE_REF)
    assert "美国国务院 Travel Advisory（South Korea）" in out
    assert "Thai-Cambodian border" not in out


def test_advisory_us_unknown_country_absent():
    assert "美国国务院" not in load_reference("unitedstates", "newyork", ref_dir=FIXTURE_REF)


def test_advisory_us_sections_splits_on_level_two_headings():
    secs = risk_reference._advisory_us_sections(
        (FIXTURE_REF / "advisory_us" / "us_state_advisories.md").read_text("utf-8")
    )
    titles = [t for t, _ in secs]
    assert "Thailand" in titles and "South Korea" in titles
    assert "台湾" not in titles, "只按 `## ` 切, 别把 `### ` 当成一国"


def test_advisory_us_missing_file_returns_empty(tmp_path):
    (tmp_path / "official").mkdir()
    assert risk_reference._advisory_us_block(tmp_path, "thailand", "bangkok", None, 1600) == ""


# --------------------------------------------------------------------------- #
# 气候：travel_lines / sea_enso_impacts / enso_status
# --------------------------------------------------------------------------- #

def test_climate_travel_lines_include_country_row_with_url_and_date():
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    assert "厄尔尼诺/气候对旅行的官方原文行" in out
    assert "tmd.go.th" in out, "每行要带实际来源 URL"
    assert "2026-09-22" in out, "每行要带日期"


def test_climate_travel_lines_exclude_other_country_rows():
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    assert "pagasa.dost.gov.ph" not in out, "菲律宾的行不该进泰国块"


def test_climate_sea_enso_picks_country_section():
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    assert "本次厄尔尼诺对该国的影响" in out
    assert "2.1 ENSO 月报" in out


def test_climate_sea_enso_section_scoped_to_country():
    """印尼的 BMKG 节不该进泰国块。"""
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF)
    assert "5.1 新闻稿《BMKG" not in out
    indo = load_reference("indonesia", "jakarta", ref_dir=FIXTURE_REF)
    assert "5.1 新闻稿《BMKG" in indo


def test_enso_status_attached_for_every_country():
    """ENSO 当期状态是全球背景, 泰/韩/美三个块都要有 NOAA 与 JMA 两节。"""
    for country, city in (("thailand", "bangkok"), ("southkorea", "seoul"), ("unitedstates", "newyork")):
        out = load_reference(country, city, ref_dir=FIXTURE_REF)
        assert "当期 ENSO 状态" in out, country
        assert "ENSO Alert System Status" in out, country
        assert "cpc.ncep.noaa.gov" in out, f"{country} 的 ENSO 块要带 NOAA 来源 URL"


def test_enso_status_only_noaa_and_jma_sections():
    """只取 NOAA/JMA 两节 —— WMO/BOM/NCC 那些不进参考块。"""
    # 总长放宽, 让 ENSO 块拿到它自己那份上限(默认 9000 下它会被总长截短)。
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF, max_chars=60000)
    assert "NOAA CPC" in out
    assert "日本気象庁" in out
    assert "wmo.int" not in out, "设计规格只要 NOAA 与 JMA"
    assert "Bureau of Meteorology" not in out
    assert "国家气候中心" not in out


def test_enso_status_omitted_for_unknown_country():
    assert "当期 ENSO 状态" not in load_reference("atlantis", "bangkok", ref_dir=FIXTURE_REF)


def test_climate_blocks_missing_dir_return_empty(tmp_path):
    (tmp_path / "official").mkdir()
    assert risk_reference._travel_lines_block(tmp_path, "thailand", None, 1800) == ""
    assert risk_reference._sea_enso_block(tmp_path, "thailand", None, 1800) == ""
    assert risk_reference._enso_status_block(tmp_path, 1400) == ""


# --------------------------------------------------------------------------- #
# 各块字数上限（参数化）与优先级截断
# --------------------------------------------------------------------------- #

def test_default_max_chars_is_9000_after_new_blocks():
    """四类新块加进来后总长上限缺省 9000(原来 6000)。"""
    assert DEFAULT_MAX_CHARS == 9000


def test_per_block_limits_are_parameters():
    assert risk_reference.HEALTH_MAX_CHARS == 2000
    assert risk_reference.ADVISORY_US_MAX_CHARS == 1600
    assert risk_reference.CLIMATE_MAX_CHARS == 1800
    assert risk_reference.ENSO_STATUS_MAX_CHARS == 1400


def test_health_max_chars_parameter_shrinks_health_block():
    """把健康块上限调小 → 该块正文变短, 但来源 URL 与日期行必须还在。"""
    big = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF, health_max_chars=2000, max_chars=60000)
    small = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF, health_max_chars=240, max_chars=60000)
    assert len(small) < len(big)
    assert "wwwnc.cdc.gov/travel/destinations/traveler/none/thailand" in small
    assert "Page last updated: August 25, 2026" in small


def test_advisory_us_max_chars_parameter_truncates():
    out = load_reference(
        "thailand", "bangkok", ref_dir=FIXTURE_REF, advisory_us_max_chars=200, max_chars=60000
    )
    seg = out[out.index("### 美国国务院 Travel Advisory（Thailand）"):]
    assert "已截断" in seg or "已省略" in seg
    assert "traveladvisories/thailand-travel-advisory.html" in seg, "截断后仍要带来源 URL"


def test_enso_status_max_chars_parameter_truncates():
    out = load_reference(
        "thailand", "bangkok", ref_dir=FIXTURE_REF, enso_status_max_chars=120, max_chars=60000
    )
    assert "当期 ENSO 状态" in out
    assert "cpc.ncep.noaa.gov" in out


def test_truncation_breaks_on_paragraph_boundary_only():
    """截断只在段落边界: 参考块里每一行原文都必须逐字出现在源文件里, 不能被切掉半句。"""
    source = (FIXTURE_REF / "health" / "thailand.md").read_text("utf-8")
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF, health_max_chars=200, max_chars=60000)
    seg = out[out.index("## 健康风险原文"):]
    seg = seg[: seg.index("\n## ")] if "\n## " in seg else seg
    # 每节正文跟在 `原文:` 之后; 取最后一段 `原文:` 之后的全部正文行来核对。
    body = seg.rsplit("原文:\n\n", 1)[1] if "原文:\n\n" in seg else ""
    assert body.strip(), "截断后仍应留下正文"
    for line in (ln.strip() for ln in body.splitlines()):
        # `原文:`/`### …` 是我们加的抬头, `…（本节…已省略）` 是截断标注, 其余每行都必须是源文件里的原句。
        if not line or line.startswith(("#", "原文", "…")):
            continue
        assert line in source, f"该行不在源文件里, 说明截断切在半句: {line[-60:]!r}"


def test_priority_order_district_advisory_health_climate():
    """优先级阶梯: 街区时段行 > 美国国务院 > 健康 > 气候 > 官方国家文件节。"""
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF, max_chars=60000)
    at = lambda needle: out.index(needle)
    assert at("涉及具体街区・时段的官方原文行") < at("美国国务院 Travel Advisory")
    assert at("美国国务院 Travel Advisory") < at("健康风险原文")
    assert at("健康风险原文") < at("当期 ENSO 状态")
    assert at("当期 ENSO 状态") < at("### 1. 危険レベル")


def test_tight_budget_keeps_high_priority_blocks_first():
    """预算很紧时, 高优先级块仍在, 且街区时段行排在最前。"""
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF, max_chars=1500)
    assert out.startswith("# 官方参考资料摘录")
    assert "涉及具体街区・时段的官方原文行" in out
    assert "美国国务院 Travel Advisory" in out
    assert out.index("涉及具体街区") < out.index("美国国务院")


def test_priority_constants_are_ordered():
    assert (
        risk_reference.PRIORITY_DISTRICT
        < risk_reference.PRIORITY_ADVISORY_US
        < risk_reference.PRIORITY_HEALTH
        < risk_reference.PRIORITY_CLIMATE
        < risk_reference.PRIORITY_OFFICIAL
        < risk_reference.PRIORITY_HOTSPOT
    )


def test_respects_max_chars_with_all_new_blocks():
    out = load_reference("thailand", "bangkok", ref_dir=FIXTURE_REF, max_chars=2500)
    assert len(out) <= 2500


# --------------------------------------------------------------------------- #
# load_home_reference（不接入出版流程）
# --------------------------------------------------------------------------- #

def test_load_home_reference_default_is_3000():
    assert risk_reference.HOME_MAX_CHARS == 3000


def test_load_home_reference_reads_both_action_lines():
    out = load_home_reference(ref_dir=FIXTURE_REF)
    assert "home/action_lines.md" in out
    assert "home/district_action_lines.md" in out, "两份常驻地资料都要出现"


def test_load_home_reference_rows_carry_verbatim_quote_and_url():
    out = load_home_reference(ref_dir=FIXTURE_REF)
    assert "向こう３か月の気温は、暖かい空気に覆われやすく" in out
    assert "data.jma.go.jp/cpd/longfcst/kaisetsu/data/P3M/json/comment_010300.json" in out
    assert "発表 2026-09-18" in out


def test_load_home_reference_skips_table_header_row():
    """表头行不是原文, 不能变成「(来源 URL: URL)」这种占位行。"""
    out = load_home_reference(ref_dir=FIXTURE_REF)
    assert "来源 URL: URL" not in out
    assert "来源 URL: ---" not in out


def test_load_home_reference_respects_max_chars():
    out = load_home_reference(ref_dir=FIXTURE_REF, max_chars=900)
    assert len(out) <= 900
    assert out.startswith("# 常驻地官方参考资料摘录")


def test_load_home_reference_missing_dir_returns_empty(tmp_path):
    assert load_home_reference(ref_dir=tmp_path / "nope") == ""
    assert load_home_reference(ref_dir=tmp_path) == ""


def test_load_home_reference_dir_without_home_returns_empty(tmp_path):
    (tmp_path / "official").mkdir()
    assert load_home_reference(ref_dir=tmp_path) == ""


def test_load_home_reference_accepts_str_ref_dir():
    assert load_home_reference(ref_dir=str(FIXTURE_REF))


def test_load_home_reference_is_not_wired_into_paper_risk():
    """本任务只实现函数与测试, 不接入出版流程 —— paper_risk 不该调用它。"""
    from personal_intel_loop import PROJECT_ROOT, paper_risk

    assert not hasattr(paper_risk, "load_home_reference")
    src = (PROJECT_ROOT / "src" / "personal_intel_loop" / "paper_risk.py").read_text("utf-8")
    assert "load_home_reference" not in src
