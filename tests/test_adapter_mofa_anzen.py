"""mofa_anzen: 只收外务省认定的那四类, level 用官方原文, 広域信息不硬猜国名。

阴性断言都配阳性对照 —— "0 条" 既可能是正确过滤, 也可能是代码坏了, 两者产出一模一样。
fixture 全部来自 2026-10-04 实抓 (原件留在 out/probe/), 删到 3-5 条。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

from personal_intel_loop.adapters import mofa_anzen as ma
from personal_intel_loop.adapters.mofa_anzen import MofaAnzenAdapter

FIX = Path(__file__).parent / "fixtures" / "risk"


def _read(name: str) -> str:
    return (FIX / name).read_text("utf-8")


def _read_bytes(name: str) -> bytes:
    """**原始字节**读入 —— 编码缺陷(R01)只在走解码路径时才暴露, 读成 str 会绕过去。

    2026-10-05 实测: 详情页响应头是 `Content-Type: text/html`(无 charset), requests 把
    `r.encoding` 设成 ISO-8859-1, 旧 fixture 是已解码的 str 写进磁盘的, 所以编码 bug
    在旧测试里**不可能**出现。
    """
    return (FIX / name).read_bytes()


#: 真正的 latin-1 乱码形态: 把 UTF-8 字节按 latin-1 解出来的样子(`外務省` → `å¤\x96å\x8b\x99ç\x9c\x81`)。
#: ⚠ 别拿 `æµ·å¤` 当乱码标记 —— 那是**正确的** UTF-8 文本, 只是长得像乱码。
LATIN1_MOJIBAKE = "外務省".encode("utf-8").decode("latin-1")


def _fake_response(content: bytes, content_type: str = "text/html",
                   status: int = 200) -> requests.Response:
    """构造一个真实形状的 `requests.Response`(不联网)。

    ⚠ 必须照 `requests.Session.send` 的做法把 `r.encoding` 按响应头设好
    (`get_encoding_from_headers`): 真实请求里 `Content-Type: text/html`(无 charset) 会让
    `r.encoding` 变成 `ISO-8859-1`, 这正是 R01 的触发条件。手工 new 一个不设 encoding 的
    Response 会让 `r.text` 走 chardet 猜测而"碰巧"解对, 编码缺陷在测试里就消失了。
    """
    r = requests.Response()
    r.status_code = status
    r._content = content
    r.headers["Content-Type"] = content_type
    r.encoding = requests.utils.get_encoding_from_headers(r.headers)
    return r


def _adapter(tmp_path, **kw):
    kw.setdefault("fetch_open_data", lambda: "")
    kw.setdefault("fetch_rss", lambda: "")
    kw.setdefault("fetch_detail", lambda key_cd: "")
    kw.setdefault("fetch_page", lambda url: "")
    # fixture 里最新的一条是 2026-09-16(T090)/ 09-28(C045), 距实测日 2026-10-05 已超 14 天,
    # 所以这里把窗口放宽到 30 天, 时间窗本身另有一个测试专门盯。
    kw.setdefault("days", 30)
    return MofaAnzenAdapter(state_path=tmp_path / "state.json", **kw)


# ---- 解析 ------------------------------------------------------------------

def test_parse_open_data_keeps_only_target_types():
    entries = ma.parse_open_data(_read("mofa_newarrival.xml"))
    types = {e["info_type"] for e in entries}
    # fixture = 2 条 R10 領事メール + 1 条 C30 スポット + 1 条 T40 危険
    assert types == {"C30", "T40"}
    assert "R10" not in types


def test_parse_open_data_field_mapping():
    entries = {e["key_cd"]: e for e in ma.parse_open_data(_read("mofa_newarrival.xml"))}
    spot = entries["2026C045"]
    assert spot["title"] == "ボリビアにおける緊急事態宣言の延長及び燃料不足等に伴う注意喚起"
    assert spot["country"] == "ボリビア"
    assert spot["area"] == "中南米"
    assert spot["url"] == "https://www.anzen.mofa.go.jp/info/pcspotinfo_2026C045.html"
    # leaveDate "2026/09/28 00:00:00" 是 JST -> UTC 前一天 15:00
    assert spot["ts"] == datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)
    assert spot["body"]


def test_parse_open_data_broken_structure_returns_empty():
    assert ma.parse_open_data("") == []
    assert ma.parse_open_data("<html><body>403</body></html>") == []


def test_parse_rss_category_whitelist():
    entries = ma.parse_rss(_read("mofa_news.xml"))
    cats = {e["category"] for e in entries}
    assert cats <= ma.TARGET_CATEGORIES
    assert "危険情報" in cats and "スポット情報" in cats
    # keyCd 从 link 反解
    assert {e["key_cd"] for e in entries} >= {"2026C045", "2026T090"}


def test_level_from_title_uses_official_text():
    assert ma._level_from_title("北マケドニアの危険情報【危険レベル解除】") == "危険レベル解除"
    assert ma._level_from_title("イラクの危険情報【危険レベル3】") == "危険レベル3"
    assert ma._level_from_title("感染症危険情報（レベル１）の発出") is None
    assert ma._level_from_title("no level here") is None


def test_country_from_title_only_known_patterns():
    assert ma._country_from_title("北マケドニアの危険情報【危険レベル解除】") == "北マケドニア"
    assert ma._country_from_title("ハイチの危険情報【危険レベル継続】（内容の更新）") == "ハイチ"
    assert ma._country_from_title("ボリビアにおける緊急事態宣言の延長") == "ボリビア"
    # 広域情報 没有国名句式 -> 不硬猜
    assert ma._country_from_title("中東情勢を受けた注意喚起（９月20日）") == ""


# ---- collect ---------------------------------------------------------------

def test_collect_field_mapping(tmp_path):
    recs = list(_adapter(tmp_path, fetch_open_data=lambda: _read("mofa_newarrival.xml")).collect())
    by_url = {r.item.url: r for r in recs}
    assert len(recs) == 2

    spot = by_url["https://www.anzen.mofa.go.jp/info/pcspotinfo_2026C045.html"]
    assert spot.item.source == "mofa_anzen:ボリビア"
    assert spot.item.lang == "ja"
    assert spot.item.author == "外務省"
    assert spot.item.ts == datetime(2026, 9, 27, 15, 0, tzinfo=timezone.utc)

    payload = json.loads(spot.source_payload_json)
    assert payload["kind"] == "risk"
    assert payload["issuer"] == "外務省"
    assert payload["published"] == "2026-09-28T00:00:00+09:00"
    assert payload["regions"] == ["ボリビア"]
    assert payload["level"] is None          # スポット情報 没有等级
    assert payload["key_cd"] == "2026C045"

    hazard = by_url["https://www.anzen.mofa.go.jp/info/pchazardspecificinfo_2026T090.html"]
    assert hazard.item.source == "mofa_anzen:北マケドニア共和国"  # 官方 <country><name> 原文
    assert json.loads(hazard.source_payload_json)["level"] == "危険レベル解除"


def test_collect_dedup_second_run_empty(tmp_path):
    xml = _read("mofa_newarrival.xml")
    state = tmp_path / "state.json"
    first = _adapter(tmp_path, fetch_open_data=lambda: xml)
    assert len(list(first.collect())) == 2
    second = _adapter(tmp_path, fetch_open_data=lambda: xml)
    assert list(second.collect()) == []


def test_collect_time_window(tmp_path):
    xml = _read("mofa_newarrival.xml")
    recs = list(_adapter(tmp_path, fetch_open_data=lambda: xml).collect(
        since=datetime(2026, 9, 27, 16, 0, tzinfo=timezone.utc)))
    assert recs == []


def test_collect_wide_area_falls_back(tmp_path):
    """広域情報(C50) 的单条 XML 里也没有 <country>, 必须落成 mofa_anzen:広域 而不是猜国名。"""
    recs = list(_adapter(tmp_path, fetch_rss=lambda: _read("mofa_news.xml"),
                         fetch_detail=lambda k: _read("mofa_mail_C042.xml")).collect())
    wide = [r for r in recs if "pcwideareaspecificinfo" in r.item.url]
    assert len(wide) == 1
    assert wide[0].item.source == "mofa_anzen:広域"
    assert json.loads(wide[0].source_payload_json)["regions"] == ["広域"]


def test_collect_fetches_detail_page_when_body_missing(tmp_path):
    """RSS 的 description 逐字重复 title, 所以 RSS 条目必须去抓详情才有正文。"""
    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>テスト国における危険情報【危険レベル2】</title>
    <link>https://www.anzen.mofa.go.jp/info/pchazardspecificinfo_2026T999.html</link>
    <description>テスト国における危険情報【危険レベル2】</description><category>危険情報</category>
    <pubDate>Mon, 28 Sep 2026 18:53:21 +0900</pubDate></item>
    </channel></rss>"""
    # 2026-10-04 实测: T 系列单条 XML 恒返回 2023 字节维护页 -> 必须能识别并退回详情页
    recs = list(_adapter(
        tmp_path,
        fetch_rss=lambda: rss,
        fetch_detail=lambda key_cd: _read("mofa_blocked_T090.html"),
        fetch_page=lambda url: _read("mofa_detail_C045.html"),
    ).collect())
    assert len(recs) == 1
    # description 逐字重复 title 时不能拿它当正文(实测 13/13 条如此)
    assert recs[0].item.body != "テスト国における危険情報【危険レベル2】"
    assert "【ポイント】" in recs[0].item.body
    assert json.loads(recs[0].source_payload_json)["level"] == "危険レベル2"


