"""place_aliases: 多语地名解析(设计规格第二节第 1 项)。

表是 `config/place_aliases.json`(真实配置, 不在测试里造): 断言的是「用户填的写法
能不能解析成正确的国家/城市 key, 以及别名集够不够喂给搜索」。
"""
from __future__ import annotations

import json

import pytest

from personal_intel_loop import place_aliases
from personal_intel_loop.place_aliases import (
    alias_path_for,
    expand,
    hotspot_city,
    load_table,
    normalize_term,
    reload,
    resolve,
)


@pytest.fixture(autouse=True)
def _fresh_table():
    reload()
    yield
    reload()


def test_alias_table_covers_required_countries_and_cities():
    """ref/official 的 7 国 + 美国四城 + 设计规格点名的其余国家, 别名表必须齐。"""
    table = load_table()
    keys = {str(c.get("key")) for c in table["countries"]}
    required = {
        # ref/official/ 里的 7 国
        "thailand", "unitedstates", "southkorea", "taiwan", "vietnam", "singapore", "china",
        # 设计规格点名的其余国家
        "japan", "malaysia", "indonesia", "philippines", "india", "unitedkingdom", "france",
        "germany", "italy", "mexico", "turkey", "egypt", "brazil",
    }
    assert required <= keys, f"别名表缺国家: {sorted(required - keys)}"

    by_key = {str(c.get("key")): c for c in table["countries"]}
    # 美国四城各自要能定位到 ref/hotspots 的段名
    for city, hotspot in (("newyork", "nyc"), ("losangeles", "la"), ("sanfrancisco", "sf"), ("chicago", "chicago")):
        assert city in by_key["unitedstates"]["cities"], f"别名表缺美国城市 {city}"
        assert by_key["unitedstates"]["cities"][city].get("hotspot") == hotspot


def test_every_country_has_three_languages_and_cities_have_hotspot_only_for_us():
    table = load_table()
    for entry in table["countries"]:
        key = entry["key"]
        for lang in ("zh", "ja", "en"):
            assert entry.get(lang), f"{key} 缺 {lang} 国名别名"
        for city_key, city in entry["cities"].items():
            assert city.get("zh") or city.get("ja") or city.get("en"), f"{key}/{city_key} 一个别名都没有"
            if city.get("hotspot"):
                assert key == "unitedstates", f"hotspot 只该登记在美国城市上, 却在 {key}/{city_key}"


def test_alias_table_file_is_valid_json_with_countries_list():
    raw = json.loads(alias_path_for().read_text("utf-8"))
    assert isinstance(raw["countries"], list) and raw["countries"]


@pytest.mark.parametrize(
    ("place", "country", "expect_country", "expect_city"),
    [
        ("曼谷", None, "thailand", "bangkok"),
        ("曼谷", "泰国", "thailand", "bangkok"),
        ("Bangkok", "泰国", "thailand", "bangkok"),
        ("バンコク", None, "thailand", "bangkok"),
        ("タイ", None, "thailand", None),
        ("Thailand", None, "thailand", None),
        ("泰国", None, "thailand", None),
        ("清迈", None, "thailand", "chiangmai"),
        ("Chiang Mai", "タイ", "thailand", "chiangmai"),
        ("チェンマイ", None, "thailand", "chiangmai"),
        ("纽约", None, "unitedstates", "newyork"),
        ("New York City", None, "unitedstates", "newyork"),
        ("New York", "美国", "unitedstates", "newyork"),
        ("纽约", None, "unitedstates", "newyork"),
        ("三藩市", None, "unitedstates", "sanfrancisco"),
        ("首尔", None, "southkorea", "seoul"),
        ("서울", None, None, None),  # 韩语不在表里: 不写不确定的别名
        ("胡志明市", None, "vietnam", "hochiminh"),
        ("ホーチミン", None, "vietnam", "hochiminh"),
        ("新加坡", None, "singapore", "singapore"),
        ("东京", None, "japan", "tokyo"),
        ("罗马", None, "italy", "rome"),
        ("伊斯坦布尔", None, "turkey", "istanbul"),
        ("开罗", None, "egypt", "cairo"),
        ("圣保罗", None, "brazil", "saopaulo"),
    ],
)
def test_expand_resolves_any_language_spelling(place, country, expect_country, expect_city):
    got = expand(place, country)
    assert got["country_key"] == expect_country
    assert got["city_key"] == expect_city


def test_city_name_alone_reverse_resolves_country():
    """城市名能反查国家 —— 用户只填「曼谷」不填国家时也要拿到泰国的全部别名。"""
    got = expand("曼谷")
    assert got["country_key"] == "thailand"
    terms = got["terms"]
    for expected in ("曼谷", "タイ", "Thailand", "バンコク", "泰国"):
        assert expected in terms, f"{expected} 不在别名集里: {terms}"


