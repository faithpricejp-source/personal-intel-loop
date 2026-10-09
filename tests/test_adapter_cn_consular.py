"""cn_consular: 只取安全提醒栏目前两页, 正文靠数括号取到 article-content, 国名抽不出落「综合」。

fixture 是 2026-10-04 实抓 `https://cs.mfa.gov.cn/aqtx/` 与 `index_1.html` 的列表片段,
以及 `202609/t20260914_12021693.html` 的详情片段(各删到 3-5 条)。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from personal_intel_loop.adapters import cn_consular as cc
from personal_intel_loop.adapters.cn_consular import CnConsularAdapter

FIX = Path(__file__).parent / "fixtures" / "risk"


def _read(name: str) -> str:
    return (FIX / name).read_text("utf-8")


def _adapter(tmp_path, **kw):
    kw.setdefault("fetch_list", lambda url: "")
    kw.setdefault("fetch_detail", lambda url: "")
    state = tmp_path if tmp_path.name == "state.json" else tmp_path / "state.json"
    return CnConsularAdapter(state_path=state, **kw)


# ---- 列表解析 --------------------------------------------------------------

def test_parse_list_field_mapping():
    entries = cc.parse_list(_read("cs_aqtx_list.html"))
    assert len(entries) == 5
    first = entries[0]
    # 相对链接要补全成绝对 URL
    assert first["url"] == ("https://cs.mfa.gov.cn/aqtx/202609/"
                            "t20260914_12021693.html")
    assert first["title"] == "提醒中国公民暂勿前往塔吉克斯坦和阿富汗边境地区"
    assert first["ts"] == datetime(2026, 9, 14, tzinfo=cc.CST)
    assert first["regions"] == ["塔吉克斯坦", "阿富汗"]


def test_parse_list_page2_relative_base():
    entries = cc.parse_list(_read("cs_aqtx_list_p2.html"), cc.PAGE_URLS[1])
    assert len(entries) == 3
    assert all(e["url"].startswith("https://cs.mfa.gov.cn/aqtx/") for e in entries)


def test_parse_list_broken_returns_empty():
    assert cc.parse_list("") == []
    assert cc.parse_list("<html><body>维护中</body></html>") == []


def test_regions_from_title():
    assert cc.regions_from_title("提醒中国公民暂勿前往塔吉克斯坦和阿富汗边境地区") == [
        "塔吉克斯坦", "阿富汗"]
    assert cc.regions_from_title("提醒中国公民谨慎赴帕劳旅游") == ["帕劳"]
    assert cc.regions_from_title("提醒在委内瑞拉中国公民加强安全防范") == ["委内瑞拉"]
    assert cc.regions_from_title("提醒中国公民近期避免前往日本") == ["日本"]
    assert cc.regions_from_title("安全提醒：驻留刚果（金）东部地区中国公民应立即撤离") == ["刚果（金）"]
    # 抽不出地名 -> 空, 由 collect 落「综合」
    assert cc.regions_from_title("外交部领事保护中心提醒注意安全") == []


# ---- 详情解析 --------------------------------------------------------------

def test_parse_detail():
    detail = cc.parse_detail(_read("cs_detail.html"))
    assert detail["title"] == "提醒中国公民暂勿前往塔吉克斯坦和阿富汗边境地区"
    assert detail["ts"] == datetime(2026, 9, 14, 14, 55, tzinfo=cc.CST)
    assert "塔吉克斯坦和阿富汗边境地区安全形势复杂严峻" in detail["body"]
    assert "<p" not in detail["body"] and "&nbsp;" not in detail["body"]


def test_parse_detail_nested_div_is_balanced():
    """正文里嵌套一层 div —— 拿第一个 </div> 当结尾会得到空 body(实测踩过)。"""
    page = ('<h1 class="article-title">T</h1>'
            '<div class="article-content">\n<div class="view_default TRS_UEDITOR">'
            "<p>第一段</p><p>第二段</p></div>\n</div>\n"
            '<div class="related-news">不该被收进来</div>')
    detail = cc.parse_detail(page)
    assert "第一段" in detail["body"] and "第二段" in detail["body"]
    assert "不该被收进来" not in detail["body"]


def test_parse_detail_picks_article_title_not_site_logo():
    """详情页第一个 <h1> 是站点 logo, 第二个才是文章标题(实测踩过)。"""
    page = ('<h1>中国领事服务网</h1>'
            '<h1 class="article-title">提醒中国公民暂勿前往X国</h1>'
            '<div class="article-content"><p>正文</p></div>')
    assert cc.parse_detail(page)["title"] == "提醒中国公民暂勿前往X国"


def test_parse_detail_broken_returns_empty_body():
    detail = cc.parse_detail("<html><body>404</body></html>")
    assert detail["body"] == ""
    assert detail["ts"] is None


# ---- collect ---------------------------------------------------------------

def _pages(p1: str, p2: str = ""):
    def fetch(url: str) -> str:
        return p1 if url == cc.PAGE_URLS[0] else p2
    return fetch


def _details(body_ok: bool = True):
    def fetch(url: str) -> str:
        return _read("cs_detail.html") if body_ok else "<html>404</html>"
    return fetch


def test_collect_field_mapping(tmp_path):
    recs = list(_adapter(tmp_path,
                         fetch_list=_pages(_read("cs_aqtx_list.html"), _read("cs_aqtx_list_p2.html")),
                         fetch_detail=_details(), days=3650).collect())
    by_url = {r.item.url: r for r in recs}
    assert len(recs) == 8   # 第 1 页 5 条 + 第 2 页 3 条

    top = by_url["https://cs.mfa.gov.cn/aqtx/202609/t20260914_12021693.html"]
    assert top.item.source == "cn_consular:塔吉克斯坦"
    assert top.item.lang == "zh"
    assert top.item.author == cc.ISSUER
    assert top.item.ts == datetime(2026, 9, 14, 6, 55, tzinfo=timezone.utc)

    payload = json.loads(top.source_payload_json)
    assert payload["kind"] == "risk"
    assert payload["issuer"] == cc.ISSUER
    assert payload["published"] == "2026-09-14T14:55:00+08:00"
    assert payload["regions"] == ["塔吉克斯坦", "阿富汗"]
    assert payload["level"] is None


def test_collect_两页都取(tmp_path):
    recs = list(_adapter(tmp_path,
                         fetch_list=_pages(_read("cs_aqtx_list.html"), _read("cs_aqtx_list_p2.html")),
                         fetch_detail=_details(), days=3650).collect())
    urls = {r.item.url for r in recs}
    # 第 2 页那三条(实测它们的 URL 日期 202607 与列表显示日期 2025-11/12 不一致, 官方如此)
    assert "https://cs.mfa.gov.cn/aqtx/202607/t20260721_11988716.html" in urls
    assert len(urls) == 8


def test_collect_pages_one_only_first(tmp_path):
    recs = list(_adapter(tmp_path,
                         fetch_list=_pages(_read("cs_aqtx_list.html"), _read("cs_aqtx_list_p2.html")),
                         fetch_detail=_details(), days=3650, pages=1).collect())
    assert len(recs) == 5


def test_collect_time_window(tmp_path):
    """默认 14 天窗口(实测日 2026-10-05): fixture 最新一条 09-14, 已出窗 -> 空。"""
    recs = list(_adapter(tmp_path,
                         fetch_list=_pages(_read("cs_aqtx_list.html"), _read("cs_aqtx_list_p2.html")),
                         fetch_detail=_details()).collect())
    assert recs == []
    # 阳性对照: since 放到 2026-09-01 就有 1 条
    one = list(_adapter(tmp_path / "b",
                        fetch_list=_pages(_read("cs_aqtx_list.html")),
                        fetch_detail=_details(),
                        days=1).collect(since=datetime(2026, 9, 1, tzinfo=timezone.utc)))
    assert len(one) == 1


def test_collect_dedup_second_run_empty(tmp_path):
    first = _adapter(tmp_path, fetch_list=_pages(_read("cs_aqtx_list.html")),
                     fetch_detail=_details(), days=3650)
    assert len(list(first.collect())) == 5
    second = _adapter(tmp_path, fetch_list=_pages(_read("cs_aqtx_list.html")),
                      fetch_detail=_details(), days=3650)
    assert list(second.collect()) == []


def test_collect_no_country_falls_back(tmp_path):
    page = ('<ul class="news-list"><li><a href="./202601/t20260101_1.html">'
            '外交部领事保护中心提醒注意安全</a><span>2026-01-01</span></li></ul>')
    detail = ('<h1 class="article-title">外交部领事保护中心提醒注意安全</h1>'
              '<div class="article-content"><p>请注意安全。</p></div>')
    recs = list(_adapter(tmp_path, fetch_list=_pages(page),
                         fetch_detail=lambda url: detail, days=3650).collect())
    assert len(recs) == 1
    assert recs[0].item.source == "cn_consular:综合"
    assert json.loads(recs[0].source_payload_json)["regions"] == ["综合"]


def test_collect_fetch_failure_returns_empty_not_raise(tmp_path):
    assert list(_adapter(tmp_path, fetch_list=lambda url: "").collect()) == []
    assert list(_adapter(tmp_path, fetch_list=lambda url: "<html>503</html>").collect()) == []
    # 列表有、正文抓不到 -> 跳过, 不抛
    assert list(_adapter(tmp_path, fetch_list=_pages(_read("cs_aqtx_list.html")),
                         fetch_detail=_details(body_ok=False)).collect()) == []


def test_collect_limit(tmp_path):
    recs = list(_adapter(tmp_path, fetch_list=_pages(_read("cs_aqtx_list.html")),
                         fetch_detail=_details(), days=3650).collect(limit=2))
    assert len(recs) == 2


def test_collect_sorted_newest_first(tmp_path):
    recs = list(_adapter(tmp_path,
                         fetch_list=_pages(_read("cs_aqtx_list.html"), _read("cs_aqtx_list_p2.html")),
                         fetch_detail=_details(), days=3650).collect())
    assert [r.item.ts for r in recs] == sorted((r.item.ts for r in recs), reverse=True)


def test_state_broken_file_not_raise(tmp_path):
    bad = tmp_path / "state.json"
    bad.write_text("{not json", "utf-8")
    recs = list(CnConsularAdapter(fetch_list=_pages(_read("cs_aqtx_list.html")),
                                  fetch_detail=_details(), state_path=bad,
                                  days=3650).collect())
    assert len(recs) == 5


# ---- 10-05 复审 R11 · limit 截断在 seen 标记之后 -----------------------------

def test_collect_limit_does_not_swallow_dropped_entries(tmp_path):
    """`limit` 落选的条目**不进 seen** —— 下一轮不限量时必须还能拿到。"""
    first = list(_adapter(tmp_path, fetch_list=_pages(_read("cs_aqtx_list.html")),
                          fetch_detail=_details(), days=3650).collect(limit=2))
    assert len(first) == 2
    second = list(_adapter(tmp_path, fetch_list=_pages(_read("cs_aqtx_list.html")),
                           fetch_detail=_details(), days=3650).collect())
    assert len(second) == 3, "落选的 3 条被 seen 吞掉了"
    assert not ({r.item.url for r in first} & {r.item.url for r in second})
    # 第三轮: 5 条都见过了 -> 0 条(阳性对照)
    assert list(_adapter(tmp_path, fetch_list=_pages(_read("cs_aqtx_list.html")),
                         fetch_detail=_details(), days=3650).collect()) == []


# ---- 10-05 复审 R12 · 剥噪词表顺序错误 ---------------------------------------

def test_regions_from_title_no_fake_region_from_live_titles():
    """真实列表页标题: `伊朗及周边地区` 不得抽出 `周边` 这种非地名。

    修复前: `_GEO_TAIL_NOISE` 里 `地区` 排在 `周边地区` 之前, `伊朗及周边地区` 先被剥成
    `伊朗及周边`, 此后不再匹配任何噪声词 -> 拆出 `['伊朗', '周边']`(实测真实列表页
    2026-03-11 那条)。降序剥噪后应为 `['伊朗']`。
    """
    assert cc.regions_from_title("再次提醒中国公民暂勿前往伊朗及周边地区") == ["伊朗"]
    assert cc.regions_from_title("提醒中国公民暂勿前往伊朗周边地区") == ["伊朗"]
    # 并列 + 双层后缀
    assert cc.regions_from_title("提醒中国公民暂勿前往塔吉克斯坦和阿富汗边境地区") == [
        "塔吉克斯坦", "阿富汗"]
    # 阳性对照: 单层后缀不受影响
    assert cc.regions_from_title("安全提醒：驻留刚果（金）东部地区中国公民应立即撤离") == ["刚果（金）"]


def test_parse_list_live_page_has_no_fake_region():
    """端到端: 真实列表页 15 条里不得出现 `周边` 这种假地名。"""
    entries = cc.parse_list(_read("cs_aqtx_list_live.html"))
    assert len(entries) == 15
    for e in entries:
        assert "周边" not in e["regions"], f"{e['title']} -> {e['regions']}"
    by_title = {e["title"]: e["regions"] for e in entries}
    assert by_title["再次提醒中国公民暂勿前往伊朗及周边地区"] == ["伊朗"]
    assert by_title["提醒中国公民暂勿前往塔吉克斯坦和阿富汗边境地区"] == [
        "塔吉克斯坦", "阿富汗"]


def test_regions_from_title_rejects_generic_area_words():
    """泛指的方位/数量词不得进 regions(它们不是国家/地区实体)。"""
    for title in ("提醒中国公民暂勿前往周边地区",
                  "提醒中国公民谨慎赴一带旅游",
                  "提醒中国公民近期避免前往附近地区"):
        assert cc.regions_from_title(title) == [], title


# ---- 10-05 复审 R13 · 每条都无条件抓详情页 -----------------------------------

def test_collect_only_fetches_details_it_returns(tmp_path):
    """`limit` 落选的条目不该触发详情页请求(实测原来两页 30 条 -> 每轮固定 30 次请求)。"""
    calls: list[str] = []

    def counting_fetch(url: str) -> str:
        calls.append(url)
        return _read("cs_detail.html")

    recs = list(_adapter(tmp_path, fetch_list=_pages(_read("cs_aqtx_list.html")),
                         fetch_detail=counting_fetch, days=3650).collect(limit=2))
    assert len(recs) == 2
    assert len(calls) == 2, f"只应抓 2 个详情页, 实际抓了 {len(calls)} 个"


def test_collect_window_filter_runs_before_detail_fetch(tmp_path):
    """窗外条目在抓详情页**之前**就被滤掉(列表页日期已够做窗口判断)。"""
    calls: list[str] = []

    def counting_fetch(url: str) -> str:
        calls.append(url)
        return _read("cs_detail.html")

    # 默认 14 天窗口(实测日 2026-10-05): fixture 最新一条 09-14, 已出窗
    assert list(_adapter(tmp_path, fetch_list=_pages(_read("cs_aqtx_list.html")),
                         fetch_detail=counting_fetch).collect()) == []
    assert calls == [], "窗外条目不该触发详情页请求"


def test_collect_live_detail_page_body(tmp_path):
    """真实详情页字节走一遍: 正文与标题都抽得到(阳性对照, 证明上面几条不是"全丢"的假通过)。"""
    page = _read("cs_aqtx_list_live.html")
    detail = _read("cs_detail_live.html")
    recs = list(_adapter(tmp_path, fetch_list=_pages(page),
                         fetch_detail=lambda u: detail, days=3650).collect())
    assert len(recs) == 15
    top = recs[0]
    assert top.item.title == "提醒中国公民暂勿前往塔吉克斯坦和阿富汗边境地区"
    assert "塔吉克斯坦和阿富汗边境地区安全形势复杂严峻" in top.item.body
    assert "<p" not in top.item.body


# ---- naive since 不依赖宿主 TZ ----------------------------------------------

def test_collect_naive_since_uses_cst(monkeypatch, tmp_path):
    """naive `--since` 按北京时间(站点所在地)解释, 不随宿主 TZ 漂移。

    修复前: `naive.astimezone(utc)` 按宿主时区算, `TZ=Asia/Tokyo` 与 `TZ=America/New_York`
    下 cutoff 差 13 小时。条目日期卡在 `2026-09-01T12:00 JST`(= 09-01T03:00Z), 正好落在
    各宿主时区算出的 cutoff 之间, 所以结果会分叉。
    """
    import time as _time

    results = {}
    for tz in ("UTC", "Asia/Tokyo", "America/New_York"):
        monkeypatch.setenv("TZ", tz)
        _time.tzset()
        results[tz] = [r.item.url for r in
                       _adapter(tmp_path / tz.replace("/", "_"),
                                fetch_list=_pages(_page_for("20260901")),
                                fetch_detail=lambda url: _detail_for("20260901"),
                                days=3650
                                ).collect(since=datetime(2026, 9, 1))]
    monkeypatch.delenv("TZ", raising=False)
    _time.tzset()
    assert results["UTC"] == results["Asia/Tokyo"] == results["America/New_York"], \
        f"naive since 的解释随宿主 TZ 漂移: {results}"
    assert len(results["UTC"]) == 1   # 阳性对照: 09-01T12:00 JST 在 since=09-01 之后


def _page_for(stamp: str) -> str:
    """列表页片段(日期 `<span>` 与 URL 前缀一致)。`stamp` 形如 `20260901`。"""
    date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
    return ('<ul class="news-list"><li>'
            f'<a href="./{stamp[:6]}/t{stamp}_1.html">提醒中国公民暂勿前往X国</a>'
            f'<span>{date}</span></li></ul>')


def _detail_for(stamp: str) -> str:
    """详情页片段,发布时间与列表页同日、带时分。"""
    date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
    return ('<h1 class="article-title">提醒中国公民暂勿前往X国</h1>'
            f'<div class="article-meta"><span>发布时间：{date} 12:00</span></div>'
            '<div class="article-content"><p>正文内容。</p></div>')
