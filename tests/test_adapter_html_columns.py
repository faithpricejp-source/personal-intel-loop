"""html_columns: 三站各自的字段/日期抽取、去重、7 天窗、日期取不到丢弃、配置缺失、零匹配升级。

fixtures 是 2026-10-05 真抓的三个列表页截取, 各删到 5 条
(`tests/fixtures/html_columns/<key>.html`)。抓取时的实测事实:

- 中工网 `workercn.cn/character/`: 27 条, URL 路径 `/c/2026-10-01/8907492.shtml` 带日期,
  条目里另有发布时间, 两者一致 → `date_source: url_path`。
- 読売 `yomiuri.co.jp/serial/jidai/`: 列表页 15 个匹配只对应 **10 个唯一 URL**(移动版区块
  重复挂出), 必须本轮内去重。**URL 里的 `20261001` 不是发布日** —— `<time datetime=
  "2026-10-02T10:00">` 才是 → `date_source: list_text`。
- 朝日 `asahi.com/rensai/list.html?id=50`: 10 条, 日期是 `2026年10月04日 05時00分` 这种
  汉字格式, 要 `年/月/日/時/分` 一组命名捕获。

三站都 SSR 直出, 不需要浏览器。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from personal_intel_loop.adapters import html_columns as hc
from personal_intel_loop.adapters.html_columns import HtmlColumnsAdapter, load_columns

FIX = Path(__file__).parent / "fixtures" / "html_columns"
REAL_CONFIG = Path(__file__).parent.parent / "config" / "html_columns.json"
KEYS = ("workercn_character", "yomiuri_jidai", "asahi_hito")

COLS = {c.key: c for c in load_columns(REAL_CONFIG)}


#: fixture 是 2026-10-05 抓的, 条目发布于 2026-09-27 ~ 2026-10-04。
#: 默认 7 天窗按**真实当前时间**算, 所以跑测试时必须显式给 cutoff, 否则全被丢掉。
CUTOFF = datetime(2026, 9, 25, tzinfo=timezone.utc)


def _fixture(key: str) -> str:
    return (FIX / f"{key}.html").read_text("utf-8")


def _config(tmp_path: Path, keys=KEYS, **overrides) -> Path:
    """把真实配置里指定栏目写到一个临时配置文件(可覆盖字段), 返回路径。"""
    entries = []
    for key in keys:
        entry = {k: v for k, v in COLS[key].__dict__.items()
                 if not k.startswith("_") and v is not None}
        entry["item_regex"] = COLS[key].item_regex
        entry.update(overrides.get(key, {}))
        entries.append(entry)
    path = tmp_path / "cols.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False), "utf-8")
    return path


def _adapter(tmp_path, list_html: dict[str, str], **kw) -> HtmlColumnsAdapter:
    kw.setdefault("fetch_list", lambda url: list_html.get(url, ""))
    kw.setdefault("fetch_detail", lambda url: "<html><body><p>正文</p></body></html>")
    # 正文要够长(> PAYWALL_BODY_CHARS=400), 否则 is_paywalled 会把假 body 判成付费墙导语
    kw.setdefault("extract_body", lambda html: "正文内容。" * 200)
    state = tmp_path / "state.json"
    # 注意: config_path 的默认值不能写成 `kw.pop("config_path", _config(tmp_path))` ——
    # 默认值是 eager 求值的, 传了 config_path 也会把全 3 栏目的配置写回同一个文件。
    config_path = kw.pop("config_path", None) or _config(tmp_path)
    return HtmlColumnsAdapter(config_path=config_path, state_path=state, **kw)


# ---- 三站各自的字段抽取与日期 ----------------------------------------------


@pytest.mark.parametrize("key", KEYS)
def test_parse_list_extracts_five_items(key):
    raws = hc.parse_list(_fixture(key), COLS[key])
    assert len(raws) == 5, f"{key} fixture 应有 5 条"
    assert all(r.title and r.url for r in raws)


def test_workercn_fields_and_date_from_url_path():
    """中工网: 日期在 URL 路径里, 拼绝对地址, 标题原文。"""
    col = COLS["workercn_character"]
    assert col.date_source == "url_path"
    raws = hc.parse_list(_fixture("workercn_character"), col)
    first = raws[0]
    assert first.url == "https://www.workercn.cn/c/2026-10-02/8907680.shtml"
    assert first.title == "追光的你｜英烈倒下的地方 长出守护的力量"
    # 2026-10-02 00:00 Asia/Shanghai -> 前一天 16:00 UTC
    assert hc._resolve_ts(col, first) == datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)


def test_yomiuri_date_comes_from_time_tag_not_url():
    """読売: URL 里是 20261001, 真实发布日 10-02 —— 走 list_text 才对。"""
    col = COLS["yomiuri_jidai"]
    assert col.date_source == "list_text"
    raws = hc.parse_list(_fixture("yomiuri_jidai"), col)
    first = raws[0]
    assert first.url == "https://www.yomiuri.co.jp/serial/jidai/20261001-GYT8T00227/"
    assert first.title.startswith("［時代の証言者］")
    # <time datetime="2026-10-02T10:00"> JST -> 01:00 UTC, **不是** URL 里的 10-01
    assert hc._resolve_ts(col, first) == datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)
    assert COLS["yomiuri_jidai"].date_source != "url_path"


def test_asahi_fields_and_kanji_date():
    """朝日: 标题带「（ひと）」前缀, 日期是汉字格式。"""
    col = COLS["asahi_hito"]
    raws = hc.parse_list(_fixture("asahi_hito"), col)
    first = raws[0]
    assert first.url == "https://www.asahi.com/articles/DA3S16559946.html"
    assert first.title == "（ひと）伏見寅威さん 阪神タイガース捕手、加入１年目で左腕をエースに導く"
    # 2026年10月04日 05時00分 JST -> 前一天 20:00 UTC
    assert hc._resolve_ts(col, first) == datetime(2026, 10, 3, 20, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("key", KEYS)
def test_collect_builds_item_with_source_and_payload(key, tmp_path):
    adapter = _adapter(tmp_path, {COLS[key].list_url: _fixture(key)})
    records = list(adapter.collect(since=CUTOFF))
    assert len(records) == 5
    rec = records[0]
    assert rec.adapter_name == "html_columns"
    assert rec.item.source == f"html_columns:{key}"
    assert COLS[key].name in rec.item.tags
    assert rec.item.lang == COLS[key].lang
    payload = json.loads(rec.source_payload_json)
    assert payload == {"kind": "warmth", "column": COLS[key].name, "paywalled": False}


def test_paywalled_flagged_when_body_is_only_lead(tmp_path):
    """付费墙只拿到导语: 照收但 paywalled=True。"""
    adapter = _adapter(
        tmp_path,
        {COLS["asahi_hito"].list_url: _fixture("asahi_hito")},
        extract_body=lambda html: "有料記事\n" + "導語のみ。" * 10,
    )
    recs = list(adapter.collect(since=CUTOFF))
    assert len(recs) == 5
    assert all(json.loads(r.source_payload_json)["paywalled"] is True for r in recs)


def test_detail_page_fetched_once_per_item(tmp_path):
    """限速 ≥3.2s, 每条详情页只该发一个请求。"""
    calls: list[str] = []

    def fetch_detail(url: str) -> str:
        calls.append(url)
        return "<html><body><p>正文</p></body></html>"

    adapter = _adapter(tmp_path, {COLS["yomiuri_jidai"].list_url: _fixture("yomiuri_jidai")},
                       fetch_detail=fetch_detail)
    list(adapter.collect(since=CUTOFF))
    assert len(calls) == len(set(calls)) == 5


# ---- 去重 ------------------------------------------------------------------


def test_duplicate_urls_within_one_page_skipped(tmp_path):
    """読売列表页 15 个匹配只对应 10 个唯一 URL —— 本轮内重复必须挡掉。"""
    page = _fixture("yomiuri_jidai")
    start = page.index('<div class="item"')
    end = page.index("</div></div></div>")
    items = page[start:end]
    doubled = page[:start] + items + items + page[end:]
    assert doubled.count("/serial/jidai/") == 2 * page.count("/serial/jidai/")

    adapter = _adapter(tmp_path, {COLS["yomiuri_jidai"].list_url: doubled},
                       config_path=_config(tmp_path, keys=("yomiuri_jidai",)))
    urls = [r.item.url for r in adapter.collect(since=CUTOFF)]
    assert len(urls) == len(set(urls)) == 5


def test_seen_urls_skipped_across_runs(tmp_path):
    """第二轮同一页不再产出(state 记住已见 url)。"""
    cfg = _config(tmp_path, keys=("workercn_character",))
    pages = {COLS["workercn_character"].list_url: _fixture("workercn_character")}

    first = list(_adapter(tmp_path, pages, config_path=cfg).collect(since=CUTOFF))
    assert len(first) == 5
    second = list(_adapter(tmp_path, pages, config_path=cfg).collect(since=CUTOFF))
    assert second == []


def test_state_keeps_seen_urls(tmp_path):
    adapter = _adapter(tmp_path, {COLS["asahi_hito"].list_url: _fixture("asahi_hito")})
    list(adapter.collect(since=CUTOFF))
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert len(state["seen"]) == 5
    assert all(u.startswith("https://www.asahi.com/articles/") for u in state["seen"])


# ---- 7 天窗 ----------------------------------------------------------------


def test_only_recent_seven_days_kept(tmp_path):
    """时间窗: `since` 就是 cutoff(与 who_don 同语义), 早于它的条目丢掉。

    窗外条目**不写进 seen** —— 否则条目滚出窗口后, 页面再出同一条也永远收不到。
    """
    key = "yomiuri_jidai"
    url = COLS[key].list_url
    cfg = _config(tmp_path, keys=(key,))
    # fixture 里 5 条发布于 2026-09-27T20:00Z ~ 2026-10-02T01:00Z
    since = datetime(2026, 9, 29, tzinfo=timezone.utc)
    adapter = _adapter(tmp_path, {url: _fixture(key)}, config_path=cfg)
    records = list(adapter.collect(since=since))
    assert len(records) == 3, "窗内应有 3 条(09-29T20 / 09-30T20 / 10-02T01)"
    assert all(r.item.ts >= since for r in records)
    seen = json.loads((tmp_path / "state.json").read_text("utf-8"))["seen"]
    assert len(seen) == 3, "窗外条目不进 seen"
    assert len(seen) == len(set(seen))


def test_since_none_uses_days_window(tmp_path):
    """不传 since 时按 days=7 相对现在取窗(默认参数确实是 7)。"""
    assert hc.DEFAULT_DAYS == 7
    adapter = _adapter(tmp_path, {COLS["asahi_hito"].list_url: _fixture("asahi_hito")})
    cutoff = datetime.now(timezone.utc) - timedelta(days=adapter.days)
    for r in adapter.collect():
        assert r.item.ts >= cutoff


def test_limit_caps_result(tmp_path):
    adapter = _adapter(tmp_path, {COLS["yomiuri_jidai"].list_url: _fixture("yomiuri_jidai")})
    assert len(list(adapter.collect(since=datetime(2026, 1, 1, tzinfo=timezone.utc), limit=2))) == 2


# ---- 日期取不到就丢弃 -------------------------------------------------------


def test_item_without_parsable_date_is_dropped(tmp_path, caplog):
    """日期取不到 → 丢条目, 不用抓取时刻冒充。

    改朝日的 `date_regex` 让它匹配不上(item_regex 仍能匹配, 所以条目确实被抽出来了,
    是日期这一步失败的 —— 正是要验的那条路径)。
    """
    key = "asahi_hito"
    cfg = _config(tmp_path, keys=(key,),
                  asahi_hito={"date_regex": r"(?P<y>19\d\d})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日"})
    adapter = _adapter(tmp_path, {COLS[key].list_url: _fixture(key)}, config_path=cfg)
    assert hc.parse_list(_fixture(key), COLS[key]), "前提: 条目本身能抽出来"
    with caplog.at_level(logging.WARNING):
        records = list(adapter.collect(since=datetime(2026, 9, 1, tzinfo=timezone.utc)))
    assert records == []
    assert any("日期取不到" in r.message for r in caplog.records)


def test_dropped_items_are_not_marked_seen(tmp_path):
    """日期取不到的条目不该写进 seen, 否则页面修好后也永远收不到。"""
    key = "asahi_hito"
    url = COLS[key].list_url
    bad_cfg = _config(tmp_path / "a", keys=(key,),
                      asahi_hito={"date_regex": r"(?P<y>19\d\d})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日"})
    good_cfg = _config(tmp_path / "b", keys=(key,))

    # 2026-10-07 补: 显式传 since(本文件头部既有约定), fixture 最旧条目 2026-09-30
    # 已被真实时钟的 7 天窗甩出, 挂钟依赖让本测试跨过 10-07 04:00(本地)后必失败
    list(_adapter(tmp_path / "a", {url: _fixture(key)}, config_path=bad_cfg).collect(since=CUTOFF))
    assert json.loads((tmp_path / "a" / "state.json").read_text("utf-8"))["seen"] == []

    recs = list(_adapter(tmp_path / "b", {url: _fixture(key)}, config_path=good_cfg).collect(since=CUTOFF))
    assert len(recs) == 5


def test_parse_date_returns_none_without_regex():
    assert hc.parse_date("2026-10-02", None, "Asia/Tokyo") is None


def test_parse_date_rejects_out_of_range():
    rx = r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"
    assert hc.parse_date("2026-13-45", rx, "Asia/Tokyo") is None


# ---- 配置缺失 / 坏配置 ------------------------------------------------------


def test_missing_config_returns_empty(tmp_path):
    adapter = HtmlColumnsAdapter(config_path=tmp_path / "nope.json",
                                 state_path=tmp_path / "state.json")
    assert list(adapter.collect()) == []


def test_no_state_file_written_when_no_config(tmp_path):
    state = tmp_path / "state.json"
    list(HtmlColumnsAdapter(config_path=tmp_path / "nope.json", state_path=state).collect())
    assert not state.exists()


def test_broken_json_config_returns_empty(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", "utf-8")
    assert load_columns(path) == []


def test_entry_without_key_or_regex_skipped(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps([
        {"key": "", "list_url": "https://x/", "item_regex": "a"},
        {"key": "no_regex", "list_url": "https://x/"},
        {"key": "bad_rx", "list_url": "https://x/", "item_regex": "([unclosed"},
    ], ensure_ascii=False), "utf-8")
    assert load_columns(path) == []


def test_invalid_date_regex_returns_none_instead_of_guessing():
    """date_regex 语法错 → 返回 None(条目被丢), 绝不退回抓取时刻。"""
    assert hc.parse_date("2026年10月04日", "([unclosed", "Asia/Tokyo") is None
    assert hc.parse_date("2026年10月04日", r"(?P<y>\d{4})年", "Asia/Tokyo") is None


# ---- 零匹配计数与升级 -------------------------------------------------------


def test_zero_match_counted_in_state(tmp_path, caplog):
    """页面结构变了(一条都匹配不到)→ warning + state 记 zero_match_runs。"""
    adapter = _adapter(tmp_path, {COLS["asahi_hito"].list_url: "<html><body>改版了</body></html>"})
    with caplog.at_level(logging.WARNING):
        assert list(adapter.collect()) == []
    assert any("零匹配" in r.message for r in caplog.records)
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["zero_match"]["asahi_hito"]["zero_match_runs"] == 1


def test_zero_match_escalates_to_error_after_three_runs(tmp_path, caplog):
    cfg = _config(tmp_path, keys=("asahi_hito",))
    pages = {COLS["asahi_hito"].list_url: "<html><body>改版了</body></html>"}
    levels = []
    for _ in range(3):
        with caplog.at_level(logging.DEBUG):
            list(_adapter(tmp_path, pages, config_path=cfg).collect())
        levels.append(max(r.levelno for r in caplog.records if "零匹配" in r.message))
        caplog.clear()
    assert levels[0] == logging.WARNING
    assert levels[1] == logging.WARNING
    assert levels[2] == logging.ERROR, "连续 3 轮为 0 应升级为 error"
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["zero_match"]["asahi_hito"]["zero_match_runs"] == 3


def test_zero_match_resets_after_recovery(tmp_path):
    """页面修好后计数归零。"""
    cfg = _config(tmp_path, keys=("asahi_hito",))
    url = COLS["asahi_hito"].list_url
    list(_adapter(tmp_path, {url: "<html>改版</html>"}, config_path=cfg).collect())
    list(_adapter(tmp_path, {url: "<html>改版</html>"}, config_path=cfg).collect())
    assert json.loads((tmp_path / "state.json").read_text("utf-8"))["zero_match"]["asahi_hito"]["zero_match_runs"] == 2
    list(_adapter(tmp_path, {url: _fixture("asahi_hito")}, config_path=cfg).collect())
    assert json.loads((tmp_path / "state.json").read_text("utf-8"))["zero_match"]["asahi_hito"]["zero_match_runs"] == 0


# ---- 其它 -------------------------------------------------------------------


def test_max_per_run_caps_per_column(tmp_path):
    cfg = _config(tmp_path, keys=("asahi_hito",), asahi_hito={"max_per_run": 2})
    adapter = _adapter(tmp_path, {COLS["asahi_hito"].list_url: _fixture("asahi_hito")},
                       config_path=cfg)
    assert len(list(adapter.collect(since=CUTOFF))) == 2


def test_records_sorted_newest_first(tmp_path):
    pages = {
        COLS["workercn_character"].list_url: _fixture("workercn_character"),
        COLS["asahi_hito"].list_url: _fixture("asahi_hito"),
    }
    adapter = _adapter(tmp_path, pages, config_path=_config(tmp_path))
    got = [r.item.ts for r in adapter.collect(since=datetime(2026, 9, 1, tzinfo=timezone.utc))]
    assert got == sorted(got, reverse=True)


def test_config_has_three_real_columns():
    """交付配置**开头**仍是设计规格冻结的那 3 个栏目(后面可以追加新栏目)。

    2026-10-05 起配置多了 2 个 browser 栏目(chinanews_fsh / chunichi_anohito),
    所以这里从"恰好 3 个"放宽成"前 3 个不变"; 新增栏目的断言见
    `test_config_now_has_five_columns_with_two_browser`。
    """
    cols = load_columns(REAL_CONFIG)
    assert [c.key for c in cols][:len(KEYS)] == list(KEYS)
    assert all(c.kind == "warmth" for c in cols)
    assert {c.lang for c in cols} == {"zh", "ja"}


# ---- item_selector / detail_meta 两条备用路径(3 个真栏目都没用到, 但配置支持) ----

_SELECTOR_PAGE = (
    '<html><body><ul>'
    '<li class="item"><a href="/a/2026-10-01/1.html"><h3>标题甲</h3>'
    '<span class="d">2026-10-01</span></a></li>'
    '<li class="other"><a href="/b/2026-10-01/2.html"><h3>不该收</h3></a></li>'
    '<li class="item"><a href="/c/2026-10-02/3.html"><h3>标题乙</h3>'
    '<span class="d">2026-10-02</span></a></li>'
    "</ul></body></html>"
)


def _selector_column(**kw) -> hc.Column:
    base = dict(
        key="sel", name="选择器测试", list_url="https://example.com/list/", lang="zh",
        item_selector="li.item", date_source="list_text",
        date_regex=r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})", tz="Asia/Shanghai",
    )
    base.update(kw)
    col = hc.Column(**base)
    return col


def test_item_selector_extracts_matching_items_only():
    """`item_selector` 只认 class 匹配的 <li> —— `li.other` 不该被收。"""
    col = _selector_column()
    raws = hc.parse_list(_SELECTOR_PAGE, col)
    assert [r.title for r in raws] == ["标题甲", "标题乙"]
    assert raws[0].url == "https://example.com/a/2026-10-01/1.html"
    assert hc._resolve_ts(col, raws[0]) == datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)


def test_item_regex_takes_precedence_over_selector():
    col = _selector_column(item_regex=r"<a href=\"(?P<url>[^\"]+)\"><h3>(?P<title>标题\w)</h3>")
    assert [r.title for r in hc.parse_list(_SELECTOR_PAGE, col)] == ["标题甲", "标题乙"]


def test_missing_named_group_yields_no_items(caplog):
    """配置里的 title_group/url_group 在正则中不存在 → 该栏目 0 条(记 warning), 不抛异常。"""
    col = _selector_column(item_regex=r"<h3>(?P<title>标题\w)</h3>")
    with caplog.at_level(logging.WARNING):
        assert hc.parse_list(_SELECTOR_PAGE, col) == []
    assert any("没有名为" in r.message for r in caplog.records)


def test_detail_meta_date_source_reads_detail_page():
    """`date_source: detail_meta` 从详情页取日期(列表页没给日期时的兜底)。"""
    col = _selector_column(item_regex=r"<a href=\"(?P<url>[^\"]+)\"><h3>(?P<title>标题\w)</h3>",
                           date_source="detail_meta",
                           date_regex=r"<time datetime=\"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})")
    page = ('<html><body><a href="/a/x.html"><h3>标题甲</h3></a>'
            '<a href="/c/y.html"><h3>标题乙</h3></a></body></html>')
    details = {"https://example.com/a/x.html": '<time datetime="2026-10-03">',
               "https://example.com/c/y.html": '<time datetime="2026-10-04">'}
    raws = hc.parse_list(page, col)
    assert [hc._resolve_ts(col, r, detail_html=lambda u: details[u]).isoformat()
            for r in raws] == ["2026-10-02T16:00:00+00:00", "2026-10-03T16:00:00+00:00"]


def test_html_entities_and_full_width_spaces_in_title_cleaned():
    """实测中工网标题里有 `&#32;` 与全角空格, 要还原成正常空格。"""
    col = COLS["workercn_character"]
    page = ('<html><body><h3><a href="/c/2026-10-01/1.shtml">'
            '逆风三分钟&#32;黄衣一束光　记 王佐帅</a></h3></body></html>')
    raws = hc.parse_list(page, col)
    assert raws[0].title == "逆风三分钟 黄衣一束光 记 王佐帅"


def test_paywall_marker_detection():
    assert hc.is_paywalled("有料記事" + "导语" * 300) is True
    assert hc.is_paywalled("短") is True
    assert hc.is_paywalled("正文" * 300) is False


# ==============================================================================
# 需浏览器渲染的两个栏目(2026-10-05 真网实测后追加)
#
# 中新网「新闻浮世绘」`channel.chinanews.com.cn/u/fsh.shtml` 与中日新聞「あの人に迫る」
# `chunichi.co.jp/wadai/feature/anohito` 的**静态 HTML 里列表是空的** —— 条目由 JS 渲染后
# 才出现, 必须无头浏览器(实测姿势见 html_columns 模块 docstring)。fixture 是渲染后 DOM
# 的真实截取, 各留 5 条(`tests/fixtures/html_columns/<key>.html`)。
#
# 实测要点(写正则前踩过的坑, 别照抄直觉):
#
# - **中新网**: 页面里同时存在一份 JS 模板区块(`listStr += '<li>...' + doc.url`),
#   fixture 故意留着它 —— 验证 `item_regex` 靠真实 URL 字面量不会误伤模板。
#   `<div class="time">` 里的 `2026-10-04 21:09:35` 与 URL 路径日期 100% 一致, 带时分,
#   所以走 `list_text`。
# - **中日新聞**: 列表页日期只有 `10月2日`, **没有年份**(页面上那几个 `2023-03-01`/
#   `2028-03-31` 是订阅套餐样板, 不是文章日期, 正则绝不能碰)。列表页也无 `<time>`/无年份。
#   精确日期只能从详情页拿(`<span class="data">2026年10月2日 16時00分`, 与 JSON-LD
#   `"datePublished":"2026-10-02T16:00:00+09:00"` 一致) → `date_source: detail_meta`。
#   `?rct=anohito` 限定本栏目, 天然排除页尾其它栏目的推荐新闻(fixture 里也留了一条)。
# - **详情页不必上浏览器**: 实测两站详情页普通 requests 都 200(trafilatura 抽到
#   773/1067 字正文), 所以只列表页用 `render: browser`, `render_detail` 留 `http`。
# ==============================================================================

BROWSER_KEYS = ("chinanews_fsh", "chunichi_anohito")


def _bconfig(tmp_path: Path, keys=KEYS + BROWSER_KEYS, **overrides) -> Path:
    """同 `_config`, 但默认带上两个 browser 栏目(5 个)。"""
    return _config(tmp_path, keys=keys, **overrides)


def _badapter(tmp_path, list_html: dict[str, str], *, render_html=None,
              detail_html: str | None = None, keys=KEYS + BROWSER_KEYS, **kw):
    """跑 browser 栏目的 adapter。

    - http 栏目走 `fetch_list`(默认返回 fixture, **绝不发真网请求**)。
    - browser 栏目走注入的假 `render_list`(返回 fixture) —— 不起浏览器。
    两个通道都从`list_html` 取fixture, 但**调用记录分开**: `adapter.calls` 只记 render 通道,
    所以测试能断言 browser 栏目没碰过 fetch_list。
    """
    render_map = render_html or {COLS[k].list_url: list_html[COLS[k].list_url]
                                 for k in BROWSER_KEYS if COLS[k].list_url in list_html}
    calls: list[tuple[str, str]] = []

    def fake_render_list(url: str, *, col=None) -> str:
        calls.append(("list", url))
        return render_map.get(url, "")

    def fake_render_detail(url: str, *, col=None) -> str:
        calls.append(("detail", url))
        return detail_html or ""

    kw.setdefault("render_list", fake_render_list)
    kw.setdefault("fetch_list", lambda url: list_html.get(url, ""))
    kw.setdefault("fetch_detail", lambda url: detail_html or "")
    kw.setdefault("extract_body", lambda html: "正文内容。" * 200)
    # 同 _adapter: config_path 的默认值是 eager 求值的, 传了就别再写回默认配置
    config_path = kw.pop("config_path", None) or _bconfig(tmp_path, keys=keys)
    adapter = HtmlColumnsAdapter(
        config_path=config_path,
        state_path=tmp_path / "state.json",
        **kw,
    )
    adapter.calls = calls          # type: ignore[attr-defined]
    return adapter


def _all_fixtures() -> dict[str, str]:
    out = {COLS[k].list_url: _fixture(k) for k in KEYS + BROWSER_KEYS}
    return out


# ---- 中新网·新闻浮世绘 --------------------------------------------------------


def test_chinanews_fsh_fields_and_date_from_list_text():
    """中新网: 标题在 `news_title`, URL 以 `//` 开头要拼成 https, 日期带时分。"""
    col = COLS["chinanews_fsh"]
    assert col.render == "browser" and col.date_source == "list_text"
    raws = hc.parse_list(_fixture("chinanews_fsh"), col)
    assert len(raws) == 5
    first = raws[0]
    assert first.url == "https://www.chinanews.com.cn/sh/2026/10-04/10707971.shtml"
    assert first.title == "新西兰青少年的三亚之行：文化体验中感知中国"
    # <div class="time">2026-10-04 21:09:35 Asia/Shanghai -> 13:09 UTC
    assert hc._resolve_ts(col, first) == datetime(2026, 10, 4, 13, 9, tzinfo=timezone.utc)


def test_chinanews_fsh_js_template_block_not_matched():
    """页面里那段 `listStr += '<li>...' + doc.url` 的 JS 模板不能被当成条目。

    模板里没有真实 URL 字面量, 所以 `item_regex` 天然排除它 —— 这条测试把这个
    事实钉住: 以后谁把正则放宽到`<li>...` 就会红。
    """
    page = _fixture("chinanews_fsh")
    assert "+ doc.url" in page, "前提: fixture 里保留了 JS 模板区块"
    raws = hc.parse_list(page, COLS["chinanews_fsh"])
    assert len(raws) == 5, "模板区块被误当成条目了"
    assert all("doc.url" not in r.url for r in raws)


# ---- 中日新聞·あの人に迫る ----------------------------------------------------


def test_chunichi_anohito_fields_and_date_from_detail_meta():
    """中日: 列表页日期无年份 → 日期必须走详情页。"""
    col = COLS["chunichi_anohito"]
    assert col.render == "browser"
    assert col.date_source == "detail_meta", "列表页没有年份, 只能走 detail_meta"
    raws = hc.parse_list(_fixture("chunichi_anohito"), col)
    assert len(raws) == 5
    first = raws[0]
    assert first.url == "https://www.chunichi.co.jp/article/1319578?rct=anohito"
    assert first.title.startswith("「能登牛」のおいしさを広めることで")
    # 列表片段里只有 "10月2日", 没有 y -> 直接对列表取日期必然 None(证明必须走详情页)
    assert hc.parse_date(first.block, col.date_regex, col.tz) is None
    detail = _fixture("chunichi_anohito_detail")
    # 2026年10月2日 16時00分 JST -> 07:00 UTC
    assert hc._resolve_ts(col, first, detail_html=lambda u: detail) == datetime(
        2026, 10, 2, 7, 0, tzinfo=timezone.utc)


def test_chunichi_anohito_list_date_has_no_year():
    """钉住"列表页日期无年份"这个实测事实: 免得日后有人改date_source 踩坑。"""
    col = COLS["chunichi_anohito"]
    first = hc.parse_list(_fixture("chunichi_anohito"), col)[0]
    assert "10月2日" in first.block
    assert not re.search(r"\d{4}年", first.block), "若日后列表页给了年份, 这条会红并提示复核"


def test_chunichi_anohito_excludes_other_columns_recommendations():
    """`?rct=anohito` 限定本栏目, 页尾别的栏目推荐新闻不该被收。"""
    page = _fixture("chunichi_anohito")
    assert "1318824" in page, "前提: fixture 里留了一条他栏新闻"
    raws = hc.parse_list(page, COLS["chunichi_anohito"])
    assert len(raws) == 5
    assert all("rct=anohito" in r.url for r in raws)


def test_chunichi_item_dropped_when_detail_date_unparsable(tmp_path, caplog):
    """详情页取不到日期 → 丢条目(不拿列表页那个无年份的日期凑, 也不拿抓取时刻冒充)。"""
    key = "chunichi_anohito"
    adapter = _badapter(tmp_path, {COLS[key].list_url: _fixture(key)},
                        detail_html="<html><body>付费墙/改版, 没有日期</body></html>",
                        keys=(key,))
    assert COLS[key].needs_browser, "前提: 这是 browser 栏目, 列表页走 render 通道"
    with caplog.at_level(logging.WARNING):
        assert list(adapter.collect(since=datetime(2026, 9, 1, tzinfo=timezone.utc))) == []
    assert any("日期取不到" in r.message for r in caplog.records)


# ---- render 选项分派 ----------------------------------------------------------


def test_render_option_dispatches_list_fetch(tmp_path):
    """browser 栏目的列表页走 `render_list`, http 栏目走 `fetch_list`, 互不串。"""
    fixtures = _all_fixtures()
    http_calls: list[str] = []
    adapter = _badapter(
        tmp_path,
        fixtures,
        render_html={COLS[k].list_url: fixtures[COLS[k].list_url] for k in BROWSER_KEYS},
        fetch_list=lambda url: (http_calls.append(url), fixtures.get(url, ""))[1],
        detail_html=_fixture("chunichi_anohito_detail"),
    )
    list(adapter.collect(since=CUTOFF))
    rendered = [u for kind, u in adapter.calls if kind == "list"]
    assert sorted(rendered) == sorted(COLS[k].list_url for k in BROWSER_KEYS)
    # http 栏目的 list_url 只出现在 fetch_list 里, 且**不含**两个 browser 栏目
    assert sorted(http_calls) == sorted(COLS[k].list_url for k in KEYS)
    assert not (set(http_calls) & {COLS[k].list_url for k in BROWSER_KEYS})


def test_render_detail_dispatches_when_configured(tmp_path):
    """`render_detail: browser` 时详情页也走 render 通道(实测两站默认都够用, 这条验分派)。"""
    key = "chunichi_anohito"
    cfg = _bconfig(tmp_path, keys=(key,), chunichi_anohito={"render_detail": "browser"})
    detail = _fixture("chunichi_anohito_detail")
    detail_calls: list[str] = []

    def fake_render_detail(url: str, *, col=None) -> str:
        detail_calls.append(url)
        return detail

    adapter = HtmlColumnsAdapter(
        config_path=cfg, state_path=tmp_path / "state.json",
        render_list=lambda url, col=None: _fixture(key),
        render_detail=fake_render_detail,
        fetch_detail=lambda url: pytest.fail("render_detail=browser 时不该走 fetch_detail"),
        extract_body=lambda html: "正文内容。" * 200,
    )
    recs = list(adapter.collect(since=CUTOFF))
    assert len(recs) == 5
    assert len(detail_calls) == 5, "每条详情页各取一次"


def test_render_detail_defaults_to_http_for_browser_columns():
    """两个 browser 栏目实测详情页普通请求就够 → `render_detail` 缺省 http。"""
    for key in BROWSER_KEYS:
        assert COLS[key].render == "browser"
        assert COLS[key].render_detail == "http"


def test_needs_browser_property():
    assert COLS["chinanews_fsh"].needs_browser is True
    for key in KEYS:
        assert COLS[key].needs_browser is False, f"{key} 是 http 栏目, 不该需要浏览器"


def test_render_scroll_configured():
    """中新网静态列表空, 必须滚轮触发懒加载 → scroll 次数要> 0。"""
    assert COLS["chinanews_fsh"].render_scroll >= 3
    assert COLS["chunichi_anohito"].render_scroll >= 0


def test_invalid_render_value_falls_back_to_http(tmp_path, caplog):
    """`render` 写错值 → 记warning + 按 http, 不让栏目永远失败。"""
    path = tmp_path / "bad_render.json"
    path.write_text(json.dumps([{
        "key": "bad", "name": "坏值", "list_url": "https://example.com/",
        "item_regex": r"<a href=\"(?P<url>[^\"]+)\">(?P<title>\w+)</a>",
        "render": "chromium", "render_detail": "banana",
    }]), "utf-8")
    with caplog.at_level(logging.WARNING):
        cols = load_columns(path)
    assert [c.render for c in cols] == ["http"]
    assert [c.render_detail for c in cols] == ["http"]
    assert any("render 非法" in r.message or "render_detail 非法" in r.message
               for r in caplog.records)


def test_missing_render_defaults_to_http(tmp_path):
    """没写 render 的配置(前三个栏目就是)→ http, 行为不变。"""
    path = tmp_path / "c.json"
    path.write_text(json.dumps([{
        "key": "nokey", "list_url": "https://example.com/",
        "item_regex": r"<a href=\"(?P<url>[^\"]+)\">(?P<title>\w+)</a>",
    }]), "utf-8")
    col = load_columns(path)[0]
    assert (col.render, col.render_detail, col.render_scroll) == ("http", "http",
                                                                 hc.DEFAULT_RENDER_SCROLL)


# ---- playwright 未安装时: browser 栏目跳过, http 栏目照跑 -------------------


def test_browser_columns_skipped_with_warning_when_playwright_missing(tmp_path, caplog,
                                                                    monkeypatch):
    """**核心降级契约**: playwright 不可用 → browser 栏目记warning 并跳过,
    同一配置里的 http 栏目**照常产出**(不能整轮挂掉)。"""
    class _NoBrowser:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            raise hc.BrowserUnavailable("playwright 未安装")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(hc, "_Browser", _NoBrowser)
    fixtures = _all_fixtures()
    http_calls: list[str] = []
    adapter = HtmlColumnsAdapter(
        config_path=_bconfig(tmp_path),
        state_path=tmp_path / "state.json",
        fetch_list=lambda url: (http_calls.append(url), fixtures.get(url, ""))[1],
        fetch_detail=lambda url: "<html><body><p>正文</p></body></html>",
        extract_body=lambda html: "正文内容。" * 200,
    )
    with caplog.at_level(logging.WARNING):
        recs = list(adapter.collect(since=CUTOFF))
    keys = {r.item.source.removeprefix("html_columns:") for r in recs}
    assert keys == set(KEYS), "http 栏目必须照常产出"
    assert len(recs) == 15, "3 个 http 栏目 × 5 条"
    assert any("playwright" in r.message and "跳过" in r.message for r in caplog.records)
    # 被跳过的 browser 栏目不该去动 http 栏目的取数通道
    assert not (set(http_calls) & {COLS[k].list_url for k in BROWSER_KEYS})


def test_browser_unavailable_is_not_counted_as_zero_match(tmp_path, monkeypatch):
    """playwright 缺失导致的 0 条**不是改版**, 不该进 zero_match 计数
    (否则连续 3 轮就误报 error, 页面根本没变)。"""
    class _NoBrowser:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            raise hc.BrowserUnavailable("playwright 未安装")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(hc, "_Browser", _NoBrowser)
    cfg = _bconfig(tmp_path, keys=BROWSER_KEYS)
    for _ in range(3):
        adapter = HtmlColumnsAdapter(config_path=cfg, state_path=tmp_path / "state.json")
        assert list(adapter.collect(since=CUTOFF)) == []
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["zero_match"] == {}, f"不该记 zero_match: {state['zero_match']}"


def test_render_injected_avoids_browser_entirely(tmp_path, monkeypatch):
    """注入了 render_list/render_detail → **绝不能**去构造 _Browser。"""

    def _boom(*a, **kw):
        raise AssertionError("注入了假 render 函数时不该起浏览器")

    monkeypatch.setattr(hc, "_Browser", _boom)
    fixtures = _all_fixtures()
    adapter = _badapter(
        tmp_path,
        fixtures,
        render_html={COLS[k].list_url: fixtures[COLS[k].list_url] for k in BROWSER_KEYS},
        detail_html=_fixture("chunichi_anohito_detail"),
    )
    recs = list(adapter.collect(since=CUTOFF))
    assert len(recs) == 25, "5 个栏目 × 5 条"


def test_browser_closed_even_when_column_raises(tmp_path, monkeypatch):
    """栏目里抛异常时浏览器也必须关掉(否则进程退不出)。"""
    closed: list[str] = []

    class _FakeBrowser:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            closed.append("closed")
            return False

        def render_html(self, url, *, scroll=0):
            # 返回真有条目的 fixture —— 零匹配的话根本走不到详情页, 测不到"关浏览器"
            return _fixture("chinanews_fsh")

    monkeypatch.setattr(hc, "_Browser", _FakeBrowser)
    # 让**渲染途中**抛(而不是取浏览器之前就抛), 才能验到"已开的浏览器会被关掉"
    monkeypatch.setattr(HtmlColumnsAdapter, "_detail_html",
                        lambda self, col, url: (_ for _ in ()).throw(RuntimeError("详情炸了")))
    adapter = HtmlColumnsAdapter(config_path=_bconfig(tmp_path, keys=("chinanews_fsh",)),
                                 state_path=tmp_path / "state.json",
                                 render_detail=lambda url, col=None: "")
    with pytest.raises(RuntimeError):
        list(adapter.collect(since=CUTOFF))
    assert closed == ["closed"]


# ---- 字段/去重/窗口: 两个 browser 栏目也要守原有契约 -------------------------


def test_browser_columns_build_item_fields(tmp_path):
    """两个 browser 栏目的 Item 字段与 payload 契约与前三个一致。"""
    fixtures = _all_fixtures()
    adapter = _badapter(tmp_path, fixtures,
                        render_html={COLS[k].list_url: fixtures[COLS[k].list_url]
                                     for k in BROWSER_KEYS},
                        detail_html=_fixture("chunichi_anohito_detail"))
    recs = list(adapter.collect(since=CUTOFF))
    by_key = {r.item.source.removeprefix("html_columns:"): r for r in recs}
    assert set(by_key) == set(KEYS + BROWSER_KEYS)
    for key in BROWSER_KEYS:
        rec = by_key[key]
        col = COLS[key]
        assert rec.adapter_name == "html_columns"
        assert rec.item.lang == col.lang
        assert col.name in rec.item.tags and "人情味" in rec.item.tags
        assert rec.item.body, "正文不该为空"
        payload = json.loads(rec.source_payload_json)
        assert payload["column"] == col.name
        assert payload["paywalled"] is False


def test_browser_columns_respect_seven_day_window_and_dedupe(tmp_path):
    """7 天窗 + 本轮内去重对 browser 栏目同样生效。"""
    fixtures = _all_fixtures()
    adapter = _badapter(tmp_path, fixtures,
                        render_html={COLS[k].list_url: fixtures[COLS[k].list_url]
                                     for k in BROWSER_KEYS},
                        detail_html=_fixture("chunichi_anohito_detail"),
                        keys=BROWSER_KEYS)
    # 09-25 的窗: 两个栏目都在内(中新网 10-04 / 中日 10-02), 共 10 条
    recs = list(adapter.collect(since=CUTOFF))
    urls = [r.item.url for r in recs]
    assert len(urls) == len(set(urls)) == 10, "本轮内重复 URL 必须挡掉"
    assert all(r.item.ts >= CUTOFF for r in recs)
    assert {r.item.source.removeprefix("html_columns:") for r in recs} == set(BROWSER_KEYS)

    # 收窄到 10-03: 中日(10-02)落到窗外被丢掉, 只剩中新网 —— 时间窗对 browser 栏目生效
    recs2 = list(_badapter(tmp_path / "b", fixtures,
                           render_html={COLS[k].list_url: fixtures[COLS[k].list_url]
                                        for k in BROWSER_KEYS},
                           detail_html=_fixture("chunichi_anohito_detail"),
                           keys=BROWSER_KEYS).collect(
        since=datetime(2026, 10, 3, tzinfo=timezone.utc)))
    assert {r.item.source.removeprefix("html_columns:") for r in recs2} == {"chinanews_fsh"}


def test_browser_columns_zero_match_upgrades_on_real_breakage(tmp_path, caplog):
    """browser 栏目页面真改版(渲染后仍无条目)→ 照样进 zero_match 三轮升级 error。"""
    cfg = _bconfig(tmp_path, keys=("chinanews_fsh",))
    levels = []
    for _ in range(3):
        adapter = HtmlColumnsAdapter(
            config_path=cfg, state_path=tmp_path / "state.json",
            render_list=lambda url, col=None: "<html><body>改版了</body></html>",
            fetch_detail=lambda url: "", extract_body=lambda html: "正文。" * 200)
        with caplog.at_level(logging.DEBUG):
            list(adapter.collect(since=CUTOFF))
        levels.append(max((r.levelno for r in caplog.records if "零匹配" in r.message),
                          default=0))
        caplog.clear()
    assert levels == [logging.WARNING, logging.WARNING, logging.ERROR]


def test_config_now_has_five_columns_with_two_browser():
    """交付配置 = 设计规格冻结的 3 个 http 栏目 + 本轮新增的 2 个 browser 栏目。"""
    cols = load_columns(REAL_CONFIG)
    assert [c.key for c in cols] == list(KEYS + BROWSER_KEYS)
    assert all(c.kind == "warmth" for c in cols)
    assert {c.lang for c in cols} == {"zh", "ja"}
    by_key = {c.key: c for c in cols}
    assert [by_key[k].render for k in KEYS] == ["http"] * 3
    assert [by_key[k].render for k in BROWSER_KEYS] == ["browser", "browser"]


def test_existing_three_http_columns_unchanged():
    """冻结范围: 已有三站的行为一字不改(全部仍是 http + 原 date_source)。"""
    expect = {
        "workercn_character": ("url_path", "Asia/Shanghai"),
        "yomiuri_jidai": ("list_text", "Asia/Tokyo"),
        "asahi_hito": ("list_text", "Asia/Tokyo"),
    }
    for key, (date_source, tz) in expect.items():
        col = COLS[key]
        assert col.render == "http" and col.render_detail == "http"
        assert col.date_source == date_source and col.tz == tz
        assert col.item_regex and col.max_per_run == 10
