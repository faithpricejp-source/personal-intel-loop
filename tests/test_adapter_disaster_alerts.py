"""disaster_alerts: 只收配置的盯点、只收事前预警、日本侧只收警報级。

阳性对照很重要: 日本侧"0 条"既可能是**正确过滤掉了注意報**, 也可能是**代码坏了**,
两者产出一模一样。所以每个否定断言都配一条已知为真的样本走同一条路径。
(实时数据常常全无警報级, 做不出阳性对照, 只能靠 fixture。)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from personal_intel_loop.adapters import disaster_alerts as da
from personal_intel_loop.adapters.disaster_alerts import DisasterAlertsAdapter

# 测试用的合成盯点(与真实配置无关)
TEST_WATCHED_CN = (
    {"province": "广东", "city": "广州市"},
    {"province": "浙江", "city": "西湖区", "match": ("西湖区", "杭州市气象台")},
)
TEST_WATCHED_JP = ({"area_code": "130000", "label": "東京都"},)


@pytest.fixture(autouse=True)
def _synthetic_watches(monkeypatch, tmp_path):
    monkeypatch.setattr(da, "WATCHED_CN", TEST_WATCHED_CN)
    monkeypatch.setattr(da, "WATCHED_JP", TEST_WATCHED_JP)
    monkeypatch.setattr(da, "LOCATION_FILE", tmp_path / "no_location.json")


def _cn_row(title, alertid="A1", issuetime="2026/08/26 20:38", url="/publish/alarm/x_1.html"):
    return {"alertid": alertid, "title": title, "issuetime": issuetime, "url": url, "pic": None}


CN_ROWS = [
    _cn_row("广东省广州市天河区气象台发布雷雨大风橙色预警信号", "A1"),
    _cn_row("广东省韶关市武江区气象台发布暴雨橙色预警信号", "A2"),      # 同省, 非目标市
    _cn_row("广东省气象台发布地质灾害黄色预警", "A3"),                   # 省级, 不含"广州市"
]

# 真实结构(2026-08-26 抓的 VPWW53_130000.xml 削减版)
def _jp_doc(items_xml: str, report="2026-08-26T16:57:00+09:00") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Report xmlns="http://xml.kishou.go.jp/jmaxml1/">
<Control><Title>気象特別警報・警報・注意報</Title><PublishingOffice>気象庁</PublishingOffice></Control>
<Head><ReportDateTime>{report}</ReportDateTime></Head>
<Body>{items_xml}</Body></Report>"""


_ADVISORY_ONLY = _jp_doc("""
<Item><Kind><Name>雷注意報</Name><Code>14</Code></Kind>
<Areas><Area><Name>千代田区</Name><Code>1310100</Code></Area></Areas></Item>
<Item><Kind><Name>波浪注意報</Name><Code>16</Code></Kind>
<Areas><Area><Name>大島町</Name><Code>1336100</Code></Area></Areas></Item>""")

_WITH_WARNING = _jp_doc("""
<Item><Kind><Name>雷注意報</Name><Code>14</Code></Kind>
<Areas><Area><Name>千代田区</Name><Code>1310100</Code></Area></Areas></Item>
<Item><Kind><Name>大雨警報</Name><Code>03</Code></Kind>
<Areas><Area><Name>八王子市</Name><Code>1320100</Code></Area></Areas></Item>
<Item><Kind><Name>暴風特別警報</Name><Code>33</Code></Kind>
<Areas><Area><Name>大島町</Name><Code>1336100</Code></Area></Areas></Item>""")

_FEED = """<feed>
<entry><title>気象特別警報・警報・注意報</title><updated>2026-08-26T07:00:00Z</updated>
<link href="https://x/20260826_0_VPWW53_130000.xml"/></entry>
<entry><title>気象特別警報・警報・注意報</title><updated>2026-08-26T07:57:56Z</updated>
<link href="https://x/20260826b_0_VPWW53_130000.xml"/></entry>
<entry><title>気象特別警報・警報・注意報</title><updated>2026-08-26T08:00:00Z</updated>
<link href="https://x/20260826_0_VPWW53_016000.xml"/></entry>
<entry><title>竜巻注意情報</title><updated>2026-08-26T09:00:00Z</updated>
<link href="https://x/20260826_0_VPOA50_130000.xml"/></entry>
</feed>"""


def _adapter(*, jp_doc=_WITH_WARNING, cn_rows=None, cn_province="广东"):
    return DisasterAlertsAdapter(
        fetch_cn=lambda province: (
            (cn_rows if cn_rows is not None else CN_ROWS) if province == cn_province else []),
        fetch_jp_feed=lambda: _FEED,
        fetch_jp_doc=lambda url: jp_doc,
    )


# ---------- 中国侧 ----------

def test_cn_keeps_only_watched_city():
    recs = [r for r in _adapter().collect() if ":cn:" in r.item.source]
    assert len(recs) == 1, [r.item.title for r in recs]
    assert "广州市天河区" in recs[0].item.title
    assert recs[0].item.source == "disaster_alerts:cn:广州市"