def test_collect_country_from_single_item_xml(tmp_path):
    """RSS 标题猜不出国名时, 单条 XML 的 <country><name> 优先。"""
    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>テスト</title>
    <link>https://www.anzen.mofa.go.jp/info/pcspotinfo_2026C043.html</link>
    <description>摘要</description><category>スポット情報</category>
    <pubDate>Sat, 26 Sep 2026 14:34:11 +0900</pubDate></item>
    </channel></rss>"""
    recs = list(_adapter(tmp_path, fetch_rss=lambda: rss,
                         fetch_detail=lambda k: _read("mofa_mail_C043.xml")).collect())
    assert len(recs) == 1
    assert recs[0].item.source == "mofa_anzi:エチオピア".replace("mofa_anzi", "mofa_anzen")
    assert recs[0].item.body


def test_collect_fetch_failure_returns_empty_not_raise(tmp_path):
    # 契约: 取数失败/结构变了 -> 记 warning 返回空, 不抛。
    assert list(_adapter(tmp_path, fetch_open_data=lambda: "<html>403</html>").collect()) == []
    assert list(_adapter(tmp_path, fetch_rss=lambda: "").collect()) == []
    # 有条目但正文补不到 -> 跳过, 也不抛(RSS 里 5 条有 4 条 body 为空, 补不到就只剩 1 条)
    recs = list(_adapter(
        tmp_path,
        fetch_rss=lambda: _read("mofa_news.xml"),
        fetch_detail=lambda k: "",
        fetch_page=lambda u: "",
    ).collect())
    assert [r.item.url for r in recs] == [
        "https://www.anzen.mofa.go.jp/info/pchazardspecificinfo_2026T090.html"]


def test_collect_limit(tmp_path):
    recs = list(_adapter(tmp_path, fetch_open_data=lambda: _read("mofa_newarrival.xml")).collect(limit=1))
    assert len(recs) == 1


def test_state_broken_file_not_raise(tmp_path):
    bad = tmp_path / "state.json"
    bad.write_text("{not json", "utf-8")
    recs = list(MofaAnzenAdapter(fetch_open_data=lambda: _read("mofa_newarrival.xml"),
                                 fetch_rss=lambda: "", fetch_detail=lambda k: "",
                                 fetch_page=lambda u: "", state_path=bad,
                                 days=30).collect())
    assert len(recs) == 2


def test_two_sources_same_keycd_deduped(tmp_path):
    recs = list(_adapter(tmp_path,
                         fetch_open_data=lambda: _read("mofa_newarrival.xml"),
                         fetch_rss=lambda: _read("mofa_news.xml")).collect())
    urls = [r.item.url for r in recs]
    assert len(urls) == len(set(urls))
    assert "https://www.anzen.mofa.go.jp/info/pcspotinfo_2026C045.html" in urls


# ---- 10-05 复审 R01 · 编码 ---------------------------------------------------

def test_get_decodes_via_meta_charset_not_latin1(monkeypatch):
    """响应头 `text/html` 无 charset 时必须按 `<meta charset>` 解, 不能退回 ISO-8859-1。

    修复前: `_get` 返回 `r.text`, requests 对无 charset 的 `text/*` 一律按ISO-8859-1 解
    → `外務省` 变成 `å¤\x96å\x8b\x99ç\x9c\x81`(见 `LATIN1_MOJIBAKE`), 且 `【ポイント】`
    匹配不上 → 静默写入乱码条目。
    """
    raw = _read_bytes("mofa_detail_T090_live.html")
    resp = _fake_response(raw, "text/html")
    assert resp.encoding == "ISO-8859-1", "本测试的前提: 无 charset 的 text/* 被requests 判成 latin-1"
    assert LATIN1_MOJIBAKE in raw.decode("latin-1"), "本测试的前提: 按 latin-1 解确实是乱码"
    monkeypatch.setattr(ma.requests, "get", lambda *a, **k: resp)

    html_text = ma._get("https://www.anzen.mofa.go.jp/info/pchazardspecificinfo_2026T090.html")
    assert html_text is not None
    assert "外務省" in html_text
    assert LATIN1_MOJIBAKE not in html_text     # latin-1 乱码标记
    assert "【ポイント】" in html_text


def test_get_prefers_meta_over_wrong_header_charset(monkeypatch):
    """`<meta charset>` 与响应头矛盾时以 meta 为准(R15 同型: 不要被头部/统计猜测覆盖)。"""
    raw = _read_bytes("mofa_detail_T090_live.html")
    resp = _fake_response(raw, "text/html; charset=Windows-1252")
    monkeypatch.setattr(ma.requests, "get", lambda *a, **k: resp)
    html_text = ma._get("https://www.anzen.mofa.go.jp/info/x.html")
    assert "【ポイント】" in html_text


def test_decode_falls_back_to_utf8_when_no_declaration():
    assert ma._decode("テスト".encode("utf-8")) == "テスト"
    assert ma._decode("テスト".encode("utf-8"), "ISO-8859-1") == "テスト"


def test_collect_decodes_detail_page_end_to_end(monkeypatch, tmp_path):
    """端到端: 详情页字节 → `_get` 解码 → 入库正文可读, 不是乱码条目。

    这是 R01 的真正端到端证据: 修复前走详情页路线的条目 body 就是 latin-1 乱码, 且因为
    乱码非空所以 `if not body` 守卫不生效, 条目照样入库(元数据全正常, 看不出来)。

    ⚠ 用**自造的 RSS** 而不是 fixture: 真实 RSS 里 T090 的 `<description>` 已经带了 525 字
    的摘要(实测), 于是 `if not body` 不成立, **根本不会去抓详情页**, 这个测试就成了假通过。
    这里让 description 逐字重复 title(= 没有正文, 实测13/13 条的常态), 强制走详情页路线。
    """
    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>テスト国における危険情報【危険レベル2】</title>
    <link>https://www.anzen.mofa.go.jp/info/pchazardspecificinfo_2026T090.html</link>
    <description>テスト国における危険情報【危険レベル2】</description>
    <category>危険情報</category>
    <pubDate>Wed, 16 Sep 2026 15:00:00 +0900</pubDate></item>
    </channel></rss>"""
    raw = _read_bytes("mofa_detail_T090_live.html")
    calls: list[str] = []

    def fake_get(url, **kw):
        calls.append(url)
        resp = _fake_response(raw, "text/html")
        return resp

    monkeypatch.setattr(ma.requests, "get", fake_get)

    recs = list(MofaAnzenAdapter(fetch_open_data=lambda: "",
                                fetch_rss=lambda: rss,
                                fetch_detail=lambda key_cd: "",
                                state_path=tmp_path / "state.json",
                                days=400).collect())
    assert any(u.endswith("2026T090.html") for u in calls), \
        "本测试的前提: 必须真的去抓了详情页(否则测不到解码路径)"
    assert len(recs) == 1
    body = recs[0].item.body
    assert LATIN1_MOJIBAKE not in body, "入库正文是 latin-1 乱码"
    assert "【ポイント】" in body
    # 修复前正文是整页剥标签(掺着全站导航/版权), 修复后只取 id="contents" 容器
    assert "Copyright ©" not in body
    assert "お問い合わせ" not in body


