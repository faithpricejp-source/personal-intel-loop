"""tokyo_events: 5 个源各自解析、年份推断(含跨年)、去重、窗口过滤、零匹配计数。

fixture 是 2026-10-05 真网抓下来的**原始字节**(`out/fetch_sources.py` 跑的, 未做任何裁剪),
所以测试里的条数就是当时的真实条数: ntj 87 / tnm 3 / t_bunka 28 / nact 9 / eiseibunko 4。

所有测试都把 `today` 钉在 2026-10-05, 所以「60 天窗口」的结果稳定可复现。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from personal_intel_loop.adapters import tokyo_events as te

FIX = Path(__file__).parent / "fixtures" / "tokyo_events"

TODAY = date(2026, 10, 5)
NOW = datetime(2026, 10, 5, 4, 0, 0, tzinfo=timezone.utc)

FIXTURE_FILES = {
    "ntj": "ntj_api.json",
    "tnm": "tnm_cid1.xml",
    "t_bunka": "t_bunka_stage.html",
    "nact": "nact_exhibition.html",
    "eiseibunko": "eiseibunko_exhibition.html",
}

#: 2026-10-05 实测: 5 源共 131 条(87+3+28+9+4)
RAW_COUNTS = {"ntj": 87, "tnm": 3, "t_bunka": 28, "nact": 9, "eiseibunko": 4}


def raw(venue_key: str) -> bytes:
    return (FIX / FIXTURE_FILES[venue_key]).read_bytes()


def _adapter(tmp_path, *, blobs=None, today=TODAY, clock=None, **kw):
    blobs = blobs if blobs is not None else {k: raw(k) for k in FIXTURE_FILES}
    state = tmp_path if tmp_path.name.endswith(".json") else tmp_path / "state.json"
    return te.TokyoEventsAdapter(
        fetch=lambda s: blobs.get(s.venue_key, b""),
        state_path=state,
        today=today,
        clock=clock or (lambda: NOW),
        **kw,
    )


# ---- 源 1: 国立劇場 JSON ----------------------------------------------------

def test_ntj_parses_all_87_rows():
    out = te.parse_ntj_api(raw("ntj"), TODAY)
    assert len(out) == RAW_COUNTS["ntj"] == 87


def test_ntj_field_mapping():
    first = te.parse_ntj_api(raw("ntj"), TODAY)[0]
    assert first["title"] == "令和8年10月歌舞伎公演"
    # url 是站内相对路径, 必须拼成绝对地址
    assert first["url"] == "https://www.ntj.jac.go.jp/schedule/kokuritsu_l/2026/0810/"
    assert first["start_date"] == "2026-10-01"
    assert first["end_date"] == "2026-10-20"
    assert "国立劇場" in first["venue"] and "他劇場" in first["venue"]
    assert first["genre"] == "歌舞伎"


def test_ntj_month_only_dates_get_year_from_year_field():
    """start_date/end_date 实测是「10月1日」这种没有年份的月日 —— 年份来自同行 `year`。"""
    out = te.parse_ntj_api(raw("ntj"), TODAY)
    assert all(e["start_date"].startswith("2026-") for e in out)
    assert all(len(e["start_date"]) == 10 for e in out), "日期必须归一成 ISO"


def test_ntj_without_year_field_falls_back_to_inference():
    """`year` 字段缺失时退回「结束日不早于今天」的推断。"""
    rows = {"rows": [
        {"title": "テスト公演A", "theatre_name": "国立能楽堂", "genre": "能・狂言",
         "start_date": "11月3日", "end_date": "11月5日",
         "url": "schedule/nou/2026/1/"},
        {"title": "テスト公演B", "theatre_name": "国立能楽堂", "genre": "能・狂言",
         "start_date": "9月1日", "end_date": "9月10日",
         "url": "schedule/nou/2026/2/"},
    ]}
    out = te.parse_ntj_api(json.dumps(rows), TODAY)
    assert [(e["start_date"], e["end_date"]) for e in out] == [
        ("2026-11-03", "2026-11-05"),   # 今年 11 月, 直接用
        ("2027-09-01", "2027-09-10"),   # 9 月已过 -> 推到明年
    ]


def test_ntj_broken_payload_returns_empty():
    assert te.parse_ntj_api(b"", TODAY) == []
    assert te.parse_ntj_api(b"<html>503</html>", TODAY) == []
    assert te.parse_ntj_api('{"count":0}', TODAY) == []
    assert te.parse_ntj_api('{"rows": "not a list"}', TODAY) == []
    assert te.parse_ntj_api("{broken", TODAY) == []


# ---- 源 2: 東京国立博物館 RSS ------------------------------------------------

def test_tnm_parses_3_items():
    assert len(te.parse_tnm_feed(raw("tnm"), TODAY)) == RAW_COUNTS["tnm"] == 3


def test_tnm_title_tags_stripped():
    """RSS 的 <title> 里 `<br />` 是**未转义的裸标签**(feedparser 也不处理)。"""
    first = te.parse_tnm_feed(raw("tnm"), TODAY)[0]
    assert "<br" not in first["title"]
    assert "歌川広重" in first["title"]


def test_tnm_span_comes_from_tail_not_phase_schedule():
    """summary 正文里有「1期：9月29日～10月25日」这种**不带年份**的分期日程, 不能被当成会期。"""
    first = te.parse_tnm_feed(raw("tnm"), TODAY)[0]
    assert first["start_date"] == "2026-09-29"
    assert first["end_date"] == "2026-12-20"     # 不是 1 期的 10-25
    assert first["venue"].endswith("本館 A室")


def test_tnm_cross_year_item():
    """第 3 条会期落到 2027 年 —— 跨年不能被当成今年。"""
    items = te.parse_tnm_feed(raw("tnm"), TODAY)
    genji = [e for e in items if "源氏物語" in e["title"]][0]
    assert genji["start_date"] == "2027-01-19"
    assert genji["end_date"] == "2027-03-14"


def test_tnm_single_date_fallback():
    body = "前置きです。<br />2026年11月3日"
    found = te._tnm_span(body, TODAY)
    assert found is not None
    span, _ = found
    assert span[0] == span[1] == date(2026, 11, 3)


def test_tnm_broken_feed_returns_empty():
    assert te.parse_tnm_feed(b"", TODAY) == []
    assert te.parse_tnm_feed(b"<html>403 Forbidden</html>", TODAY) == []


# ---- 源 3: 東京文化会館 SSR HTML ---------------------------------------------

def test_tbunka_parses_28_rows():
    """实测 28 行, 其中只有 20 行自带日期单元格(其余靠 rowspan 继承)。"""
    assert len(te.parse_tbunka_html(raw("t_bunka"), TODAY)) == RAW_COUNTS["t_bunka"] == 28


def test_tbunka_date_comes_from_three_spans():
    """日期被拆成 yea/day/week 三个 span, 不能直接搜「2026年10月9日」连续串。"""
    first = te.parse_tbunka_html(raw("t_bunka"), TODAY)[0]
    assert first["start_date"] == "2026-10-09"
    assert first["end_date"] == "2026-10-09"        # 单日粒度
    assert first["url"] == "https://www.t-bunka.jp/stage/32954/"


def test_tbunka_rowspan_rows_inherit_previous_date():
    """`rowspan=2|3` 的续行**没有**日期单元格, 必须继承上一行 —— 少了会静默丢 8 场。"""
    out = te.parse_tbunka_html(raw("t_bunka"), TODAY)
    by_url = {e["url"]: e for e in out}
    # 实测这 8 行的 <tr> 里没有 class="date_row"(上面一行是 10月25日 的 rowspan=2 那组)
    for url, expect in (("https://www.t-bunka.jp/stage/33109/", "2026-10-25"),
                        ("https://www.t-bunka.jp/stage/33120/", "2026-11-07"),
                        ("https://www.t-bunka.jp/stage/33015/", "2026-11-27")):
        assert by_url[url]["start_date"] == expect, url
    # 阳性对照: 34064 自己带日期单元格, 不依赖继承
    assert by_url["https://www.t-bunka.jp/stage/34064/"]["start_date"] == "2026-11-21"


def test_tbunka_all_28_rows_have_dates():
    """继承逻辑生效的标志: 28 行全部拿到日期, 没有一条 start_date 为空。"""
    out = te.parse_tbunka_html(raw("t_bunka"), TODAY)
    assert len(out) == 28
    assert all(e["start_date"] for e in out)
    assert all(e["end_date"] == e["start_date"] for e in out)   # 单日粒度


def test_tbunka_venue_is_actual_hall_not_the_venue_itself():
    """`まちなかコンサート` 的实际场地是别的馆, 后面还跟着 ※ 提示和（アクセス）尾巴。"""
    first = te.parse_tbunka_html(raw("t_bunka"), TODAY)[0]
    assert first["venue"] == "国立西洋美術館 本館1階ロビー"
    assert "※" not in first["venue"]
    assert "アクセス" not in first["venue"]


def test_tbunka_same_title_different_dates_are_distinct():
    """同一个「まちなかコンサート」一周内重复多条 —— 按日期区分, 不按标题去重。"""
    out = te.parse_tbunka_html(raw("t_bunka"), TODAY)
    machinaka = [e for e in out if e["title"].startswith("まちなかコンサート")]
    assert len(machinaka) >= 3
    assert len({e["start_date"] for e in machinaka}) == len(machinaka)


def test_tbunka_genre_filters_out_mujinushi_jigyo():
    out = te.parse_tbunka_html(raw("t_bunka"), TODAY)
    assert all("主催事業" not in e["genre"] for e in out)
    assert "コンサート" in out[0]["genre"]


def test_tbunka_broken_html_returns_empty():
    assert te.parse_tbunka_html(b"", TODAY) == []
    assert te.parse_tbunka_html(b"<html><body>503</body></html>", TODAY) == []


# ---- 源 4: 国立新美術館 SSR HTML --------------------------------------------

def test_nact_parses_9_items_across_three_sections():
    out = te.parse_nact_html(raw("nact"), TODAY)
    assert len(out) == RAW_COUNTS["nact"] == 9
    assert {e["genre"] for e in out} == {"企画展", "公募展", "イベント"}


def test_nact_time_datetime_is_iso():
    first = te.parse_nact_html(raw("nact"), TODAY)[0]
    assert first["start_date"] == "2026-09-09"
    assert first["end_date"] == "2026-12-13"
    assert first["url"] == "https://www.nact.jp/exhibition_special/2026/louvre2026/"


def test_nact_keeps_upcoming_exhibition():
    """「開催予定」正是日报最想要的先行信息, 不能只取開催中。"""
    out = te.parse_nact_html(raw("nact"), TODAY)
    upcoming = [e for e in out if e["title"].startswith("少女漫画")]
    assert upcoming and upcoming[0]["start_date"] == "2026-10-28"
    assert upcoming[0]["end_date"] == "2027-02-08"      # 跨年
    assert "開催予定" in upcoming[0]["summary"]


def test_nact_public_call_exhibits_use_anchor_urls():
    out = te.parse_nact_html(raw("nact"), TODAY)
    public = [e for e in out if e["genre"] == "公募展"]
    assert len(public) == 3
    assert all(e["url"].endswith(tuple(f"#0064{n}" for n in (14, 15, 16))) for e in public)


def test_nact_event_uses_only_first_ex_date():
    """活动段的 ex_date2 是**另一个区间**(实测 006605 的 ex_date2 是 08-20～09-13)。"""
    out = te.parse_nact_html(raw("nact"), TODAY)
    by_url = {e["url"]: e for e in out}
    assert by_url["https://www.nact.jp/event/2026/006605.html"]["start_date"] == "2026-10-04"
    two = by_url["https://www.nact.jp/event/2026/006622.html"]
    assert (two["start_date"], two["end_date"]) == ("2026-10-30", "2026-11-16")


def test_nact_broken_html_returns_empty():
    assert te.parse_nact_html(b"", TODAY) == []
    assert te.parse_nact_html(b"<html><body>404</body></html>", TODAY) == []


# ---- 源 5: 永青文庫 SSR HTML ------------------------------------------------

def test_eiseibunko_parses_4_quarters():
    out = te.parse_eiseibunko_html(raw("eiseibunko"), TODAY)
    assert len(out) == RAW_COUNTS["eiseibunko"] == 4
    assert [e["title"][:3] for e in out] == ["春季展", "夏季展", "秋季展", "早春展"]


def test_eiseibunko_span_survives_br_and_ideographic_spaces():
    """会期被 `<br>` + 一堆全角空格隔断, 不预处理就一条都抽不到。"""
    got = {e["title"][:3]: (e["start_date"], e["end_date"])
           for e in te.parse_eiseibunko_html(raw("eiseibunko"), TODAY)}
    assert got["春季展"] == ("2026-04-11", "2026-06-07")
    assert got["夏季展"] == ("2026-07-11", "2026-09-06")


def test_eiseibunko_next_year_exhibition():
    """早春展会期落到 2027 年, 结束日「4月11日」没有年份, 要从起始日继承。"""
    out = te.parse_eiseibunko_html(raw("eiseibunko"), TODAY)
    fuyu = [e for e in out if e["title"].startswith("早春展")][0]
    assert fuyu["start_date"] == "2027-01-16"
    assert fuyu["end_date"] == "2027-04-11"


def test_eiseibunko_cross_year_span_rolls_end_into_next_year():
    """12月～1月的会期: 结束日落次年, 不能同年。"""
    assert te._eisei_span("2026年12月20日（日）　～1月5日（火）", TODAY) == (
        date(2026, 12, 20), date(2027, 1, 5),
    )
    # 阳性对照: 同年内不推
    assert te._eisei_span("2026年4月11日（土）～6月7日（日）", TODAY) == (
        date(2026, 4, 11), date(2026, 6, 7),
    )
    # 结束日早于起始日且跨过去也不合理(跨度 > 一年) -> None(交给窗口滤掉, 不硬编)
    assert te._eisei_span("2026年12月20日（日）　～12月1日（火）", TODAY) is None


def test_eiseibunko_title_is_single_line():
    """秋季展标题在 <strong> 里跨两行, Item.title 必须是单行。"""
    out = te.parse_eiseibunko_html(raw("eiseibunko"), TODAY)
    assert all("\n" not in e["title"] for e in out)
    aki = [e for e in out if e["title"].startswith("秋季展")][0]
    assert "白隠ワールド" in aki["title"]


def test_eiseibunko_summary_extracted():
    out = te.parse_eiseibunko_html(raw("eiseibunko"), TODAY)
    assert all(len(e["summary"]) > 30 for e in out)


def test_eiseibunko_broken_html_returns_empty():
    assert te.parse_eiseibunko_html(b"", TODAY) == []
    assert te.parse_eiseibunko_html(b"<html>404</html>", TODAY) == []


# ---- 编码 ------------------------------------------------------------------

def test_decode_ignores_lying_content_type_charset():
    """eiseibunko 响应头写 ISO-8859-1, 但 meta 与正文都是 utf-8 —— 用 r.text 会出乱码。"""
    body = raw("eiseibunko")
    assert b'charset=shift_jis' not in body[:4000].lower()
    text = te._decode(body)
    assert "永青文庫" in text
    # 若按 latin-1 解, 日文会变成乱码
    assert "永青文庫" not in body.decode("latin-1")


def test_decode_prefers_meta_charset():
    raw_bytes = '<meta charset="euc-jp">'.encode() + "日本".encode("euc_jp")
    assert "日本" in te._decode(raw_bytes)


def test_decode_handles_bytes_str_and_empty():
    assert te._decode(None) == ""
    assert te._decode(b"") == ""
    assert te._decode("そのまま") == "そのまま"


def test_html_to_text_strips_script_and_comments():
    junk = "<script>var a=1;</script><!-- 2023年の旧展 --><p>本館 A室<br />2026年</p>"
    text = te._html_to_text(junk)
    assert "var a" not in text
    assert "旧展" not in text
    assert "本館 A室" in text


# ---- 年份推断(纯函数) ------------------------------------------------------

def test_infer_span_uses_year_hint_when_given():
    assert te.infer_span((10, 1), (10, 20), TODAY, 2026) == (
        date(2026, 10, 1), date(2026, 10, 20),
    )


def test_infer_span_year_hint_rolls_end_into_next_year():
    """12月～1月: year_hint 是起始年, 结束日要落到次年。"""
    assert te.infer_span((12, 20), (1, 5), TODAY, 2026) == (
        date(2026, 12, 20), date(2027, 1, 5),
    )


def test_infer_span_without_hint_anchors_on_end_not_before_today():
    # 结束日不早于今天 -> 用今年
    assert te.infer_span((11, 3), (11, 5), TODAY) == (date(2026, 11, 3), date(2026, 11, 5))
    # 结束日已过 -> 整体推到明年
    assert te.infer_span((9, 1), (9, 10), TODAY) == (date(2027, 9, 1), date(2027, 9, 10))


def test_infer_span_without_hint_pulls_start_back_a_year():
    """起始月日晚于结束月日 -> 起始在上一年(实测 12月20日～1月5日 这种跨年展)。"""
    assert te.infer_span((12, 20), (1, 5), TODAY) == (date(2026, 12, 20), date(2027, 1, 5))


def test_infer_span_rejects_impossible():
    assert te.infer_span((13, 1), (13, 2), TODAY) is None
    assert te.infer_span((2, 30), (3, 1), TODAY, 2026) is None


def test_parse_full_date_needs_year():
    assert te.parse_full_date("2026年9月 9日（水）") == date(2026, 9, 9)
    assert te.parse_full_date("2026年09月29日") == date(2026, 9, 29)
    assert te.parse_full_date("10月9日") is None          # 只有月日, 不能直接解析
    assert te.parse_full_date("") is None


# ---- 窗口过滤 --------------------------------------------------------------

def test_window_keeps_running_and_upcoming(tmp_path):
    adapter = _adapter(tmp_path)
    assert adapter.in_window(date(2026, 9, 1), date(2026, 12, 20), TODAY) is True   # 展出中
    assert adapter.in_window(date(2026, 10, 28), date(2027, 2, 8), TODAY) is True   # 尚未开幕
    assert adapter.in_window(date(2026, 12, 1), date(2026, 12, 20), TODAY) is True   # 60 天内


def test_window_drops_ended_and_too_far(tmp_path):
    adapter = _adapter(tmp_path)
    # 已结束
    assert adapter.in_window(date(2026, 4, 11), date(2026, 6, 7), TODAY) is False
    # 60 天后才开(默认窗口 60 天, 边界内/外各测一次)
    assert adapter.in_window(date(2026, 12, 4), date(2026, 12, 5), TODAY) is True
    assert adapter.in_window(date(2026, 12, 5), date(2026, 12, 6), TODAY) is False


def test_window_drops_missing_dates(tmp_path):
    adapter = _adapter(tmp_path)
    assert adapter.in_window(None, date(2026, 10, 10), TODAY) is False
    assert adapter.in_window(date(2026, 10, 10), None, TODAY) is False


def test_window_days_is_configurable(tmp_path):
    adapter = _adapter(tmp_path, window_days=7)
    assert adapter.in_window(date(2026, 10, 10), date(2026, 10, 12), TODAY) is True
    assert adapter.in_window(date(2026, 10, 20), date(2026, 10, 21), TODAY) is False


def test_collect_applies_window(tmp_path):
    """实测日 2026-10-05: 永青文庫已结束的春/夏季展掉出去, 秋季展(10/3~11/29)留下,
    早春展(2027/1/16 开)超出 60 天窗口也掉。"""
    recs = list(_adapter(tmp_path).collect())
    eisei = [r.item.title for r in recs if r.item.source == "tokyo_events:eiseibunko"]
    assert not any(t.startswith("春季展") for t in eisei)
    assert not any(t.startswith("夏季展") for t in eisei)
    assert any(t.startswith("秋季展") for t in eisei)     # 10/3~11/29 展出中
    assert not any(t.startswith("早春展") for t in eisei)  # 2027/1/16, 距今 103 天 > 60


def test_collect_drops_items_older_than_window(tmp_path):
    """东博「源氏物語」2027/1/19 开、3/14 结束 —— 距今 106 天, 不在 60 天窗口内。"""
    titles = [r.item.title for r in _adapter(tmp_path).collect()]
    assert not any("源氏物語" in t for t in titles)
    # 阳性对照: 10/28 开幕的「少女漫画」在窗口内, 要留着
    assert any(t.startswith("少女漫画") for t in titles)


def test_collect_totals(tmp_path):
    """5 源共 131 条解析出, 窗口过滤后剩 100 条(实测日 2026-10-05)。"""
    recs = list(_adapter(tmp_path).collect())
    assert len(recs) == 100
    counts: dict[str, int] = {}
    for r in recs:
        counts[r.item.source] = counts.get(r.item.source, 0) + 1
    assert counts == {
        "tokyo_events:ntj": 62,
        "tokyo_events:t_bunka": 28,
        "tokyo_events:nact": 7,
        "tokyo_events:tnm": 2,
        "tokyo_events:eiseibunko": 1,
    }


# ---- Item 字段映射 ---------------------------------------------------------

def test_collect_item_field_mapping(tmp_path):
    recs = list(_adapter(tmp_path).collect())
    top = recs[0]
    assert top.item.source.startswith("tokyo_events:")
    assert top.item.lang == "ja"
    assert top.item.ts == NOW          # ts = 抓到它的时刻, 不是官方发布时间
    assert top.item.author
    assert "闲与美" in top.item.tags


def test_collect_ts_is_fetch_time_not_publish_time(tmp_path):
    """展演没有发布时间 —— ts 必须是抓取时刻(这里注入的固定 NOW)。"""
    recs = list(_adapter(tmp_path, clock=lambda: datetime(2026, 10, 9, 23, 30,
                                                          tzinfo=timezone.utc)).collect())
    assert {r.item.ts for r in recs} == {
        datetime(2026, 10, 9, 23, 30, tzinfo=timezone.utc)}


def test_collect_source_payload_shape(tmp_path):
    recs = list(_adapter(tmp_path).collect())
    for r in recs:
        payload = json.loads(r.source_payload_json)
        assert set(payload) == {"kind", "venue", "start_date", "end_date", "genre"}
        assert payload["kind"] == "leisure"
        assert payload["venue"] and payload["genre"]
        date.fromisoformat(payload["start_date"])       # ISO
        date.fromisoformat(payload["end_date"])


def test_collect_body_is_plain_text(tmp_path):
    recs = list(_adapter(tmp_path).collect())
    for r in recs:
        body = r.item.body
        assert body.startswith("会期：")
        assert "会場：" in body
        assert "タイプ：" in body
        for junk in ("<p", "<br", "&nbsp;", "&lt;", "</span>"):
            assert junk not in body


def test_collect_body_dates_match_payload(tmp_path):
    for r in _adapter(tmp_path).collect():
        payload = json.loads(r.source_payload_json)
        assert f"会期：{payload['start_date']}～{payload['end_date']}" in r.item.body


def test_collect_titles_are_single_line(tmp_path):
    for r in _adapter(tmp_path).collect():
        assert "\n" not in r.item.title


def test_collect_url_is_official_page(tmp_path):
    """url 是官方页。`Item.url` 的 validator 会归一化(去尾斜杠/去 fragment)。"""
    urls = {r.item.url for r in _adapter(tmp_path).collect()}
    assert "https://www.tnm.jp/modules/r_free_page/index.php?id=2759" in urls
    assert "https://www.t-bunka.jp/stage/32954" in urls
    assert "https://www.ntj.jac.go.jp/schedule/nou/2026/85026" in urls
    assert "https://www.nact.jp/exhibition_special/2026/louvre2026" in urls
    assert "http://www.eiseibunko.com/exhibition.html" in urls


def test_collect_limit(tmp_path):
    assert len(list(_adapter(tmp_path).collect(limit=5))) == 5


def test_collect_since_filters_by_ts(tmp_path):
    """since 晚于抓取时刻 -> 全滤掉(ts 就是抓取时刻)。"""
    future = datetime(2026, 10, 6, tzinfo=timezone.utc)
    assert list(_adapter(tmp_path).collect(since=future)) == []


# ---- 去重 ------------------------------------------------------------------

def test_dedup_key_is_venue_title_startdate():
    key = te.dedup_key("永青文庫", "早春展 「細川家四代展（仮）」", "2027-01-16")
    assert key == "永青文庫|早春展 「細川家四代展（仮）」|2027-01-16"


def test_dedup_key_normalizes_whitespace():
    a = te.dedup_key("国立能楽堂", "10月定例公演　禁野・熊坂", "2026-10-07")
    b = te.dedup_key("国立能楽堂", "10月定例公演 禁野・熊坂", "2026-10-07")
    assert a == b


def test_collect_second_run_is_empty(tmp_path):
    assert len(list(_adapter(tmp_path).collect())) == 100
    assert list(_adapter(tmp_path).collect()) == []      # 同一 state -> 全是重复


def test_collect_fresh_state_path_recollects(tmp_path):
    assert len(list(_adapter(tmp_path / "a").collect())) == 100
    assert len(list(_adapter(tmp_path / "b").collect())) == 100


def test_collect_item_ids_unique(tmp_path):
    """nact 公募展三条 href 都是页内锚点, canonicalize_url 会剥掉 fragment ——
    用 compute_item_id(url=...) 会撞成同一个 id, 三场只剩一场。"""
    recs = list(_adapter(tmp_path).collect())
    ids = [r.item.id for r in recs]
    assert len(ids) == len(set(ids))
    public = [r for r in recs if r.item.title.startswith("第90回")]
    assert public and public[0].item.id.startswith("tokyo_events:")


def test_collect_same_show_different_dates_are_separate_items(tmp_path):
    """「まちなかコンサート」同名多场(实测窗口内 8 场): 一场一条, 不按标题吞掉。"""
    recs = list(_adapter(tmp_path).collect())
    machinaka = [r for r in recs if r.item.title.startswith("まちなかコンサート")]
    assert len(machinaka) == 8
    assert len({r.item.id for r in machinaka}) == 8
    assert len({r.item.url for r in machinaka}) == 8


def test_compute_event_id_shape():
    ident = te.compute_event_id("venue|title|2026-10-05")
    assert ident.startswith("tokyo_events:")
    assert len(ident.split(":")[1]) == 40       # sha1


# ---- 零匹配计数 ------------------------------------------------------------

#: 抓取成功但页面解析不出条目(改版/结构不对)的样子: 非空, 但五个 parser 都解析出 0 条
UNPARSABLE = b"<html><body>maintenance</body></html>"


def test_zero_match_counts_and_escalates(tmp_path, caplog):
    """抓取成功但一条都解析不到 -> warning + state 计数; 连续 3 轮升级 error。
    I-3: 抓取失败(b"")不再计入, 计数只认解析出 0 条。"""
    with caplog.at_level(logging.WARNING):
        list(_adapter(tmp_path, blobs={k: b"" for k in FIXTURE_FILES}).collect())
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["zero_streak"] == {}, "抓取失败不计入零条连击(旧语义这里是各源 1)"
    assert not [r for r in caplog.records if "一条都没解析到" in r.getMessage()]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        list(_adapter(tmp_path, blobs={k: UNPARSABLE for k in FIXTURE_FILES}).collect())
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["zero_streak"] == {k: 1 for k in FIXTURE_FILES}
    # 各解析函数自己也会 warn 一条(「页面结构不对」之类), 这里只数升级计数那一条
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING and "一条都没解析到" in r.getMessage()]
    assert len(warnings) == 5
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_zero_match_escalates_to_error_on_third_round(tmp_path, caplog):
    """I-3 语义: 夹在中间的抓取失败轮既不计数也不清零, 第 3 个「解析出 0 条」轮才升 error。"""
    unparsable = {k: UNPARSABLE for k in FIXTURE_FILES}
    for blobs in (unparsable, {}, unparsable):
        with caplog.at_level(logging.WARNING):
            list(_adapter(tmp_path, blobs=blobs).collect())
    assert not [r for r in caplog.records if r.levelno == logging.ERROR], "抓取失败轮不得把计数推到 3"
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["zero_streak"]["ntj"] == 2
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        list(_adapter(tmp_path, blobs=unparsable).collect())
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "连续第 3 轮" in errors[0].getMessage()
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["zero_streak"]["ntj"] == 3


def test_zero_streak_resets_after_recovery(tmp_path):
    adapter = _adapter(tmp_path, blobs={k: UNPARSABLE for k in FIXTURE_FILES})
    list(adapter.collect())
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["zero_streak"]["ntj"] == 1
    # I-3: 抓取失败轮不清零(旧语义下 b"" 会把它推到 2)
    list(_adapter(tmp_path, blobs={}).collect())
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["zero_streak"]["ntj"] == 1
    healthy = _adapter(tmp_path)
    list(healthy.collect())
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["zero_streak"]["ntj"] == 0


def test_fetch_failure_not_counted_in_zero_streak(tmp_path, caplog):
    """I-3(照 html_columns D10): fetch 返回空串 = 抓取失败(网络/non-200),
    不计入零条连击, 也不清零已有计数; 只有抓取成功但解析出 0 条才计入。"""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"seen": [], "zero_streak": {k: 2 for k in FIXTURE_FILES}}), "utf-8")
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            list(_adapter(tmp_path, blobs={}).collect())
    state = json.loads(state_path.read_text("utf-8"))
    assert state["zero_streak"] == {k: 2 for k in FIXTURE_FILES}, "抓取失败既不加一也不清零"
    assert not [r for r in caplog.records if "一条都没解析到" in r.getMessage()]
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_zero_match_for_single_source_does_not_stop_others(tmp_path):
    """一个源挂掉, 其他源照常出条目。"""
    blobs = {k: raw(k) for k in FIXTURE_FILES}
    blobs["nact"] = b"<html>500</html>"
    recs = list(_adapter(tmp_path, blobs=blobs).collect())
    assert recs
    assert not [r for r in recs if r.item.source == "tokyo_events:nact"]
    assert len(recs) == 93


def test_parser_exception_does_not_kill_other_sources(tmp_path, monkeypatch):
    def boom(raw_, today):
        raise RuntimeError("parser exploded")
    monkeypatch.setitem(te.PARSE_BY_KEY, "nact", boom)
    recs = list(_adapter(tmp_path).collect())
    assert len(recs) == 93
    assert not [r for r in recs if r.item.source == "tokyo_events:nact"]


# ---- state 健壮性 ----------------------------------------------------------

def test_broken_state_file_not_raise(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{not json", "utf-8")
    assert len(list(_adapter(tmp_path).collect())) == 100


def test_state_written_shape(tmp_path):
    list(_adapter(tmp_path).collect())
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert len(state["seen"]) == 100
    assert state["zero_streak"] == {k: 0 for k in FIXTURE_FILES}
    assert state["updated_at"].endswith("Z")


def test_fetch_failure_returns_empty_not_raise(tmp_path):
    adapter = _adapter(tmp_path, blobs={})
    assert list(adapter.collect()) == []


# ---- 配置 / 网络层 ---------------------------------------------------------

def test_sources_config_shape():
    assert [s.venue_key for s in te.SOURCES] == [
        "ntj", "tnm", "t_bunka", "nact", "eiseibunko"]
    assert {s.kind for s in te.SOURCES} == {"json", "rss", "ssr_html"}
    for s in te.SOURCES:
        assert s.url.startswith("http")
        assert s.venue and s.author
        assert te.PARSE_BY_KEY[s.venue_key] is not None


def test_headers_avoid_brotli_and_ask_japanese():
    """实测: 带 br 时 requests 会把正文解成乱码; 素 UA 在东博直接 403。"""
    headers = te._build_headers()
    assert "br" not in headers["Accept-Encoding"].split(",")
    assert "ja" in headers["Accept-Language"]
    assert "Mozilla" in headers["User-Agent"]


def test_fetcher_sleeps_between_sources():
    slept: list[float] = []
    fetcher = te.make_fetcher(interval_s=3.5, sleep=slept.append)
    fetcher(te.SOURCES[0])
    fetcher(te.SOURCES[1])
    assert slept == [3.5, 3.5]


def test_default_window_is_60_days():
    assert te.WINDOW_DAYS == 60
    assert te.TokyoEventsAdapter(fetch=lambda s: b"").window_days == 60


@pytest.mark.parametrize("venue_key,count", sorted(RAW_COUNTS.items()))
def test_every_source_yields_something(venue_key, count, tmp_path):
    """阳性对照: 5 个源各自都真能出条目(不是靠别的源凑数)。"""
    parser = te.PARSE_BY_KEY[venue_key]
    assert len(parser(raw(venue_key), TODAY)) == count


@pytest.mark.parametrize("venue_key", sorted(FIXTURE_FILES))
def test_parsed_entries_all_have_required_fields(venue_key):
    for entry in te.PARSE_BY_KEY[venue_key](raw(venue_key), TODAY):
        assert entry["title"] and "\n" not in entry["title"]
        assert entry["url"].startswith("http")
        assert entry["venue"] and entry["genre"]
        date.fromisoformat(entry["start_date"])
        date.fromisoformat(entry["end_date"])
        assert date.fromisoformat(entry["end_date"]) >= date.fromisoformat(entry["start_date"])