def test_cn_relative_url_absolutized():
    rec = [r for r in _adapter().collect() if ":cn:" in r.item.source][0]
    assert rec.item.url == "https://www.nmc.cn/publish/alarm/x_1.html"


def test_cn_issuetime_parsed_as_beijing_time():
    rec = [r for r in _adapter().collect() if ":cn:" in r.item.source][0]
    # 2026/08/26 20:38 CST == 12:38 UTC
    assert rec.item.ts == datetime(2026, 8, 26, 12, 38, tzinfo=timezone.utc)


def test_cn_since_filters_older():
    recs = _adapter().collect(since=datetime(2026, 8, 27, tzinfo=timezone.utc))
    assert [r for r in recs if ":cn:" in r.item.source] == []


@pytest.mark.parametrize("bad", ["", "2026-08-26 20:38", "昨天"])
def test_cn_unparseable_time_dropped_not_raised(bad):
    rows = [_cn_row("广东省广州市天河区气象台发布雷雨大风橙色预警信号", issuetime=bad)]
    assert [r for r in _adapter(cn_rows=rows).collect() if ":cn:" in r.item.source] == []


# ---------- 日本侧: 阴性 + 阳性成对 ----------

def test_jp_advisory_only_yields_nothing():
    """注意報 是常态背景噪音, 不是能让人行动的东西 —— 不产出条目。"""
    recs = [r for r in _adapter(jp_doc=_ADVISORY_ONLY).collect() if ":jp:" in r.item.source]
    assert recs == []


def test_jp_warning_level_is_kept():
    """阳性对照: 同一条代码路径, 文档里有警報时必须产出 —— 证明上面那条 0 是过滤不是坏掉。"""
    recs = [r for r in _adapter(jp_doc=_WITH_WARNING).collect() if ":jp:" in r.item.source]
    assert len(recs) == 1
    item = recs[0].item
    assert "大雨警報" in item.title and "暴風特別警報" in item.title
    assert "雷注意報" not in item.title and "雷注意報" not in item.body
    assert "八王子市: 大雨警報" in item.body
    assert item.source == "disaster_alerts:jp:東京都"
    assert item.ts == datetime(2026, 8, 26, 7, 57, tzinfo=timezone.utc)


def test_jp_picks_latest_doc_for_that_prefecture():
    """按府県码选文档, 且取 updated 最大的那份; 别的府県、别的电文种别都不能串进来。"""
    assert da._latest_jp_doc_url(_FEED, "130000") == (
        "https://x/20260826b_0_VPWW53_130000.xml", "2026-08-26T07:57:56Z")
    assert da._latest_jp_doc_url(_FEED, "016000")[0].endswith("_016000.xml")
    assert da._latest_jp_doc_url(_FEED, "999999") is None


def test_jp_warning_matcher_excludes_advisories():
    """钉住判据本身: 含「警報」的进(含 特別警報), 注意報 天然不含「警報」二字故自动出局。"""
    got = da._jp_warnings(_WITH_WARNING)
    assert ("八王子市", "大雨警報") in got
    assert ("大島町", "暴風特別警報") in got
    assert all("注意報" not in k for _, k in got)


# ---------- fail-soft ----------

def test_network_failure_yields_empty_not_exception():
    """一个地方拉不到不该让整轮 ingest 挂掉。"""
    adapter = DisasterAlertsAdapter(
        fetch_cn=lambda province: [],
        fetch_jp_feed=lambda: "",
        fetch_jp_doc=lambda url: "",
    )
    assert list(adapter.collect()) == []


# ---------- 等级门槛: 黄/蓝不采 ----------

def test_yellow_and_blue_dropped_at_ingest():
    """阴阳对照: 黄/蓝丢掉, 橙/红留下 —— 证明 0 条黄色是过滤而不是整条 CN 路径坏了。"""
    rows = [_cn_row("广东省广州市天河区气象台发布台风蓝色预警信号", "B1"),
            _cn_row("广东省广州市天河区气象台发布暴雨黄色预警信号", "Y1"),
            _cn_row("广东省广州市天河区气象台发布暴雨橙色预警信号", "O1"),
            _cn_row("广东省广州市天河区气象台发布暴雨红色预警信号", "R1")]
    got = [r.item.title for r in _adapter(cn_rows=rows).collect() if ":cn:" in r.item.source]
    assert len(got) == 2, got
    assert all("蓝色" not in t and "黄色" not in t for t in got)
    assert any("橙色" in t for t in got) and any("红色" in t for t in got)