# ---- 10-05 复审 R02 · limit 截断在 seen 标记之后 -----------------------------

def test_collect_limit_does_not_swallow_dropped_entries(tmp_path):
    """`limit` 落选的条目**不进 seen** —— 下一轮不限量时必须还能拿到。

    修复前: `seen.add()` 对每条产出都执行且 state 在 `records[:limit]` 之前落盘, 被截掉的
    那条 key_cd 已进 seen → 下一轮 `key_cd in seen` 直接跳过 → 永久丢失, 无任何日志。
    """
    xml = _read("mofa_newarrival.xml")
    first = list(_adapter(tmp_path, fetch_open_data=lambda: xml).collect(limit=1))
    assert len(first) == 1

    # 同一 state 文件, 不再限量 -> 落选的那条应该还在
    second = list(_adapter(tmp_path, fetch_open_data=lambda: xml).collect())
    assert len(second) == 1, "落选条目被 seen 吞掉了"
    assert second[0].item.url != first[0].item.url

    # 第三轮: 两条都见过了 -> 0 条(阳性对照)
    assert list(_adapter(tmp_path, fetch_open_data=lambda: xml).collect()) == []


# ---- 10-05 复审 R03 · 维护页当正文 -------------------------------------------

def test_strip_page_rejects_maintenance_page():
    """HTTP 200 的「找不到页面」维护页正文非空, 必须识别成空(否则当正文入库)。"""
    assert ma._strip_page(_read("mofa_blocked_T090.html")) == ""


