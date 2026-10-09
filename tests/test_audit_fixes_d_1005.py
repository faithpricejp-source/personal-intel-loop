"""10-05 审计 D 卷修复的回归测试。每条修复一个测试, 修复前失败、修复后通过。

对应审计: audits/AUDIT_D.md(D01–D23; D24–D27 属 risk_reference.py, 本任务冻结不改)。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from personal_intel_loop.place_aliases import (
    expand,
    load_table,
    normalize_term,
    reload,
    resolve,
)


@pytest.fixture(autouse=True)
def _fresh_table():
    """别名表有进程级缓存, 改配置后必须 reload 才能看到。"""
    reload()
    yield
    reload()


# --- D01: hoian 块里混进了顺化(Hue)的别名, 顺化/会安是两个城市 ---


def test_d01_hue_is_not_resolved_as_hoian():
    assert resolve("顺化") == ("vietnam", "hue")
    assert resolve("順化") == ("vietnam", "hue")
    assert resolve("フエ") == ("vietnam", "hue")
    assert resolve("Hue") == ("vietnam", "hue")
    # 会安自己的别名不受影响
    assert resolve("会安") == ("vietnam", "hoian")
    assert resolve("ホイアン") == ("vietnam", "hoian")


def test_d01_hue_city_entry_has_complete_aliases():
    """拆出来的独立城市要带完整的 zh/ja/en 别名。"""
    table = load_table()
    vietnam = next(c for c in table["countries"] if c.get("key") == "vietnam")
    hue = vietnam["cities"]["hue"]
    for lang in ("zh", "ja", "en"):
        assert hue.get(lang), f"hue 缺 {lang} 别名"


# --- D02: boracay 块里混进了薄荷岛(Bohol), 两个是不同的岛 ---


def test_d02_bohol_is_not_resolved_as_boracay():
    assert resolve("薄荷岛") == ("philippines", "bohol")
    assert resolve("長灘島") == ("philippines", "boracay")
    assert resolve("ボラカイ") == ("philippines", "boracay")
    assert resolve("Boracay") == ("philippines", "boracay")


def test_d02_bohol_city_entry_has_complete_aliases():
    table = load_table()
    philippines = next(c for c in table["countries"] if c.get("key") == "philippines")
    bohol = philippines["cities"]["bohol"]
    for lang in ("zh", "ja", "en"):
        assert bohol.get(lang), f"bohol 缺 {lang} 别名"


# --- D03: bath 块里的「巴塞」是巴塞罗那的常用简称, 不是英国巴斯 ---


def test_d03_basei_ambiguous_abbreviation_removed():
    assert resolve("巴塞") == (None, None)
    assert resolve("巴斯") == ("unitedkingdom", "bath")
    assert resolve("バス") == ("unitedkingdom", "bath")
    assert resolve("Bath") == ("unitedkingdom", "bath")


# --- D04: hurghada 块混进了沙姆沙伊赫, 且 ja 写法「ハーグダ」非标准(海牙是ハーグ) ---


def test_d04_sharm_is_not_resolved_as_hurghada():
    assert resolve("沙姆沙伊赫") == ("egypt", "sharm")
    assert resolve("Sharm el-Sheikh") == ("egypt", "sharm")
    # 赫尔格达自己的别名不受影响, 标准 ja 写法要能命中
    assert resolve("赫尔格达") == ("egypt", "hurghada")
    assert resolve("フルガダ") == ("egypt", "hurghada")
    assert resolve("Hurghada") == ("egypt", "hurghada")
    # 非标准写法「ハーグダ」不该再留在表里
    assert normalize_term("ハーグダ") not in load_table()["by_term"]


# --- D05: 五处非标准日文片假名, 标准写法反而匹配不到 ---


@pytest.mark.parametrize(
    ("standard", "city"),
    [
        ("デリー", ("india", "delhi")),
        ("コタキナバル", ("malaysia", "kotakinabalu")),
        ("バンドン", ("indonesia", "bandung")),
        ("ルクソール", ("egypt", "luxor")),
        ("アンタルヤ", ("turkey", "antalya")),
    ],
)
def test_d05_standard_katakana_resolves(standard, city):
    assert resolve(standard) == city


@pytest.mark.parametrize(
    "obsolete",
    ["デルヒ", "コタキンタバン", "バンディング", "ラクサー", "アンタリュア"],
)
def test_d05_nonstandard_katakana_removed(obsolete):
    assert normalize_term(obsolete) not in load_table()["by_term"]


# --- D06: 全半角不折叠(IME/PDF 复制常见), 全角写法静默漏匹配 ---


def test_d06_normalize_term_folds_width_nfkc():
    assert normalize_term("ＵＳＡ") == "usa"
    assert normalize_term("ﾆｭｰﾖｰｸ") == normalize_term("ニューヨーク")


def test_d06_fullwidth_input_resolves_via_table(tmp_path):
    """全角写法要能命中表里的半角别名。"""
    import json

    table = tmp_path / "place_aliases.json"
    table.write_text(
        json.dumps({"countries": [{"key": "us", "zh": ["美国"], "ja": ["アメリカ"],
                                   "en": ["USA"], "cities": {}}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    reload()
    got = expand("ＵＳＡ", path=table)
    assert got["country_key"] == "us"
    reload()


# --- D07: 表文件字节损坏/非 UTF-8 时按 docstring 应退化, 不该抛 UnicodeDecodeError ---


def test_d07_undecodable_table_degrades_without_raising(tmp_path):
    bad = tmp_path / "place_aliases.json"
    bad.write_bytes(b'{"countries": [\xff\xfe\xfa}')
    reload()
    got = expand("Bangkok", path=bad)          # 修复前在这里直接抛 UnicodeDecodeError
    assert got["country_key"] is None
    assert got["terms"] == ["Bangkok"]
    reloaded = load_table(bad)
    assert reloaded == {"countries": [], "by_term": {}}
    reload()


# --- D08: 读失败得到的空表被永久缓存, 文件恢复后也不重试 ---


def test_d08_failed_load_is_not_cached_forever(tmp_path):
    path = tmp_path / "place_aliases.json"
    path.write_text("{ broken json", encoding="utf-8")
    reload()
    assert load_table(path)["by_term"] == {}
    # 文件恢复正常 —— 长驻进程里必须能自愈, 不能永远命中缓存里的空表
    path.write_text(
        '{"countries": [{"key": "thailand", "zh": ["泰国"], "cities": {}}]}',
        encoding="utf-8",
    )
    got = load_table(path)
    assert got["by_term"].get(normalize_term("泰国")) == ("thailand", None)
    reload()


# --- D09: 地点栏命中国家级、国家栏却是另一个国家时, 地点栏被静默丢弃 ---


def test_d09_place_bar_wins_on_country_conflict(tmp_path):
    """地点=タイ / 国家=美国(误填) —— 用户要去的是泰国, 风险证据不能路由到美国。"""
    import json

    table = tmp_path / "place_aliases.json"
    table.write_text(
        json.dumps({"countries": [
            {"key": "thailand", "zh": ["泰国"], "ja": ["タイ"], "en": ["Thailand"], "cities": {}},
            {"key": "unitedstates", "zh": ["美国"], "en": ["United States"], "cities": {}},
        ]}, ensure_ascii=False),
        encoding="utf-8",
    )
    reload()
    assert resolve("タイ", "美国", path=table) == ("thailand", None)
    # 国家栏与地点一致、以及地点认不出靠国家栏兜底的原行为不变
    assert resolve("タイ", "泰国", path=table) == ("thailand", None)
    assert resolve("somewhere local", "タイ", path=table) == ("thailand", None)
    reload()


# --- D10: 连续抓取失败被当成「页面改版零匹配」计 error, 与 docstring 承诺不符 ---


_MINIMAL_COLUMN = [{
    "key": "net", "name": "网络", "list_url": "https://example.com/list", "lang": "zh",
    "item_regex": r"<a href=\"(?P<url>[^\"]+)\">(?P<title>[^<]+)</a>",
    "date_source": "url_path",
    "date_regex": r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})",
}]


def test_d10_fetch_failure_is_not_counted_as_zero_match(tmp_path):
    from personal_intel_loop.adapters.html_columns import HtmlColumnsAdapter

    cfg = tmp_path / "cols.json"
    cfg.write_text(json.dumps(_MINIMAL_COLUMN, ensure_ascii=False), encoding="utf-8")
    state = tmp_path / "state.json"
    # 连续 3 轮网络故障: fetch_list 返回空串(与 _http_get 失败时的行为一致)
    for _ in range(3):
        adapter = HtmlColumnsAdapter(config_path=cfg, state_path=state,
                                     fetch_list=lambda url: "")
        list(adapter.collect())
    payload = json.loads(state.read_text("utf-8"))
    assert payload["zero_match"] == {}, "抓不到页面(网络失败)不该进改版计数"


def test_d10_real_zero_match_still_counted(tmp_path):
    """对照: 页面真抓到了但没有条目(改版) → 照旧计数升级。"""
    from personal_intel_loop.adapters.html_columns import HtmlColumnsAdapter

    cfg = tmp_path / "cols.json"
    cfg.write_text(json.dumps(_MINIMAL_COLUMN, ensure_ascii=False), encoding="utf-8")
    state = tmp_path / "state.json"
    for _ in range(3):
        adapter = HtmlColumnsAdapter(config_path=cfg, state_path=state,
                                     fetch_list=lambda url: "<html>改版了</html>")
        list(adapter.collect())
    payload = json.loads(state.read_text("utf-8"))
    assert payload["zero_match"]["net"]["zero_match_runs"] == 3


# --- D11: <li> 不写闭合标签时全嵌进第一条, ul > li 子代链漏抓 ---


def test_d11_unclosed_li_are_siblings_under_selector_chain():
    from personal_intel_loop.adapters.html_columns import Column, parse_list

    col = Column(key="sel", name="选择器", list_url="https://example.com/", lang="ja",
                 item_selector="ul > li")
    html_doc = ('<html><body><ul>'
                '<li><a href="/1">第一条</a>'
                '<li><a href="/2">第二条</a>'
                '</ul></body></html>')
    raws = parse_list(html_doc, col)
    assert [r.title for r in raws] == ["第一条", "第二条"]
    assert [r.url for r in raws] == ["https://example.com/1", "https://example.com/2"]


def test_d11_explicit_and_mixed_nesting_still_ok():
    """已闭合 / 嵌套列表的树形不能被弄坏。"""
    from personal_intel_loop.adapters.html_columns import Column, parse_list

    col = Column(key="sel", name="选择器", list_url="https://example.com/", lang="ja",
                 item_selector="ul > li")
    html_doc = ('<ul>'
                '<li><a href="/1">外层一</a></li>'
                '<li><a href="/2">外层二</a>'
                '<ul><li><a href="/3">内层</a></li></ul></li>'
                '</ul>')
    raws = parse_list(html_doc, col)
    assert [r.title for r in raws] == ["外层一", "外层二", "内层"]


# --- D12: 配置里写错类型(数字/布尔)会炸掉整个 load_columns, 三栏目连坐 ---


_GOOD_RX = r"<a href=\"(?P<url>[^\"]+)\">(?P<title>[^<]+)</a>"


def test_d12_bad_config_entry_types_skip_only_that_column(tmp_path):
    from personal_intel_loop.adapters.html_columns import load_columns

    cfg = tmp_path / "cols.json"
    cfg.write_text(json.dumps([
        {"key": "badrx", "name": "正则非字符串", "list_url": "https://example.com/",
         "item_regex": 123},
        {"key": "badsel", "name": "selector非字符串", "list_url": "https://example.com/",
         "item_selector": 456},
        {"key": "badmax", "name": "max_per_run非数字", "list_url": "https://example.com/",
         "item_regex": _GOOD_RX, "max_per_run": "ten"},
        {"key": "good", "name": "好栏目", "list_url": "https://example.com/",
         "item_regex": _GOOD_RX},
    ], ensure_ascii=False), encoding="utf-8")
    cols = load_columns(cfg)          # 修复前: re.compile(123) 抛 TypeError, 整轮崩
    assert [c.key for c in cols] == ["badmax", "good"]
    assert cols[0].max_per_run == 10   # "ten" 解析不了 → 回默认值, 不抛 ValueError


# --- D13: state 写一半被杀 → seen 清零重采; 写入必须原子 ---


def test_d13_state_write_is_atomic_on_midwrite_kill(tmp_path, monkeypatch):
    from pathlib import Path

    from personal_intel_loop.adapters import html_columns as hc

    state = tmp_path / "state.json"
    hc._save_state(state, {"https://old/1", "https://old/2"}, {})
    old_payload = json.loads(state.read_text("utf-8"))
    assert len(old_payload["seen"]) == 2

    real_write_text = Path.write_text

    def half_write(self, data, *args, **kwargs):
        # 模拟进程在 write 中途被杀: 文件先被截断、只写入一半
        real_write_text(self, data[: len(data) // 2], *args, **kwargs)
        raise OSError("simulated mid-write kill")

    monkeypatch.setattr(Path, "write_text", half_write)
    hc._save_state(state, {"https://new/1"}, {})
    monkeypatch.setattr(Path, "write_text", real_write_text)

    # 旧 state 必须原样健在(半截临时文件不能覆盖正式文件), 且没留垃圾
    payload = json.loads(state.read_text("utf-8"))
    assert sorted(payload["seen"]) == ["https://old/1", "https://old/2"]
    assert list(tmp_path.glob("*.tmp")) == []


# --- D14: seen 截断按字典序而非采集先后, 换域名后最新条目反而先被丢 ---


def test_d14_seen_truncated_by_insertion_order(tmp_path, monkeypatch):
    from personal_intel_loop.adapters import html_columns as hc

    monkeypatch.setattr(hc, "SEEN_LIMIT", 2)
    seen = hc._OrderedSeen(["https://mmm.example/old", "https://zzz.example/old2"])
    seen.add("http://aaa.example/new")     # 最新采集, 但字典序最小
    hc._save_state(tmp_path / "state.json", seen, {})
    got, _ = hc._load_state(tmp_path / "state.json")
    assert "http://aaa.example/new" in got, "最新采集的条目必须保留"
    assert "https://mmm.example/old" not in got, "先丢的该是最旧采集"


def test_d14_roundtrip_preserves_insertion_order(tmp_path):
    """state 存取一圈后顺序仍在(再截断时丢的还是最旧的)。"""
    from personal_intel_loop.adapters import html_columns as hc

    seen = hc._OrderedSeen(["https://a/1", "https://b/2", "https://c/3"])
    hc._save_state(tmp_path / "state.json", seen, {})
    got, _ = hc._load_state(tmp_path / "state.json")
    assert list(got) == ["https://a/1", "https://b/2", "https://c/3"]


# --- D15: 退化正文路径剥不掉未闭合的 <script>, JS 源码留在正文里 ---


def test_d15_extract_body_strips_unclosed_script(monkeypatch):
    import sys

    from personal_intel_loop.adapters import html_columns as hc

    # 强制走「trafilatura 缺失」的退化路径(pyproject 本来就没这个依赖)
    monkeypatch.setitem(sys.modules, "trafilatura", None)
    page = ('<html><head><title>列表</title></head><body>'
            '脚本之前的可见文本。'
            '<div><script>var a=1; document.write("ad");</div>'
            '脚本之后的内容按浏览器语义也被吞掉</body></html>')
    body = hc._extract_body(page)
    assert "var a=1" not in body, "未闭合 <script> 的 JS 源码不能留在正文里"
    assert "document.write" not in body
    assert "脚本之前的可见文本" in body


def test_d15_strip_helper_closes_matched_blocks_too():
    """闭合完好的 script/style/noscript 照常剥掉(原行为不变)。"""
    import sys

    from personal_intel_loop.adapters import html_columns as hc

    page = ('<div><script src="/x.js"></script><style>p{}</style>'
            '<noscript>请开启JS</noscript>正文部分</div>')
    body = hc._strip_script_blocks(page)
    assert "p{}" not in body and "请开启JS" not in body and "正文部分" in body


# --- D16: 详情页抓取失败(空串)被 is_paywalled("") 误标成付费墙 ---


def test_d16_detail_fetch_failure_not_marked_paywalled(tmp_path):
    from personal_intel_loop.adapters.html_columns import HtmlColumnsAdapter

    cfg = tmp_path / "cols.json"
    cfg.write_text(json.dumps([{
        "key": "d16", "name": "详情失败", "list_url": "https://example.com/", "lang": "zh",
        "item_regex": r"<a href=\"(?P<url>[^\"]+)\">(?P<title>[^<]+)</a>",
        "date_source": "url_path",
        "date_regex": r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})",
    }], ensure_ascii=False), encoding="utf-8")
    adapter = HtmlColumnsAdapter(
        config_path=cfg, state_path=tmp_path / "state.json",
        fetch_list=lambda url: '<a href="/c/2026-10-01/1.shtml">标题一</a>',
        fetch_detail=lambda url: "",            # 详情页抓取失败
        extract_body=lambda html: html,
    )
    recs = list(adapter.collect(since=datetime(2026, 9, 1, tzinfo=timezone.utc)))
    assert len(recs) == 1
    payload = json.loads(recs[0].source_payload_json)
    assert payload["paywalled"] is False, "网络失败不是付费墙"
    assert payload["detail_fetch_failed"] is True
    assert recs[0].item.body == ""


def test_d16_real_paywall_still_marked(tmp_path):
    """对照: 详情页抓到了但正文短/带标记 → 照旧标付费墙。"""
    from personal_intel_loop.adapters.html_columns import HtmlColumnsAdapter

    cfg = tmp_path / "cols.json"
    cfg.write_text(json.dumps([{
        "key": "d16b", "name": "真付费墙", "list_url": "https://example.com/", "lang": "ja",
        "item_regex": r"<a href=\"(?P<url>[^\"]+)\">(?P<title>[^<]+)</a>",
        "date_source": "url_path",
        "date_regex": r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})",
    }], ensure_ascii=False), encoding="utf-8")
    adapter = HtmlColumnsAdapter(
        config_path=cfg, state_path=tmp_path / "state.json",
        fetch_list=lambda url: '<a href="/c/2026-10-01/2.shtml">標題</a>',
        fetch_detail=lambda url: "<html>有料記事 これだけの導入文。</html>",
        extract_body=lambda html: "有料記事" + "導语" * 50,
    )
    recs = list(adapter.collect(since=datetime(2026, 9, 1, tzinfo=timezone.utc)))
    assert len(recs) == 1
    payload = json.loads(recs[0].source_payload_json)
    assert payload["paywalled"] is True
    assert "detail_fetch_failed" not in payload


# --- D17: 「今天」按 UTC 日期而非东京本地日期, JST 0:00~8:59 之间差一天 ---


def test_d17_today_follows_tokyo_local_date():
    from datetime import date, datetime, timedelta, timezone

    from personal_intel_loop.adapters import tokyo_events as te

    adapter = te.TokyoEventsAdapter(
        fetch=lambda s: b"",
        clock=lambda: datetime(2026, 10, 5, 8, 0, tzinfo=timezone(timedelta(hours=9))),
    )
    assert adapter.today() == date(2026, 10, 5), "JST 早上 8 点, 东京日期还是 10-05(UTC 才是 10-04)"


# --- D18: 会期含 2/29 时, 非闰年候选年直接 return None, 不再试下一年 ---


def test_d18_feb29_span_tries_next_leap_year():
    from datetime import date

    from personal_intel_loop.adapters import tokyo_events as te

    today = date(2026, 10, 5)
    got = te.infer_span((2, 29), (3, 5), today)
    assert got == (date(2028, 2, 29), date(2028, 3, 5)), "2026/2027 都没有 2/29, 最近的是 2028"


def test_d18_ordinary_spans_unchanged():
    """普通会期的推断行为不能被改动。"""
    from datetime import date

    from personal_intel_loop.adapters import tokyo_events as te

    today = date(2026, 10, 5)
    assert te.infer_span((11, 3), (11, 5), today) == (date(2026, 11, 3), date(2026, 11, 5))
    assert te.infer_span((9, 1), (9, 10), today) == (date(2027, 9, 1), date(2027, 9, 10))
    assert te.infer_span((12, 20), (1, 5), today) == (date(2026, 12, 20), date(2027, 1, 5))
    assert te.infer_span((13, 1), (13, 2), today) is None
    assert te.infer_span((2, 30), (3, 1), today, 2026) is None


# --- D19: t_bunka 日期行解析失败时续行继承上一行的旧日期 ---


def _tbunka_row(date_cells: str, href: str, title: str) -> str:
    return (f'<tr data-is_last_more="1">{date_cells}'
            f'<td><a href="{href}"><h2><span>{title}</span></h2></a></td></tr>')


def test_d19_broken_date_row_does_not_leak_old_date():
    from datetime import date

    from personal_intel_loop.adapters import tokyo_events as te

    good = ('<th class="date_row"><span class="yea">2026年</span>'
            '<span class="day">10月9日</span></th>')
    # 改版后: 带 date_row class 但 yea/day span 没了
    broken = '<th class="date_row"></th>'
    html_doc = ('<html><body><table id="result">'
                + _tbunka_row(good, "/stage/001/", "公演A")
                + _tbunka_row(broken, "/stage/002/", "公演B")
                + _tbunka_row("", "/stage/003/", "公演C")   # rowspan 续行
                + '</table></body></html>')
    out = te.parse_tbunka_html(html_doc, date(2026, 10, 5))
    titles = [e["title"] for e in out]
    # 公演B 的日期行已坏、公演C 是它的续行 —— 都不能继承公演A 的 10-09
    assert titles == ["公演A"], f"坏日期行与其续行该跳过, 实际: {titles}"
    assert out[0]["start_date"] == "2026-10-09"


# --- D20: <time datetime> 带时刻时整条静默丢弃 ---


_NACT_PAGE = (
    '<html><main><h1>展覧会</h1>'
    '<h2 class="ttl2">企画展</h2>'
    '<ul><li><a href="exhibition_special/2026/006622">'
    '<h2>ルーヴル展</h2>'
    '<p class="ex_date"><time datetime="2026-10-30T09:00">2026-10-30</time>'
    '～<time datetime="2026-11-16T10:00">2026-11-16</time></p>'
    '</a></li></ul>'
    '</main></html>'
)


def test_d20_time_datetime_with_time_component_is_kept():
    from datetime import date

    from personal_intel_loop.adapters import tokyo_events as te

    out = te.parse_nact_html(_NACT_PAGE, date(2026, 10, 5))
    assert len(out) == 1, f"带时刻的 <time datetime> 不该让整条消失: {out}"
    assert out[0]["start_date"] == "2026-10-30"
    assert out[0]["end_date"] == "2026-11-16"


def test_d20_pure_date_still_works():
    """纯日期写法(当前实测页面的形状)行为不变。"""
    from datetime import date

    from personal_intel_loop.adapters import tokyo_events as te

    out = te.parse_nact_html(_NACT_PAGE.replace("2026-10-30T09:00", "2026-10-30")
                             .replace("2026-11-16T10:00", "2026-11-16"), date(2026, 10, 5))
    assert len(out) == 1
    assert (out[0]["start_date"], out[0]["end_date"]) == ("2026-10-30", "2026-11-16")


# --- D21: tokyo_events 的 seen 字典序截断 + 非原子写 ---


def test_d21_tokyo_seen_truncated_by_insertion_order(tmp_path, monkeypatch):
    from personal_intel_loop.adapters import tokyo_events as te

    monkeypatch.setattr(te, "SEEN_LIMIT", 2)
    state = tmp_path / "state.json"
    te._save_state(state, ["mmm|旧展|2026-01-01", "zzz|旧展2|2026-02-01",
                           "aaa|新展|2026-10-01"], {})
    payload = json.loads(state.read_text("utf-8"))
    assert payload["seen"] == ["zzz|旧展2|2026-02-01", "aaa|新展|2026-10-01"], \
        "截断按采集先后, 最新采集的 aaa 必须保留(修复前按字典序先丢 aaa)"


def test_d21_tokyo_state_write_is_atomic(tmp_path, monkeypatch):
    from pathlib import Path

    from personal_intel_loop.adapters import tokyo_events as te

    state = tmp_path / "state.json"
    te._save_state(state, ["k1", "k2"], {"ntj": 1})
    real_write_text = Path.write_text

    def half_write(self, data, *args, **kwargs):
        real_write_text(self, data[: len(data) // 2], *args, **kwargs)
        raise OSError("simulated mid-write kill")

    monkeypatch.setattr(Path, "write_text", half_write)
    te._save_state(state, ["k3"], {})
    monkeypatch.setattr(Path, "write_text", real_write_text)

    payload = json.loads(state.read_text("utf-8"))
    assert payload["seen"] == ["k1", "k2"], "写一半被杀, 旧 state 必须健在"
    assert payload["zero_streak"] == {"ntj": 1}
    assert list(tmp_path.glob("*.tmp")) == []


# --- D22: 切断正则里的「問い合わせleo」「 Industrias」是异物, 永远匹配不上 ---


def test_d22_note_cut_cuts_toiawase():
    from datetime import date

    from personal_intel_loop.adapters import tokyo_events as te

    # 「問い合わせ」后面跟着日期样式的杂讯: 修复前切不掉, 会期被解析成 None(杂讯
    # 03月3日 被当结束日硬推次年, 跨度超上限), 整条丢掉
    span = te._eisei_span("2026年10月3日（土）～ 問い合わせ：03月3日", date(2026, 10, 5))
    assert span == (date(2026, 10, 3), date(2026, 10, 3))


def test_d22_note_cut_pattern_has_no_foreign_junk():
    from personal_intel_loop.adapters import tokyo_events as te

    pattern = te._NOTE_CUT_RE.pattern
    assert "問い合わせ" in pattern
    assert "leo" not in pattern and "Industrias" not in pattern


# --- D23: in_window 注释说「60 天内开始」但代码对开始日没有下界(长期展照收)。
# 修复 = 注释对齐代码(行为是合理的: 还能去的展都该收)。下面的测试锁定该语义,
# 修复前后都通过 —— 防的是将来有人照旧注释把下界加上。 ---


def test_d23_long_running_exhibition_has_no_start_lower_bound():
    from datetime import date

    from personal_intel_loop.adapters import tokyo_events as te

    adapter = te.TokyoEventsAdapter(fetch=lambda s: b"")
    today = date(2026, 10, 5)
    # 4 个多月前就开始、还没结束的长期展: 注释的旧说法(60 天内开始)是 False, 代码是 True
    assert adapter.in_window(date(2026, 6, 1), date(2027, 1, 10), today) is True
    # 已结束 / 窗口外未来开幕的判定不变
    assert adapter.in_window(date(2026, 4, 11), date(2026, 6, 7), today) is False
    assert adapter.in_window(date(2026, 12, 5), date(2026, 12, 6), today) is False
    assert te.WINDOW_DAYS == 60