def test_district_watch_narrows_to_that_district():
    """区县级盯点: 同一轮台风里同市别的区县丢掉, 盯的区留下。"""
    rows = [_cn_row("浙江省杭州市淳安县气象台发布台风橙色预警信号", "N1"),
            _cn_row("浙江省杭州市萧山区气象台发布台风橙色预警信号", "N2"),
            _cn_row("浙江省杭州市西湖区气象台发布台风橙色预警信号", "N3")]
    got = [r for r in _adapter(cn_rows=rows, cn_province="浙江").collect()
           if ":cn:" in r.item.source]
    assert [r.item.title for r in got] == ["浙江省杭州市西湖区气象台发布台风橙色预警信号"]
    assert got[0].item.source == "disaster_alerts:cn:西湖区"


def test_city_level_alert_still_kept_for_district_watch():
    """市级台不带区县但覆盖所盯的区 —— 不能因为标题里没有区名就漏掉。"""
    rows = [_cn_row("浙江省杭州市气象台发布台风橙色预警信号", "N4")]
    got = [r for r in _adapter(cn_rows=rows, cn_province="浙江").collect()
           if ":cn:" in r.item.source]
    assert len(got) == 1, [r.item.title for r in got]


def test_no_level_word_is_kept():
    """没写等级的不丢 —— 宁可多看一眼, 不能把没标等级的重灾情丢了。"""
    rows = [_cn_row("广东省广州市气象台发布山洪灾害气象风险预警", "N1")]
    got = [r for r in _adapter(cn_rows=rows).collect() if ":cn:" in r.item.source]
    assert len(got) == 1


def test_cn_level_mapping():
    assert da.cn_level("暴雨红色预警信号") == 4
    assert da.cn_level("暴雨橙色预警信号") == 3
    assert da.cn_level("暴雨黄色预警信号") == 2
    assert da.cn_level("台风蓝色预警信号") == 1
    assert da.cn_level("山洪灾害气象风险预警") == 0


# --- 临时位置覆盖（PIL_LOCATION_FILE） ---

def _reload_da(monkeypatch, payload, tmp_path):
    """把 LOCATION_FILE 指到临时文件再读，避免碰真实位置文件。"""
    import json as _json
    from personal_intel_loop.adapters import disaster_alerts as da
    f = tmp_path / "loc.json"
    if payload is not None:
        f.write_text(payload if isinstance(payload, str) else _json.dumps(payload, ensure_ascii=False))
    monkeypatch.setattr(da, "LOCATION_FILE", f)
    return da


def test_location_override_jp_adds_new_prefecture(monkeypatch, tmp_path):
    da = _reload_da(monkeypatch, {"country": "JP", "jp_area_code": "270000", "label": "大阪府"}, tmp_path)
    cn, jp = da._location_override()
    assert cn == ()
    assert jp == ({"area_code": "270000", "label": "大阪府"},)


def test_location_override_jp_skips_already_watched(monkeypatch, tmp_path):
    """已在 WATCHED_JP 里的府県不能重复追加。"""
    da = _reload_da(monkeypatch, {"country": "JP", "jp_area_code": "130000", "label": "東京都"}, tmp_path)
    assert da._location_override() == ((), ())


def test_location_override_cn_carries_match(monkeypatch, tmp_path):
    da = _reload_da(monkeypatch, {"country": "CN", "cn_province": "上海", "cn_city": "浦东新区",
                                  "cn_match": ["浦东新区", "上海市气象台"]}, tmp_path)
    cn, jp = da._location_override()
    assert jp == ()
    assert cn == ({"province": "上海", "city": "浦东新区", "match": ("浦东新区", "上海市气象台")},)


def test_location_override_falls_back_when_file_broken(monkeypatch, tmp_path):
    """坏文件/缺文件一律回落主配置盯点。"""
    da = _reload_da(monkeypatch, "{ 坏掉的 json", tmp_path)
    assert da._location_override() == ((), ())


def test_location_override_falls_back_when_file_missing(monkeypatch, tmp_path):
    da = _reload_da(monkeypatch, None, tmp_path)
    assert da._location_override() == ((), ())


# --- 盯点配置文件 ---

def test_load_watch_config_parses_cn_and_jp(tmp_path):
    import json as _json
    f = tmp_path / "disaster_alerts.json"
    f.write_text(_json.dumps({
        "cn": [{"province": "广东", "city": "广州市"},
               {"province": "浙江", "city": "西湖区", "match": "西湖区"}],
        "jp": [{"area_code": "270000", "label": "大阪府"}],
    }, ensure_ascii=False), encoding="utf-8")
    cn, jp = da.load_watch_config(f)
    assert cn == ({"province": "广东", "city": "广州市"},
                  {"province": "浙江", "city": "西湖区", "match": ("西湖区",)})
    assert jp == ({"area_code": "270000", "label": "大阪府"},)


def test_load_watch_config_missing_or_broken_is_empty(tmp_path):
    assert da.load_watch_config(tmp_path / "absent.json") == ((), ())
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    assert da.load_watch_config(bad) == ((), ())


def test_example_config_is_loadable():
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "config" / "disaster_alerts.example.json"
    cn, jp = da.load_watch_config(example)
    assert cn and jp