def test_collect_skips_entry_whose_detail_page_is_maintenance(tmp_path):
    """详情页是维护页时该条目**不入库、也不进 seen**(修复后站点恢复能补抓)。"""
    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>テスト国における危険情報【危険レベル2】</title>
    <link>https://www.anzen.mofa.go.jp/info/pchazardspecificinfo_2026T999.html</link>
    <description>テスト国における危険情報【危険レベル2】</description><category>危険情報</category>
    <pubDate>Mon, 28 Sep 2026 18:53:21 +0900</pubDate></item>
    </channel></rss>"""
    blocked = _read_bytes("mofa_blocked_T090.html")
    recs = list(_adapter(tmp_path, fetch_rss=lambda: rss,
                         fetch_detail=lambda k: "",
                         fetch_page=lambda url: blocked.decode("utf-8")).collect())
    assert recs == [], "维护页正文不得入库"

    # seen 也不能记 —— 否则站点恢复后不再补抓
    assert ma._load_state(tmp_path / "state.json") == set()


def test_collect_recovers_after_detail_page_back_online(tmp_path):
    """维护页那轮没进 seen, 详情页恢复后下一轮能补到正确正文。"""
    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>テスト国における危険情報【危険レベル2】</title>
    <link>https://www.anzen.mofa.go.jp/info/pchazardspecificinfo_2026T999.html</link>
    <description>テスト国における危険情報【危険レベル2】</description><category>危険情報</category>
    <pubDate>Mon, 28 Sep 2026 18:53:21 +0900</pubDate></item>
    </channel></rss>"""
    blocked = _read_bytes("mofa_blocked_T090.html").decode("utf-8")
    good = _read("mofa_detail_C045.html")
    assert list(_adapter(tmp_path, fetch_rss=lambda: rss, fetch_detail=lambda k: "",
                         fetch_page=lambda url: blocked).collect()) == []
    recs = list(_adapter(tmp_path, fetch_rss=lambda: rss, fetch_detail=lambda k: "",
                         fetch_page=lambda url: good).collect())
    assert len(recs) == 1 and "【ポイント】" in recs[0].item.body