def test_terms_include_all_languages_and_dedup_and_keep_input_first():
    got = expand("Bangkok", "泰国")
    terms = got["terms"]
    assert terms[0] == "Bangkok", "用户原始写法应排最前, 便于日志排查"
    assert len(terms) == len(set(terms)), "别名去重"
    for expected in ("曼谷", "バンコク", "タイ", "Thailand", "泰国"):
        assert expected in terms


def test_country_only_place_gives_country_terms_not_city_terms():
    got = expand("泰国")
    assert got["city_key"] is None
    assert "曼谷" not in got["terms"], "只填国家时不该把某一座城市的别名混进来"


def test_unknown_place_degrades_to_raw_input():
    got = expand(" someplace unknown ")
    assert got["country_key"] is None and got["city_key"] is None
    assert got["terms"] == ["someplace unknown"], "查不到时也要能按原样匹配"


def test_empty_inputs():
    # 空地名不进别名集(空串 LIKE '%%' 会命中全库, 必须挡掉)
    assert expand("")["terms"] == []
    assert expand("", "泰国")["country_key"] == "thailand"
    assert expand("", "泰国")["city_key"] is None
    assert expand(None, None)["terms"] == []


def test_whitespace_and_case_insensitive():
    assert expand("  bangkok ")["city_key"] == "bangkok"
    assert expand("THAILAND")["country_key"] == "thailand"
    assert expand("New  York")["city_key"] == "newyork"
    assert expand("Bangkok", " タイ ")["country_key"] == "thailand"


def test_country_argument_resolves_when_place_unknown():
    """目的地写了本地写法、认不出, 但国家列认得出时, 别名仍要按那个国家补齐。"""
    got = expand("somewhere local", "タイ")
    assert got["country_key"] == "thailand"
    assert "タイ" in got["terms"] and "Thailand" in got["terms"]


def test_resolve_returns_keys_only():
    assert resolve("バンコク", None) == ("thailand", "bangkok")
    assert resolve("nowhere", None) == (None, None)


def test_normalize_term_folds_case_space_and_punctuation():
    assert normalize_term("New York") == "newyork"
    assert normalize_term("  New-York  ") == "newyork"
    assert normalize_term("-employed") == "employed"
    assert normalize_term("") == ""
    assert normalize_term(None) == ""


def test_hotspot_city_mapping():
    assert hotspot_city("unitedstates", "newyork") == "nyc"
    assert hotspot_city("unitedstates", "losangeles") == "la"
    assert hotspot_city("unitedstates", "sanfrancisco") == "sf"
    assert hotspot_city("unitedstates", "chicago") == "chicago"
    # 没登记 hotspot / 未知城市 / 未知国家 → None
    assert hotspot_city("unitedstates", "washington") is None
    assert hotspot_city("thailand", "bangkok") is None
    assert hotspot_city("thailand", None) is None
    assert hotspot_city("nowhere", "bangkok") is None


def test_load_table_indexes_every_alias_back_to_its_country():
    table = load_table()
    for term in ("タイ", "Thailand", "泰国", "Bangkok", "曼谷", "Seoul", "南部"):
        if term == "南部":  # 表里没有的写法就不该命中
            assert normalize_term(term) not in table["by_term"]
            continue
        assert normalize_term(term) in table["by_term"], f"{term} 没有进反查索引"


def test_missing_table_file_degrades_without_raising(tmp_path):
    """表读不到时全部退化成原样匹配, 不能抛 —— 别名表是增强不是硬依赖。"""
    reload()
    got = expand("曼谷", path=tmp_path / "nope.json")
    assert got["country_key"] is None
    assert got["terms"] == ["曼谷"]


def test_broken_table_file_degrades_without_raising(tmp_path):
    bad = tmp_path / "place_aliases.json"
    bad.write_text("{ not json", encoding="utf-8")
    reload()
    got = expand("Bangkok", path=bad)
    assert got["country_key"] is None
    assert got["terms"] == ["Bangkok"]


def test_malformed_table_entries_are_skipped(tmp_path):
    """表里混进脏数据(非 dict / 没 key / cities 非 dict)不能把加载打挂。"""
    bad = tmp_path / "place_aliases.json"
    bad.write_text(
        json.dumps(
            {
                "countries": [
                    "not-a-dict",
                    {"key": "", "zh": ["无 key"]},
                    {"key": "xland", "zh": ["测试国"], "cities": "not-a-dict"},
                    {"key": "thailand", "zh": ["泰国"], "ja": ["タイ"], "cities": {"bangkok": "not-a-dict"}},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reload()
    got = expand("泰国", path=bad)
    assert got["country_key"] == "thailand"
    assert got["city_key"] is None
    assert "テスト国" not in got["terms"]


def test_module_exposes_expected_surface():
    for name in ("expand", "resolve", "load_table", "reload", "normalize_term", "hotspot_city", "alias_path_for"):
        assert callable(getattr(place_aliases, name))
