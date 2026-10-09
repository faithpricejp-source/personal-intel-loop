"""enso_status: 两个官方机构各一条, **发布日期不变就不产出** —— 这是本 adapter 存在的理由。

阴性断言都配阳性对照: "0 条" 既可能是正确去重, 也可能是抓取坏了, 两者产出一模一样。
fixture 是 2026-10-04 实抓的 ensodisc.shtml / kanshi_joho1.html 片段。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

from personal_intel_loop.adapters import enso_status as es
from personal_intel_loop.adapters.enso_status import EnsoStatusAdapter

FIX = Path(__file__).parent / "fixtures" / "risk"
NOAA_HTML = (FIX / "noaa_ensodisc.html").read_text("utf-8")
JMA_HTML = (FIX / "jma_kanshi.html").read_text("utf-8")
#: 2026-10-05 实抓的**原始字节**(编码缺陷 R15 只在走解码路径时才暴露)。
NOAA_LIVE_BYTES = (FIX / "noaa_ensodisc_live.html").read_bytes()
JMA_LIVE_BYTES = (FIX / "jma_kanshi_live.html").read_bytes()


def _fake_response(content: bytes, content_type: str = "text/html") -> requests.Response:
    """构造真实形状的 `requests.Response`(不联网)。

    必须照 `requests.Session.send` 的做法按响应头设 `r.encoding`: 真实请求里
    `Content-Type: text/html`(无 charset) 会让它变成 `ISO-8859-1`, 而 NOAA 的响应头
    **带** `charset=utf-8`(requests 已正确设好) —— 两者都是 R15 的触发条件。
    """
    r = requests.Response()
    r.status_code = 200
    r._content = content
    r.headers["Content-Type"] = content_type
    r.encoding = requests.utils.get_encoding_from_headers(r.headers)
    return r


def _adapter(tmp_path, **kw):
    kw.setdefault("fetch_noaa", lambda: "")
    kw.setdefault("fetch_jma", lambda: "")
    state = tmp_path if tmp_path.name == "state.json" else tmp_path / "state.json"
    return EnsoStatusAdapter(state_path=state, **kw)


def _both(tmp_path, noaa=NOAA_HTML, jma=JMA_HTML, **kw):
    return _adapter(tmp_path, fetch_noaa=lambda: noaa, fetch_jma=lambda: jma, **kw)


# ---- NOAA 解析 -------------------------------------------------------------

def test_parse_noaa():
    parsed = es.parse_noaa(NOAA_HTML)
    # 正文里还有 "next ... scheduled for 8 October 2026" —— 不能拿它当本期日期
    assert parsed["published"] == datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert parsed["status"] == "El Niño Advisory"
    assert parsed["synopsis"].startswith("El Niño is strengthening, with a greater than 90%")
    assert "sea surface temperature anomalies" in parsed["body"]
    # "This discussion is a consolidated effort" 之后是版权块, 不收
    assert "consolidated effort" not in parsed["body"]
    assert "<font" not in parsed["body"]


def test_parse_noaa_broken_returns_empty():
    assert es.parse_noaa("") == {}
    assert es.parse_noaa("<html><body>maintenance</body></html>") == {}
    # 状态块没了 -> 抽不出, 返回 {} 而不是抛
    assert es.parse_noaa("10 September 2026") == {}


def test_parse_noaa_ignores_next_issue_date():
    """把本期日期抹掉后, 剩下的唯一日期是 "scheduled for 8 October 2026" —— 不能被当本期。"""
    stripped = NOAA_HTML.replace("10 September 2026", "", 1)
    parsed = es.parse_noaa(stripped)
    assert parsed.get("published") != datetime(2026, 10, 8, tzinfo=timezone.utc)


# ---- 気象庁 解析 ------------------------------------------------------------

def test_parse_jma():
    parsed = es.parse_jma(JMA_HTML)
    # 页面写「令和8年9月9日」→ 2026-09-09
    assert parsed["published"] == datetime(2026, 9, 9, tzinfo=timezone.utc)
    assert parsed["number"] == "No.408"
    assert parsed["synopsis"] == "2026年春からエルニーニョ現象が続いているとみられる。"
    assert parsed["status"] == "今後、冬にかけてエルニーニョ現象が続く見込み（100％）。"
    assert "エルニーニョ監視指数" in parsed["body"]


def test_parse_jma_broken_returns_empty():
    assert es.parse_jma("") == {}
    assert es.parse_jma("<html>404</html>") == {}


def test_parse_jma_reiwa_conversion():
    page = JMA_HTML.replace("令和8年9月9日", "令和1年1月1日")
    assert es.parse_jma(page)["published"] == datetime(2019, 1, 1, tzinfo=timezone.utc)


# ---- collect ---------------------------------------------------------------

def test_collect_two_orgs(tmp_path):
    recs = list(_both(tmp_path).collect())
    assert len(recs) == 2
    by_source = {r.item.source: r for r in recs}
    assert set(by_source) == {"enso_status:noaa", "enso_status:jma"}

    noaa = by_source["enso_status:noaa"]
    assert noaa.item.lang == "en"
    assert noaa.item.ts == datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert noaa.item.title == (
        "NOAA ENSO 状态 2026-09：El Niño is strengthening, with a greater than 90% chance "
        "of a very strong event during the Northern Hemisphere fall and winter 2026-27.")
    assert noaa.item.url == es.NOAA_URL

    payload = json.loads(noaa.source_payload_json)
    assert payload["kind"] == "risk"
    assert payload["issuer"] == es.ISSUER_NOAA
    assert payload["published"] == "2026-09-10T09:00:00+09:00"
    assert payload["regions"] == []
    assert payload["level"] == "El Niño Advisory"
    assert payload["published_date"] == "2026-09-10"

    jma = by_source["enso_status:jma"]
    assert jma.item.lang == "ja"
    assert jma.item.title.startswith("気象庁 ENSO 状态 2026-09：2026年春からエルニーニョ現象が続いているとみられる。")
    assert json.loads(jma.source_payload_json)["level"] == "今後、冬にかけてエルニーニョ現象が続く見込み（100％）。"


def test_collect_same_publish_date_no_output(tmp_path):
    """同一期不重复产出: 第二次跑 state 里已有日期 -> 0 条。"""
    adapter = _both(tmp_path)
    assert len(list(adapter.collect())) == 2
    again = _both(tmp_path)
    assert list(again.collect()) == []


def test_collect_changed_publish_date_outputs(tmp_path):
    """发布日变了 -> 产出一条新的。"""
    first = _both(tmp_path)
    assert len(list(first.collect())) == 2

    newer = NOAA_HTML.replace("10 September 2026", "8 October 2026", 1)
    second = _both(tmp_path, noaa=newer)
    recs = list(second.collect())
    assert len(recs) == 1                       # 只有 NOAA 变了, JMA 那条被去重
    assert recs[0].item.source == "enso_status:noaa"
    assert json.loads(recs[0].source_payload_json)["published_date"] == "2026-10-08"
    # 再跑一次 -> 0 条
    assert list(_both(tmp_path, noaa=newer).collect()) == []


def test_collect_time_window(tmp_path):
    """days 缺省 400(见文件头): 9 月发布的当期通报仍在窗内。"""
    assert len(list(_both(tmp_path).collect())) == 2
    # since 卡在 9 月 10 日 -> NOAA(09-10) 在窗内, JMA(09-09) 出窗
    later = list(_both(tmp_path / "b").collect(
        since=datetime(2026, 9, 10, tzinfo=timezone.utc)))
    assert [r.item.source for r in later] == ["enso_status:noaa"]
    # 再往后卡 -> 两条都出窗
    assert list(_both(tmp_path / "c").collect(
        since=datetime(2026, 9, 11, tzinfo=timezone.utc))) == []


def test_collect_fetch_failure_returns_empty_not_raise(tmp_path):
    assert list(_both(tmp_path, noaa="", jma="").collect()) == []
    assert list(_both(tmp_path, noaa="<html>503</html>",
                     jma="<html>503</html>").collect()) == []
    # 一边挂一边好: 好的那半边照样产出
    recs = list(_both(tmp_path / "half", noaa="", jma=JMA_HTML).collect())
    assert [r.item.source for r in recs] == ["enso_status:jma"]


def test_collect_limit(tmp_path):
    assert len(list(_both(tmp_path).collect(limit=1))) == 1


def test_state_broken_file_not_raise(tmp_path):
    bad = tmp_path / "state.json"
    bad.write_text("{not json", "utf-8")
    assert len(list(_both(bad).collect())) == 2


def test_sorted_newest_first(tmp_path):
    recs = list(_both(tmp_path).collect())
    assert [r.item.ts for r in recs] == sorted((r.item.ts for r in recs), reverse=True)


# ---- 10-05 复审 R14 · limit 截断在 state 标记之前(后果最重) -------------------

def test_collect_limit_does_not_mark_dropped_org_as_produced(tmp_path):
    """`limit=1` 落选的机构**不进 state** —— 下一轮必须还能产出它这一期。

    这里的 state 语义是「这一期我已经产出过了」, 所以丢一条不是丢一天, 而是**丢一个月**:
    被标记后要等官方下个月出新通报才会有新条目。原来 `last[org] = stamp` 早于
    `records.append`、`_save_state` 又早于 `records[:limit]` 截断, 所以 `limit=1` 时
    另一个机构的这一期被永久标记成「已产出」却从未返回。
    """
    first = list(_both(tmp_path).collect(limit=1))
    assert len(first) == 1
    assert first[0].item.source == "enso_status:noaa"     # 09-10 比 09-09 新

    # 落选那一机构没进 state -> 第二轮还能拿到
    second = list(_both(tmp_path).collect())
    assert [r.item.source for r in second] == ["enso_status:jma"]

    # 第三轮: 两个机构的当期都已产出 -> 0 条(阳性对照)
    assert list(_both(tmp_path).collect()) == []


def test_collect_limit_state_only_contains_returned_orgs(tmp_path):
    """state 里只该有实际返回的机构。"""
    list(_both(tmp_path).collect(limit=1))
    assert es._load_state(tmp_path / "state.json") == {"noaa": "2026-09-10"}


def test_collect_single_org_failure_still_records_other(tmp_path):
    """对照: 单源失败那轮只写成功那家的 state, 恢复后能补上(原行为不变)。"""
    recs = list(_both(tmp_path / "half", noaa="", jma=JMA_HTML).collect())
    assert [r.item.source for r in recs] == ["enso_status:jma"]
    assert es._load_state(tmp_path / "half" / "state.json") == {"jma": "2026-09-09"}
    recovered = list(_both(tmp_path / "half", noaa=NOAA_HTML, jma=JMA_HTML).collect())
    assert [r.item.source for r in recovered] == ["enso_status:noaa"]


# ---- 10-05 复审 R15 · apparent_encoding 覆盖正确 charset ---------------------

def test_get_does_not_override_correct_header_charset(monkeypatch):
    """NOAA 响应头已给 `charset=utf-8`, 不许被 `apparent_encoding` 覆盖。

    修复前 `r.encoding = r.apparent_encoding or "utf-8"` 是无条件覆盖, 实测这一行把正确的头
    猜成 `Windows-1252` 顶掉 —— 只因页面恰好纯 ASCII 才没出事。
    """
    resp = _fake_response(NOAA_LIVE_BYTES, "text/html; charset=utf-8")
    assert resp.encoding == "utf-8"
    monkeypatch.setattr(es.requests, "get", lambda *a, **k: resp)
    html_text = es._get(es.NOAA_URL)
    assert "ENSO Alert System Status" in html_text
    assert es.parse_noaa(html_text)["published"] == datetime(2026, 9, 10, tzinfo=timezone.utc)


def test_get_decodes_jma_utf8_without_header_charset(monkeypatch):
    """JMA 头无 charset(requests 退成 ISO-8859-1), 必须按 `meta charset` 解 UTF-8。

    实测该页有 8364 个非 ASCII 字节、`meta charset` 写 UTF-8; 按 latin-1 解会全乱码。
    """
    raw = JMA_LIVE_BYTES
    assert any(b > 0x7F for b in raw), "本测试的前提: JMA 页面确有非 ASCII 字节"
    resp = _fake_response(raw, "text/html")
    assert resp.encoding == "ISO-8859-1"
    monkeypatch.setattr(es.requests, "get", lambda *a, **k: resp)
    html_text = es._get(es.JMA_URL)
    assert "令和8年9月9日" in html_text
    # 真正的 latin-1 乱码形态(`気象庁` 的 UTF-8 字节按 latin-1 解出来的样子)
    mojibake = "気象庁".encode("utf-8").decode("latin-1")
    assert mojibake not in html_text
    assert es.parse_jma(html_text)["published"] == datetime(2026, 9, 9, tzinfo=timezone.utc)


def test_decode_prefers_meta_over_header():
    """meta charset 与响应头矛盾时以 meta 为准(实测 NOAA 两者本身就矛盾)。"""
    raw = "テスト".encode("utf-8")
    assert es._decode(b'<meta charset="windows-1252">' + raw, "ISO-8859-1") != ""
    assert es._decode(raw, "ISO-8859-1") == "テスト"     # latin-1 兜底要被剔除


def test_get_decodes_non_ascii_noaa_page_as_utf8(monkeypatch):
    """NOAA 页面一旦出现非 ASCII 原文, 覆盖成 Windows-1252 就变乱码。

    真实 NOAA 页恰好是纯 ASCII(`ñ` 写成 `Ni&ntilde;o` 实体, 实测非 ASCII 字节数 = 0), 所以
    `apparent_encoding` 猜错这一直**侥幸无害**。这条把那个实体换成真 UTF-8 字节, 把潜在
    风险变成可观测的故障: 修复前 `r.encoding = r.apparent_encoding`(实测猜成
    `Windows-1252`, 还覆盖掉响应头里已经正确的 `utf-8`)会把它解成 `NiÃ±o`。
    注意 NOAA 的 `meta charset` 写 `windows-1252` 而响应头给 `utf-8`, **两者本身矛盾**,
    字节其实是 UTF-8 —— 所以正确路径是跳过那个单字节编码。
    """
    raw = NOAA_LIVE_BYTES.replace(b"Ni&ntilde;o Advisory",
                                  "Niño Advisory".encode("utf-8"))
    assert b"\xc3\xb1" in raw, "本测试的前提: 页面已含非 ASCII 的 UTF-8 字节(ñ = C3 B1)"
    resp = _fake_response(raw, "text/html; charset=utf-8")
    monkeypatch.setattr(es.requests, "get", lambda *a, **k: resp)
    html_text = es._get(es.NOAA_URL)
    assert "Niño Advisory" in html_text or "Niño" in html_text, \
        f"非 ASCII 原文被解坏了: {html_text[html_text.find('ENSO Alert'):][:120]!r}"
    assert "Ã±" not in html_text


# ---- 10-05 复审 R16 · 下期日期的排除靠固定宽度 lookbehind -------------------

def test_parse_noaa_ignores_next_issue_date_with_newline():
    """`scheduled for` 与下期日期之间隔着换行时也必须挡住。

    修复前是固定宽度 lookbehind `(?<!scheduled for )`, 只在紧邻时生效; 换行分隔就漏 →
    下期日期被当本期采信 → `published` 变未来日期, `stamp` 变未来值, `last["noaa"]` 被写成
    未来日期, 当期条目 `ts` 落在未来。
    """
    page = NOAA_HTML.replace("10 September 2026", "", 1).replace(
        "scheduled for 8 October 2026", "scheduled for\n8 October 2026")
    parsed = es.parse_noaa(page)
    assert parsed.get("published") != datetime(2026, 10, 8, tzinfo=timezone.utc)


def test_parse_noaa_ignores_next_issue_date_with_tag():
    """`scheduled for <b>8 October 2026</b>` 这种标签分隔也必须挡住。"""
    page = NOAA_HTML.replace("10 September 2026", "", 1).replace(
        "scheduled for 8 October 2026", "scheduled for <b>8 October 2026</b>")
    parsed = es.parse_noaa(page)
    assert parsed.get("published") != datetime(2026, 10, 8, tzinfo=timezone.utc)


def test_parse_noaa_returns_current_date_not_next_one():
    """方向要钉死: 不只「不等于下期日期」, 而「等于本期日期」(否则返回 {} 也会蒙混过关)。"""
    assert es.parse_noaa(NOAA_HTML)["published"] == datetime(2026, 9, 10, tzinfo=timezone.utc)
    # 变形后的页面也必须仍取到本期日期
    for replacement in ("scheduled for\n8 October 2026", "scheduled for <b>8 October 2026</b>"):
        page = NOAA_HTML.replace("scheduled for 8 October 2026", replacement)
        assert es.parse_noaa(page)["published"] == datetime(2026, 9, 10, tzinfo=timezone.utc)


def test_collect_noaa_future_date_not_written_to_state(tmp_path):
    """下期日期被误采信时不该把未来日期写进 state(库里会出现 future item)。"""
    page = NOAA_HTML.replace("10 September 2026", "", 1).replace(
        "scheduled for 8 October 2026", "scheduled for<br>8 October 2026")
    recs = list(_both(tmp_path, noaa=page).collect())
    for r in recs:
        assert r.item.ts <= datetime.now(timezone.utc), "产出了未来时间的条目"
    assert es._load_state(tmp_path / "state.json").get("noaa") != "2026-10-08"


# ---- naive since 不依赖宿主 TZ ----------------------------------------------

def test_collect_naive_since_is_tz_independent(monkeypatch, tmp_path):
    """naive `--since` 的解释不能随宿主 TZ 变化。"""
    import time as _time

    results = {}
    for tz in ("UTC", "Asia/Tokyo", "America/New_York"):
        monkeypatch.setenv("TZ", tz)
        _time.tzset()
        results[tz] = [r.item.source for r in
                       _both(tmp_path / tz.replace("/", "_")).collect(
                           since=datetime(2026, 9, 10))]
    monkeypatch.delenv("TZ", raising=False)
    _time.tzset()
    assert results["UTC"] == results["Asia/Tokyo"] == results["America/New_York"], \
        f"naive since 的解释随宿主 TZ 漂移: {results}"
    assert results["UTC"] == ["enso_status:noaa"]   # 阳性对照