# ---- 10-05 复审 R04 · 正文混入全站导航 ---------------------------------------

def test_strip_page_extracts_contents_container_from_live_page():
    """真实详情页上 `id="contents"` 容器必须能取到正文段, 且不含全站导航/版权。"""
    html_text = _read_bytes("mofa_detail_T090_live.html").decode("utf-8")
    body = ma._strip_page(html_text)
    assert "【ポイント】" in body
    assert "危険レベル" in body
    for junk in ("Copyright ©", "サイトマップ", "お問い合わせ", "Facebook\n友だち追加"):
        assert junk not in body, f"正文里混进了全站导航/版权: {junk}"


def test_strip_page_no_leading_attribute_fragment():
    """修复前整页兜底会留下 `\">` 这种属性残片(只按行 strip 去不掉)。"""
    body = ma._strip_page(_read("mofa_detail_C045.html"))
    assert not body.startswith('">')
    assert "【ポイント】" in body


# ---- 10-05 复审 R05 · pubDate 无时区 ----------------------------------------

def test_parse_rfc822_without_tz_uses_site_timezone(monkeypatch):
    """无时区的 pubDate 按站点所在地时区(JST)解释, 不随宿主 TZ 漂移。

    修复前: naive datetime 直接 `.astimezone(utc)` 按宿主时区算,
    `TZ=Asia/Tokyo` 下 18:53 变成 09:53Z(差 9 小时), 影响 `ts < cutoff` 的窗口判定。
    """
    monkeypatch.setenv("TZ", "America/New_York")
    import time as _time
    _time.tzset()
    try:
        assert ma._parse_rfc822("Mon, 28 Sep 2026 18:53:21") == datetime(
            2026, 9, 28, 9, 53, 21, tzinfo=timezone.utc)
    finally:
        monkeypatch.delenv("TZ", raising=False)
        _time.tzset()
    # 带时区的照旧
    assert ma._parse_rfc822("Mon, 28 Sep 2026 18:53:21 +0900") == datetime(
        2026, 9, 28, 9, 53, 21, tzinfo=timezone.utc)
    assert ma._parse_rfc822("garbage") is None


# ---- 10-05 复审 R06 · スポット情報被静默标成広域 -----------------------------

def test_non_wide_category_without_country_not_labelled_wide(tmp_path):
    """非 `広域情報` 类别且猜不出国名时不得落 `mofa_anzen:広域`。

    修复前: `regions = [WIDE_AREA] if wide else ([country] if country else [WIDE_AREA])`
    的三元回落把"没国名"和"広域情報"混同 → `スポット情報` 落进广域桶。
    """
    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>ネパール中部で発生した大規模な洪水について</title>
    <link>https://www.anzen.mofa.go.jp/info/pcspotinfo_2026C040.html</link>
    <description>ネパール中部で発生した大規模な洪水について</description>
    <category>スポット情報</category>
    <pubDate>Mon, 28 Sep 2026 18:53:21 +0900</pubDate></item>
    </channel></rss>"""
    recs = list(_adapter(tmp_path, fetch_rss=lambda: rss, fetch_detail=lambda k: "",
                         fetch_page=lambda url: _read("mofa_detail_C045.html")).collect())
    assert len(recs) == 1
    assert recs[0].item.source == f"mofa_anzen:{ma.UNKNOWN_AREA}"
    assert recs[0].item.source != "mofa_anzen:広域"
    # 抽不出就不硬猜: regions 留空, 不塞假地名
    assert json.loads(recs[0].source_payload_json)["regions"] == []


def test_wide_area_category_still_labelled_wide(tmp_path):
    """阳性对照: 真的 `広域情報` 仍然落 `mofa_anzen:広域`。"""
    recs = list(_adapter(tmp_path, fetch_rss=lambda: _read("mofa_news.xml"),
                         fetch_detail=lambda k: _read("mofa_mail_C042.xml")).collect())
    wide = [r for r in recs if "pcwideareaspecificinfo" in r.item.url]
    assert len(wide) == 1
    assert wide[0].item.source == "mofa_anzen:広域"
    assert json.loads(wide[0].source_payload_json)["regions"] == ["広域"]


# ---- naive since 不依赖宿主 TZ ----------------------------------------------

def test_collect_naive_since_uses_jst(monkeypatch, tmp_path):
    """`cli.py --since 2026-09-01` 是 naive datetime, 必须按 JST 解释而非宿主时区。

    修复前: `naive.astimezone(utc)` 按宿主时区解释, 同一命令在 `TZ=UTC` 与
    `TZ=Asia/Tokyo` 下 cutoff 差 9 小时。这里用一条卡在边界上的条目钉住结果:
    它的 ts 折 UTC 是 08-31T15:00Z, 只有按 JST 解释 `--since 2026-09-01` 才在窗外。
    """
    import time as _time

    def fetch_page(url):
        return _read("mofa_detail_C045.html")

    # 钉住「不依赖宿主 TZ」: 两个宿主时区下结果必须一致。
    results = {}
    for tz in ("UTC", "Asia/Tokyo", "America/New_York"):
        monkeypatch.setenv("TZ", tz)
        _time.tzset()
        results[tz] = [
            r.item.url for r in _adapter(tmp_path / tz.replace("/", "_"),
                                         fetch_rss=_boundary_rss,
                                         fetch_detail=lambda k: "",
                                         fetch_page=fetch_page
                                         ).collect(since=datetime(2026, 9, 20))]
    monkeypatch.delenv("TZ", raising=False)
    _time.tzset()

    assert results["UTC"] == results["Asia/Tokyo"] == results["America/New_York"], \
        "naive since 的解释随宿主 TZ 漂移了"
    # 按 JST 解释 cutoff = 09-19T15:00Z, 条目 20:00Z 在窗内 -> 必须产出
    assert len(results["UTC"]) == 1, results
    # 阳性对照: since 放到 09-25 -> 条目在窗外
    assert list(_adapter(tmp_path / "outside", fetch_rss=_boundary_rss,
                         fetch_detail=lambda k: "", fetch_page=fetch_page
                         ).collect(since=datetime(2026, 9, 25))) == []


def _boundary_rss() -> str:
    """pubDate = 2026-09-19T20:00Z(= JST 09-20 05:00)。

    卡在两种解释的 cutoff 之间: `--since 2026-09-20` 按 **JST** 解释 → cutoff 09-19T15:00Z,
    条目(20:00Z)仍在窗内; 按 **宿主本地时区**解释(UTC 机器)→ cutoff 09-20T00:00Z,
    条目已在窗外。所以这个值能区分"按 JST 解释"与"按宿主 TZ 解释"。
    """
    return """<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>テスト国における危険情報【危険レベル2】</title>
    <link>https://www.anzen.mofa.go.jp/info/pchazardspecificinfo_2026T998.html</link>
    <description>テスト国における危険情報【危険レベル2】</description><category>危険情報</category>
    <pubDate>Sun, 20 Sep 2026 05:00:00 +0900</pubDate></item>
    </channel></rss>"""
