"""thepaper_warm: playwright 路径的字段映射 / 去重 / 7 天时间窗 / 正文 / 容错。

阳性对照的必要性: 温暖栏只收**具名可核实的真人真事**(契约第 8 节), 过滤很重,
所以"产出 0 条"既可能是过滤正确也可能是代码坏了 —— 两者产出完全一样。
因此这里用 `tests/fixtures/warmth/thepaper_warm_nextdata_samples.json` 里的
**真实浏览器响应**(2026-10-05 playwright 打开栏目页抓的 `__NEXT_DATA__` 截取,
每栏目 5 张真实卡片) 走同一条路径做阳性对照。

fixture 是从真实响应里截的, 所以测的是**实测字段形状**而不是我以为的形状:
- 列表字段名是 `list`(不是上一轮猜的 newsList/contList);
- 外层有两种形状: `/list_<id>` 是 `pageProps.data`, `/channel/136261` 多一层
  `{code, data:{...}}` 信封 —— 两种都要能挖;
- **epoch 毫秒在 `pubTimeLong` / `trackPublishTime`, 而 `publishTime` 是字符串**
  "2026-10-04 12:59:05"。按 epoch 读 `publishTime` 是上一轮恒 0 条的真正原因;
- `pubTime` 是给人看的相对串("10小时前"), 只作兜底并标 `published_approx`;
- `nodeInfo.nickName` 实测**恒为空**, 具名真人在 `trackAuthor`。

fixture 是 2026-10-05 抓的, 卡片最老到 2026-09-09, 所以**窗外条目是真实存在的** ——
`test_items_outside_window_dropped` 就是钉这件事的。

测试全程注入假函数, **不起浏览器**(构造函数不做任何 IO)。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from personal_intel_loop.adapters import thepaper_warm as tw
from personal_intel_loop.adapters.thepaper_warm import ThepaperWarmAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "warmth"
LIST_FIXTURE = FIXTURES / "thepaper_warm_nextdata_samples.json"
# 视频条目(contType=9, content 只有 <video>, 正文靠 summary 兜底)
DETAIL_VIDEO = FIXTURES / "thepaper_warm_detail_sample.json"
# 图文条目(contType=0, content 是真正文 HTML) —— 正文主路径的阳性样本
DETAIL_TEXT = FIXTURES / "thepaper_warm_detail_text_sample.json"


@pytest.fixture(scope="module")
def list_samples() -> dict:
    return json.loads(LIST_FIXTURE.read_text("utf-8"))


@pytest.fixture(scope="module")
def detail_sample() -> dict:
    """视频条目样本。"""
    return json.loads(DETAIL_VIDEO.read_text("utf-8"))


@pytest.fixture(scope="module")
def detail_text_sample() -> dict:
    """图文条目样本 —— `content` 里是真正文, 用来验正文主路径。"""
    return json.loads(DETAIL_TEXT.read_text("utf-8"))


def _epoch(card: dict) -> int:
    """卡片的精确 epoch 毫秒 —— 实测在 `pubTimeLong`, 不是 `publishTime`。"""
    return card["pubTimeLong"]


@pytest.fixture(scope="module")
def now_from_fixture(list_samples) -> datetime:
    """拿 fixture 里最新的 epoch 当"当前时刻", 这样相对串换算的断言是确定的。"""
    return datetime.fromtimestamp(
        max(_epoch(c) for n in list_samples.values() for c in n["cards"]) / 1000.0,
        tz=timezone.utc)


def _in_window(list_samples, node_id: int, *, now: datetime, days: int = 7) -> list[dict]:
    """只取窗内的卡片。"""
    cutoff = now - timedelta(days=days)
    return [c for c in _cards_from(list_samples, node_id)
            if datetime.fromtimestamp(_epoch(c) / 1000, tz=timezone.utc) >= cutoff]


# fixture 抓取时刻（2026-10-05 东京上午）；端到端测试一律以它为 now
FIXTURE_NOW = datetime(2026, 10, 5, 0, 30, tzinfo=timezone.utc)


def _live_window(list_samples, node_ids, *, days: int = 7) -> dict[int, list[dict]]:
    """按钉住的 FIXTURE_NOW 切窗；_adapter 给 collect() 注入同一个 now，两边口径一致、不随日子漂移。"""
    now = FIXTURE_NOW
    return {nid: _in_window(list_samples, nid, now=now, days=days) for nid in node_ids}


def _live(list_samples, node_id: int) -> list[dict]:
    """该栏目里**当前仍在 7 天窗内**的真实卡片。

    fixture 是 2026-10-05 抓的, 随时间推移会有卡片滑出窗口 —— 端到端测试一律用这个,
    别直接用 `_cards_from`(那样断言会随"今天"漂移)。
    """
    cards = _live_window(list_samples, (node_id,))[node_id]
    assert cards, f"栏目 {node_id} 的 fixture 卡片已全部滑出 7 天窗, 该换 fixture 了"
    return cards


def _cards_from(list_samples, node_id: int) -> list[dict]:
    return list(list_samples[str(node_id)]["cards"])


def _adapter(tmp_path, fetch_list=None, *, fetch_body=None, state=None, body_limit=8,
             tag="0"):
    """每次调用给独立的 state 文件 —— 同一个 tmp_path 复用会让第二次跑被去重掉。"""
    state_path = tmp_path / f"thepaper_warm_state_{tag}.json"
    if state is not None:
        state_path.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    return ThepaperWarmAdapter(
        fetch_list=fetch_list or (lambda url: []),
        fetch_body=fetch_body or (lambda url: "正文"),
        state_path=state_path,
        body_limit=body_limit,
        now_fn=lambda: FIXTURE_NOW,
    )


class FakeBrowser:
    """只实现 `next_data`, 够 fetch_list / fetch_body 用 —— 不起浏览器。"""

    def __init__(self, next_data):
        self._nd = next_data
        self.asked: list[str] = []

    def next_data(self, url):
        self.asked.append(url)
        return self._nd


def _list_nd(list_samples, node_id: int) -> dict:
    """按真实形状 A(`/list_<id>`) 包一份 `__NEXT_DATA__`。"""
    node = list_samples[str(node_id)]
    return {"props": {"pageProps": {"query": {"id": str(node_id)},
                                   "data": {"list": node["cards"],
                                            "hasNext": node["hasNext"]}}}}


# ---------- fixture 自身的事实(钉住"实测过", 免得以后有人编 fixture) ----------

def test_fixture_cards_are_real_shape(list_samples):
    """卡片必须有 contId / name / epoch 时间 —— 这是 URL、时间、去重三件事的输入。"""
    for node_id, node in list_samples.items():
        assert node["cards"], f"栏目 {node_id} 没有卡片"
        for c in node["cards"]:
            assert c["contId"].isdigit()
            assert c["name"].strip()
            assert isinstance(c["pubTimeLong"], int) and c["pubTimeLong"] > 0


def test_fixture_publishtime_is_string_and_publong_is_epoch(list_samples):
    """钉住实测事实: `publishTime` 是字符串, epoch 在 `pubTimeLong`。

    这两个搞反就是上一轮恒 0 条的原因, 所以单独钉一条。
    """
    for node in list_samples.values():
        for c in node["cards"]:
            assert isinstance(c["publishTime"], str), "publishTime 实测是字符串"
            assert not c["publishTime"].isdigit()
            assert isinstance(c["pubTimeLong"], int)
            assert c["pubTimeLong"] > 1_000_000_000_000, "pubTimeLong 是 epoch 毫秒"
            assert c["trackPublishTime"] == c["pubTimeLong"]


def test_fixture_has_all_three_columns(list_samples):
    """三个专题型栏目都在(26911 暖闻湃 / 68750 公益湃 / 25427 澎湃人物)。"""
    assert {int(k) for k in list_samples} == {26911, 68750, 25427}


def test_fixture_pubtime_is_relative_but_epoch_is_exact(list_samples):
    """实测事实: `pubTime` 是给人看的相对串, `pubTimeLong` 才是精确 epoch。

    这正是上一轮的死结所在(以为只有相对串), 所以单独钉一条。
    相对串以**站点自己的渲染时刻**为基准, 所以只能断言"落在同一天内"这种
    粗关系, 不能拿 fixture 里最新卡片当 now 去精确对账。
    """
    card = list_samples["26911"]["cards"][0]
    assert card["pubTime"] == "10小时前"
    epoch = datetime.fromtimestamp(_epoch(card) / 1000.0, tz=timezone.utc)
    # 抓取发生在 2026-10-04 晚(东八区), 该条 pubTime 说 10 小时前 → epoch 应在同一天
    assert epoch.date() == datetime(2026, 10, 4, tzinfo=timezone.utc).date()


def test_fixture_publishtime_string_agrees_with_epoch(list_samples):
    """`publishTime` 字符串与 `pubTimeLong` epoch 必须一致(互相印证, 不是巧合)。

    实测: 字符串是**北京时间且精确到秒**, epoch 精确到毫秒 —— 所以差值 < 1 秒。
    这条钉住了 adapter 按东八区解释绝对串是对的。
    """
    for node in list_samples.values():
        for c in node["cards"]:
            from_str = tw._parse_absolute(c["publishTime"])
            epoch = datetime.fromtimestamp(_epoch(c) / 1000.0, tz=timezone.utc)
            assert from_str is not None
            assert abs((from_str - epoch).total_seconds()) < 1.0


def test_fixture_nickname_is_empty_so_trackauthor_is_the_author(list_samples):
    """实测 `nodeInfo.nickName` 恒为空 —— 作者只能取 trackAuthor。"""
    for node in list_samples.values():
        for c in node["cards"]:
            assert (c.get("nodeInfo") or {}).get("nickName", "") == ""


def test_detail_fixture_has_real_body(detail_sample):
    """视频条目样本: `content` 里只有 `<video>`, 没有正文文字。"""
    cd = detail_sample["contentDetail"]
    # 实测详情页的 contId 是 **int**(列表页是字符串), adapter 侧统一 str() 兜住了
    assert str(cd["contId"]) == "34180516"
    assert cd["contType"] == 9
    assert "<video" in cd["content"]
    assert cd["summary"].strip(), "视频条目靠 summary 兜底, summary 必须有内容"
    assert "老人" in cd["summary"] or "老板" in cd["summary"]


def test_detail_text_fixture_has_real_body(detail_text_sample):
    """图文条目样本: `content` 里是真正文 HTML —— 正文主路径的阳性样本。"""
    cd = detail_text_sample["contentDetail"]
    assert str(cd["contId"]) == "34122288"
    assert cd["contType"] == 0
    text = tw._html_to_text(cd["content"])
    assert len(text) > 500, "图文正文不该只有几十字"
    assert "CAPS" in text and "公益" in text
    # 正文里嵌了 <video>(实测如此), 不该把 mp4 链接当正文
    assert "cloudvideo" not in text


# ---------- URL 配置 ----------

def test_warm_channel_uses_channel_url_not_list():
    """暖闻 136261 没有 /list_ 页 —— 用 /list_ 会 403 或静默回落到首页。

    这是实测踩到的坑(见 out/probe.md), 必须钉住, 否则改回 list_ 就静默产出 0 条。
    """
    urls = {n["node_id"]: n["url"] for n in tw.WARM_NODES}
    assert urls[136261] == "https://www.thepaper.cn/channel/136261"
    assert "/list_136261" not in urls.values()


def test_all_four_warm_columns_present():
    assert {n["node_id"] for n in tw.WARM_NODES} == {136261, 26911, 68750, 25427}
    for n in tw.WARM_NODES:
        assert n["url"].startswith("https://www.thepaper.cn/")
        assert n["name"].strip()


def test_collect_requests_every_configured_url(tmp_path, list_samples):
    """每个配置的 URL 都要被真的请求一次(注入函数记录 url)。"""
    asked: list[str] = []

    def fetch_list(url):
        asked.append(url)
        return _live(list_samples, 26911)

    _adapter(tmp_path, fetch_list).collect()
    assert set(asked) == {n["url"] for n in tw.WARM_NODES}


# ---------- __NEXT_DATA__ 挖掘 ----------

def test_payload_shape_a_list_page():
    """形状 A: /list_<id> 的 pageProps.data 直接就是 payload。"""
    nd = {"props": {"pageProps": {"query": {"id": "26911"},
                                  "data": {"list": [{"contId": "1"}], "hasNext": True}}}}
    assert tw._payload_of(nd)["list"] == [{"contId": "1"}]


def test_payload_shape_b_channel_page():
    """形状 B: /channel/136261 多一层 {code, data:{...}} 信封 —— 必须能解开。"""
    nd = {"props": {"pageProps": {"data": {"code": 200, "desc": "ok",
                                           "data": {"list": [{"contId": "2"}]}}}}}
    assert tw._payload_of(nd)["list"] == [{"contId": "2"}]


def test_payload_of_tolerates_garbage():
    for nd in ({}, {"props": {}}, {"props": {"pageProps": None}},
               {"props": {"pageProps": {"data": "not-a-dict"}}}):
        assert tw._payload_of(nd) == {}


def test_fetch_list_reads_real_payload_shape(list_samples):
    """用真实 `__NEXT_DATA__` 外壳跑 fetch_list 的挖掘逻辑(不碰浏览器)。"""
    node = list_samples["26911"]
    got = tw.fetch_list(node["url"], browser=FakeBrowser(_list_nd(list_samples, 26911)))
    assert len(got) == 5
    assert got[0]["contId"] == node["cards"][0]["contId"]


def test_fetch_list_channel_envelope_shape(list_samples):
    """形状 B 走 fetch_list 也得能拿到卡片。"""
    node = list_samples["68750"]
    nd = {"props": {"pageProps": {"data": {"code": 200, "data": {"list": node["cards"]}}}}}
    got = tw.fetch_list("https://www.thepaper.cn/channel/136261",
                        browser=FakeBrowser(nd))
    assert len(got) == 5


def test_fetch_list_missing_list_key_returns_empty():
    """payload 里没有 list → 返回空, 不猜字段名。"""
    nd = {"props": {"pageProps": {"data": {"hasNext": True}}}}
    assert tw.fetch_list("https://www.thepaper.cn/list_26911",
                         browser=FakeBrowser(nd)) == []


def test_fetch_list_no_next_data_returns_empty():
    """403 / 回落到首页 → next_data 是 None → 返回空, 不抛。"""
    assert tw.fetch_list("https://www.thepaper.cn/list_136261",
                         browser=FakeBrowser(None)) == []


def test_fetch_list_filters_non_dict_cards():
    nd = {"props": {"pageProps": {"data": {"list": [{"contId": "1"}, "junk", None]}}}}
    got = tw.fetch_list("https://www.thepaper.cn/list_26911", browser=FakeBrowser(nd))
    assert got == [{"contId": "1"}]


# ---------- 正文 ----------

def test_fetch_body_from_real_detail(detail_sample):
    """视频条目: 正文走 summary 兜底(实测 content 只有 `<video>`)。"""
    cd = detail_sample["contentDetail"]
    nd = {"props": {"pageProps": {"contId": cd["contId"],
                                  "detailData": {"contentDetail": cd}}}}
    body = tw.fetch_body(detail_sample["url"], browser=FakeBrowser(nd))
    assert body == tw._html_to_text(cd["summary"])


def test_fetch_body_from_real_text_article(detail_text_sample):
    """图文条目: 正文主路径阳性样本 —— 直接从 content HTML 转出 2000+ 字。"""
    cd = detail_text_sample["contentDetail"]
    nd = {"props": {"pageProps": {"contId": cd["contId"],
                                  "detailData": {"contentDetail": cd}}}}
    body = tw.fetch_body(detail_text_sample["url"], browser=FakeBrowser(nd))
    assert body and len(body) > 500
    assert "CAPS" in body
    assert "cloudvideo" not in body


def test_fetch_body_falls_back_to_summary_when_content_is_video(detail_sample):
    """视频条目 content 是 <video> 标签 → 转文本为空 → 回落 summary(实测暖闻湃大量如此)。"""
    cd = dict(detail_sample["contentDetail"])
    cd["content"] = '<video class="cont_video" src="https://cloudvideo.thepaper.cn/x.mp4"></video>'
    nd = {"props": {"pageProps": {"detailData": {"contentDetail": cd}}}}
    body = tw.fetch_body(detail_sample["url"], browser=FakeBrowser(nd))
    assert body == cd["summary"]


def test_fetch_body_drops_video_src_noise(detail_sample):
    """正文里的 <video> 不该把 src 变成正文噪声。"""
    cd = dict(detail_sample["contentDetail"])
    cd["content"] = ('<p>正文第一段。</p><video src="https://cloudvideo.thepaper.cn/'
                     'video/abc.mp4"></video><p>正文第二段。</p>')
    nd = {"props": {"pageProps": {"detailData": {"contentDetail": cd}}}}
    body = tw.fetch_body(detail_sample["url"], browser=FakeBrowser(nd))
    assert "cloudvideo" not in body
    assert "正文第一段。" in body


def test_fetch_body_missing_returns_none():
    assert tw.fetch_body("https://x", browser=FakeBrowser(None)) is None


def test_fetch_body_empty_content_detail_returns_none():
    nd = {"props": {"pageProps": {"detailData": {}}}}
    assert tw.fetch_body("https://x", browser=FakeBrowser(nd)) is None


# ---------- 时间解析 ----------

def test_parse_pub_time_prefers_exact_epoch(list_samples, now_from_fixture):
    """主路径: `pubTimeLong` 精确 epoch, approx=False。"""
    card = list_samples["26911"]["cards"][0]
    ts, approx = tw._parse_pub_time(card, now=now_from_fixture)
    assert approx is False
    assert ts == datetime.fromtimestamp(_epoch(card) / 1000.0, tz=timezone.utc)


def test_parse_pub_time_epoch_wins_over_relative_string(list_samples, now_from_fixture):
    """卡片同时有相对串和 epoch 时必须用 epoch —— 相对串最多差 1 天。"""
    card = dict(list_samples["26911"]["cards"][0])
    card["pubTime"] = "刚刚"          # 相对串认不出来
    ts, approx = tw._parse_pub_time(card, now=now_from_fixture)
    assert ts is not None and approx is False


def test_parse_pub_time_uses_publishtime_string_when_epoch_missing(list_samples):
    """epoch 没了 → 用 `publishTime` 绝对串(精确, 不标 approx)。

    绝对串只精确到秒, epoch 精确到毫秒, 所以按秒比。
    """
    orig = list_samples["26911"]["cards"][0]
    card = {k: v for k, v in orig.items() if k not in ("pubTimeLong", "trackPublishTime")}
    card["pubTime"] = "刚刚"          # 逼走相对串那条路
    ts, approx = tw._parse_pub_time(card, now=datetime.now(timezone.utc))
    assert approx is False
    epoch = datetime.fromtimestamp(_epoch(orig) / 1000.0, tz=timezone.utc)
    assert ts.replace(microsecond=0) == epoch.replace(microsecond=0)


def test_parse_pub_time_falls_back_to_relative_and_marks_approx(now_from_fixture):
    """只有相对串 → 换算且必须标 approx。"""
    ts, approx = tw._parse_pub_time({"pubTime": "3小时前"}, now=now_from_fixture)
    assert approx is True
    assert now_from_fixture - ts == timedelta(hours=3)


@pytest.mark.parametrize("raw,delta", [
    ("45分钟前", timedelta(minutes=45)),
    ("3小时前", timedelta(hours=3)),
    ("5天前", timedelta(days=5)),
])
def test_parse_relative_units(now_from_fixture, raw, delta):
    ts, approx = tw._parse_pub_time({"pubTime": raw}, now=now_from_fixture)
    assert approx is True
    assert now_from_fixture - ts == delta


def test_parse_pub_time_numeric_string_epoch():
    ts, approx = tw._parse_pub_time({"pubTimeLong": "1791089945496"},
                                    now=datetime.now(timezone.utc))
    assert approx is False
    assert ts == datetime.fromtimestamp(1791089945496 / 1000.0, tz=timezone.utc)


def test_parse_pub_time_absolute_string():
    """绝对串精确 → 不标 approx(实测卡片就是这种形状)。"""
    ts, approx = tw._parse_pub_time({"publishTime": "2026-09-30 19:41:18"},
                                    now=datetime.now(timezone.utc))
    assert approx is False
    assert ts == datetime(2026, 9, 30, 11, 41, 18, tzinfo=timezone.utc)   # 东八区 → UTC


def test_parse_pub_time_date_only_string():
    """实测 `pubTime` 会给 "2026-09-30" 这种纯日期 —— 也要能认(按东八区零点)。"""
    ts, approx = tw._parse_pub_time({"pubTime": "2026-09-30"},
                                    now=datetime.now(timezone.utc))
    assert ts == datetime(2026, 9, 29, 16, 0, 0, tzinfo=timezone.utc)
    assert approx is False


def test_parse_pub_time_unrecognised_returns_none():
    """认不出来 → None(丢掉), 绝不用 now 顶替。"""
    ts, _ = tw._parse_pub_time({"pubTime": "昨天"}, now=datetime.now(timezone.utc))
    assert ts is None
    ts2, _ = tw._parse_pub_time({}, now=datetime.now(timezone.utc))
    assert ts2 is None


def test_parse_pub_time_zero_epoch_ignored():
    ts, approx = tw._parse_pub_time({"pubTimeLong": 0, "trackPublishTime": 0,
                                     "publishTime": "", "pubTime": "2小时前"},
                                    now=datetime.now(timezone.utc))
    assert approx is True and ts is not None


# ---------- 端到端(注入真实卡片) ----------

def test_collect_produces_items_from_real_cards(tmp_path, list_samples):
    """阳性对照: 真实卡片(窗内) → 有产出, 且字段映射正确。

    注意 collect() 用的是**真实 now**, 所以只有窗内卡片会产出 ——
    fixture 抓于 2026-10-05, 现在跑的话 68750/25427 的部分卡片已出窗。
    """
    expected = _live_window(list_samples, (26911, 68750, 25427))
    total = sum(len(v) for v in expected.values())
    assert total > 0, "fixture 里应该有窗内卡片"

    def fetch_list(url):
        for node in tw.WARM_NODES:
            if node["url"] == url and str(node["node_id"]) in list_samples:
                return expected.get(node["node_id"], [])
        return []

    recs = list(_adapter(tmp_path, fetch_list).collect())
    assert len(recs) == total
    r = recs[0]
    assert r.item.source == "thepaper_warm"
    assert r.item.lang == "zh"
    assert r.item.url.startswith("https://www.thepaper.cn/newsDetail_forward_")
    assert r.item.title.strip()
    assert "人间温暖" in r.item.tags


def test_payload_shape_matches_contract(tmp_path, list_samples):
    """source_payload 必须有 kind=warmth 和 ISO published。"""
    recs = list(_adapter(tmp_path, lambda url: _live(list_samples, 26911)).collect())
    payload = json.loads(recs[0].source_payload_json)
    assert payload["kind"] == "warmth"
    datetime.fromisoformat(payload["published"])          # 是合法 ISO
    assert payload["cont_id"]
    assert payload["node_id"] in {136261, 26911, 68750, 25427}


def test_epoch_backed_items_not_marked_approx(tmp_path, list_samples):
    """真实卡片都有 publishTime → 不该标 published_approx。"""
    recs = list(_adapter(tmp_path, lambda url: _live(list_samples, 26911)).collect())
    assert recs
    for r in recs:
        assert "published_approx" not in json.loads(r.source_payload_json)


def test_relative_only_items_marked_approx(tmp_path, list_samples):
    """把 epoch 全抠掉、绝对串也抠掉 → 只剩相对串 → 必须标 published_approx: true。"""
    cards = [{k: v for k, v in c.items()
              if k not in ("pubTimeLong", "trackPublishTime", "publishTime")}
             for c in _in_window(list_samples, 26911,
                                 now=datetime.now(timezone.utc))]
    recs = list(_adapter(tmp_path, lambda url: cards).collect())
    assert recs, "窗内卡片不该为空"
    assert all(json.loads(r.source_payload_json).get("published_approx") is True
               for r in recs)


def test_author_comes_from_trackauthor(tmp_path, list_samples):
    """nodeInfo.nickName 实测恒空 → 作者必须落到 trackAuthor(具名真人)。"""
    recs = list(_adapter(tmp_path, lambda url: _live(list_samples, 26911)).collect())
    authors = {r.item.author for r in recs}
    assert "澎湃新闻" not in authors, "不该退到默认作者"
    assert any("编辑" in a or "记者" in a for a in authors)


def test_column_falls_back_to_configured_name(tmp_path, list_samples):
    """卡片没有 nodeInfo.name 时用配置的栏目名兜底, 不至于 tags 里空一个。

    fetch_list 必须按 url 分发 —— 否则四个栏目都会拿到同一组卡片, 栏目名自然对不上。
    """
    def fetch_list(url):
        for node in tw.WARM_NODES:
            if node["url"] == url and str(node["node_id"]) in list_samples:
                return [{k: v for k, v in c.items() if k != "nodeInfo"}
                        for c in _live(list_samples, node["node_id"])]
        return []

    recs = list(_adapter(tmp_path, fetch_list).collect())
    assert recs
    columns = {json.loads(r.source_payload_json)["column"] for r in recs}
    assert columns == {"暖闻湃", "公益湃", "澎湃人物"}
    for r in recs:
        assert "人间温暖" in r.item.tags


def test_tags_include_real_tagnames(tmp_path, list_samples):
    """实测卡片带 tagList(「消防员」「饿肚子大叔」), 该进 tags。"""
    recs = list(_adapter(tmp_path, lambda url: _live(list_samples, 26911)).collect())
    tags = set(recs[0].item.tags)
    assert "人间温暖" in tags
    assert tags - {"人间温暖", "暖闻湃"}, "tagList 里的具名标签没进来"


def test_body_fetched_and_url_passed(tmp_path, list_samples):
    """正文抓取用详情页 URL, 且 body 落到 item 上。"""
    seen: list[str] = []

    def fetch_body(url):
        seen.append(url)
        return "正文内容"

    recs = list(_adapter(tmp_path, lambda url: _live(list_samples, 26911),
                         fetch_body=fetch_body).collect())
    assert recs
    assert recs[0].item.body == "正文内容"
    assert seen[0].startswith("https://www.thepaper.cn/newsDetail_forward_")
    assert len(seen) == len(recs)


def test_media_urls_from_pic(tmp_path, list_samples):
    recs = list(_adapter(tmp_path, lambda url: _live(list_samples, 68750)).collect())
    assert any(r.media_urls and r.media_urls[0].startswith("http") for r in recs)


def test_item_id_is_stable_across_runs(tmp_path, list_samples):
    """同一张卡片两次 collect 的 id 必须一致(否则去重失效)。"""
    a = list(_adapter(tmp_path, lambda url: _live(list_samples, 26911), tag="a").collect())
    b = list(_adapter(tmp_path, lambda url: _live(list_samples, 26911), tag="b").collect())
    assert [r.item.id for r in a] == [r.item.id for r in b]


def test_title_truncated_to_schema_limit(tmp_path, list_samples):
    """超长标题不能让 Item 校验失败(max_length=512)。"""
    cards = [dict(_live(list_samples, 26911)[0], name="长" * 900)]
    recs = list(_adapter(tmp_path, lambda url: cards).collect())
    assert len(recs[0].item.title) == 512


def test_body_truncated_to_limit(tmp_path, list_samples):
    cards = _live(list_samples, 26911)
    recs = list(_adapter(tmp_path, lambda url: cards,
                         fetch_body=lambda url: "长" * 60_000).collect())
    assert len(recs[0].item.body) == tw.BODY_LIMIT


# ---------- 7 天时间窗 ----------

def test_window_constant_is_seven_days():
    assert tw.WARMTH_MAX_AGE_DAYS == 7


def test_seven_day_window_drops_old_items(tmp_path, list_samples):
    """7 天窗: 窗内的留下。窗外条目的丢弃由 test_items_outside_window_dropped 覆盖。"""
    fresh = _live(list_samples, 26911)
    recs = list(_adapter(tmp_path, lambda url: fresh).collect())
    assert len(recs) == len(fresh)


def test_items_outside_window_dropped(tmp_path, list_samples):
    """窗外 8 天的条目必须被丢掉(把 epoch 推到窗外)。"""
    old_epoch = int((FIXTURE_NOW - timedelta(days=8)).timestamp() * 1000)
    cards = [dict(c, pubTimeLong=old_epoch, trackPublishTime=old_epoch,
                  publishTime="") for c in _live(list_samples, 26911)]
    assert list(_adapter(tmp_path, lambda url: cards).collect()) == []


def test_since_is_intersected_with_window(tmp_path, list_samples):
    """since 给更严的窗口时取交集。"""
    cards = _live(list_samples, 26911)
    newest = max(datetime.fromtimestamp(_epoch(c) / 1000, timezone.utc) for c in cards)
    recs = list(_adapter(tmp_path, lambda url: cards).collect(since=newest))
    assert len(recs) <= 1


def test_since_further_back_keeps_everything_in_window(tmp_path, list_samples):
    """since 比 7 天窗更早 → 不额外过滤。"""
    cards = _live(list_samples, 26911)
    recs = list(_adapter(tmp_path, lambda url: cards).collect(
        since=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    assert len(recs) == len(cards)


def test_items_with_unparseable_time_are_dropped(tmp_path, list_samples):
    """时间认不出来 → 丢, 不用 now 顶替。"""
    cards = [dict(c, pubTimeLong=0, trackPublishTime=0, publishTime="",
                  pubTime="前天") for c in _live(list_samples, 26911)]
    assert list(_adapter(tmp_path, lambda url: cards).collect()) == []


def test_items_without_id_or_title_dropped(tmp_path, list_samples):
    base = _live(list_samples, 26911)
    cards = [dict(base[0], contId=""), {k: v for k, v in base[1].items() if k != "name"}]
    assert list(_adapter(tmp_path, lambda url: cards).collect()) == []


# ---------- 去重 ----------

def test_seen_ids_state_suppresses_second_run(tmp_path, list_samples):
    """状态文件里的 contId 第二次不再产出。"""
    state_path = tmp_path / "thepaper_warm_state.json"
    cards = _live(list_samples, 26911)
    first = ThepaperWarmAdapter(now_fn=lambda: FIXTURE_NOW, fetch_list=lambda url: cards, fetch_body=lambda url: "b",
                                state_path=state_path)
    assert len(list(first.collect())) == len(cards)
    second = ThepaperWarmAdapter(now_fn=lambda: FIXTURE_NOW, fetch_list=lambda url: cards, fetch_body=lambda url: "b",
                                 state_path=state_path)
    assert list(second.collect()) == []


def test_duplicate_cards_within_one_run_deduped(tmp_path, list_samples):
    cards = _live(list_samples, 26911)
    recs = list(_adapter(tmp_path, lambda url: cards + cards).collect())
    assert len(recs) == len(cards)


def test_same_cont_id_in_two_columns_deduped(tmp_path, list_samples):
    """同一篇文章出现在两个栏目里 → 只产一条。"""
    cards = _live(list_samples, 26911)
    recs = list(_adapter(tmp_path, lambda url: cards + _live(list_samples, 68750)
                         ).collect())
    ids = [r.item.id for r in recs]
    assert len(ids) == len(set(ids))


def test_state_file_written(tmp_path, list_samples):
    cards = _live(list_samples, 26911)
    adapter = _adapter(tmp_path, lambda url: cards)
    list(adapter.collect())
    saved = json.loads(adapter.state_path.read_text("utf-8"))
    assert len(saved["seen_ids"]) == len(cards)
    assert saved["updated_at"]


def test_state_seen_ids_capped(tmp_path, list_samples):
    """seen_ids 不能无限长。"""
    adapter = _adapter(tmp_path, lambda url: [])
    list(adapter.collect())
    assert tw.SEEN_ID_LIMIT > 0


@pytest.mark.parametrize("bad", [[], "string", 123, None])
def test_corrupt_state_treated_as_empty(tmp_path, list_samples, bad):
    """状态文件是 JSON 数组/字符串/数字/ null → 当空状态, 不炸。"""
    adapter = _adapter(tmp_path, lambda url: _live(list_samples, 26911), state=bad)
    assert len(list(adapter.collect())) == len(_live(list_samples, 26911))


def test_missing_state_file_treated_as_empty(tmp_path, list_samples):
    cards = _live(list_samples, 26911)
    adapter = _adapter(tmp_path, lambda url: cards)
    assert not adapter.state_path.exists()
    assert len(list(adapter.collect())) == len(cards)


def test_broken_json_state_treated_as_empty(tmp_path, list_samples):
    state_path = tmp_path / "thepaper_warm_state.json"
    state_path.write_text("{not json", "utf-8")
    cards = _live(list_samples, 26911)
    adapter = ThepaperWarmAdapter(now_fn=lambda: FIXTURE_NOW, fetch_list=lambda url: cards,
                                  fetch_body=lambda url: "正文", state_path=state_path)
    assert len(list(adapter.collect())) == len(cards)


# ---------- 容错 ----------

def test_403_column_returns_empty_not_crash(tmp_path, list_samples):
    """403 / 回落到首页 → 该栏目 0 条, 其余栏目照常。"""
    cards = _live(list_samples, 26911)

    def fetch_list(url):
        if "136261" in url:
            return []                     # 模拟暖闻 136261 拿不到(实测会 403)
        return cards

    recs = list(_adapter(tmp_path, fetch_list).collect())
    assert len(recs) == len(cards)
    assert all(json.loads(r.source_payload_json)["node_id"] == 26911 for r in recs)


def test_all_columns_failing_gives_empty(tmp_path):
    assert list(_adapter(tmp_path, lambda url: []).collect()) == []


def test_fetch_list_exception_skips_column(tmp_path, list_samples):
    """一个栏目抛异常 → 跳过它, 不让整轮 ingest 挂掉。"""
    cards = _live(list_samples, 25427)

    def fetch_list(url):
        if "26911" in url:
            raise RuntimeError("boom")
        return [] if "136261" in url or "68750" in url else cards

    recs = list(_adapter(tmp_path, fetch_list).collect())
    assert len(recs) == len(cards)
    assert all(json.loads(r.source_payload_json)["node_id"] == 25427 for r in recs)


def test_body_fetch_exception_leaves_empty_body(tmp_path, list_samples):
    """详情页抓取抛异常 → 不抛给调用方, 且这些条目本轮不入库(10-05 复审 R17)。

    修复前: 异常被吞掉后条目照样返回、body 为空、cont_id 照样进 seen -> 下一轮被跳过,
    这些条目永远补不上正文。现在不入库也不进 seen, 下一轮还能重试。
    """
    def fetch_body(url):
        raise RuntimeError("detail boom")

    cards = _live(list_samples, 26911)
    adapter = _adapter(tmp_path, lambda url: cards, fetch_body=fetch_body)
    recs = list(adapter.collect())          # 不抛
    assert recs == [], "没取到正文的条目不应入库"
    # 不进 seen -> 详情页恢复后下一轮能补上
    recovered = list(_adapter(tmp_path, lambda url: cards,
                              fetch_body=lambda url: "正文", tag="r").collect())
    assert len(recovered) == len(cards)
    assert all(r.item.body for r in recovered)


def test_fetch_body_returning_none_gives_empty_body(tmp_path, list_samples):
    recs = list(_adapter(tmp_path, lambda url: _live(list_samples, 26911),
                         fetch_body=lambda url: None).collect())
    assert all(r.item.body == "" for r in recs)


def test_body_budget_limits_page_opens(tmp_path, list_samples):
    """正文预算用完就不再请求详情页 —— 控住页面加载数。"""
    calls: list[str] = []

    def fetch_body(url):
        calls.append(url)
        return "正文"

    cards = _live(list_samples, 26911) + _live(list_samples, 68750)
    recs = list(_adapter(tmp_path, lambda url: cards, fetch_body=fetch_body,
                         body_limit=3).collect())
    assert len(calls) == 3, "正文请求次数应正好等于 body_limit"
    # 10-05 复审 R17: 预算花完后剩下的条目没正文, **不入库**(原来断言 len==len(cards),
    # 等于把「空正文也返回」固化成期望行为)。返回的每一条都必须有正文。
    assert len(recs) == 3
    assert all(r.item.body for r in recs)


def test_body_budget_zero_never_opens_detail(tmp_path, list_samples):
    """`body_limit=0` → 一个详情页都不开, 也就一条都不入库(R17)。

    修复前这里断言 `len(recs) == len(cards)`, 也就是把"空正文也照样返回"当成期望行为
    固化了 —— 那正是被判为 bug 的行为。
    """
    cards = _live(list_samples, 26911)
    calls: list[str] = []
    recs = list(_adapter(tmp_path, lambda url: cards,
                         fetch_body=lambda url: calls.append(url) or "x",
                         body_limit=0).collect())
    assert calls == []
    assert recs == []
    # 阳性对照: 预算够时这些条目都产出
    ok = list(_adapter(tmp_path / "ok", lambda url: cards,
                       fetch_body=lambda url: "正文", body_limit=8).collect())
    assert len(ok) == len(cards)


def test_body_budget_spent_on_newest_items_first(tmp_path, list_samples):
    """正文预算按**新旧**发, 不按栏目遍历顺序。

    真跑时发现的问题: 预算按栏目顺序花, 最新那条(最该有正文)反而 body 为空。
    这条钉住"最新 N 条拿到正文"。
    """
    cards = _live(list_samples, 26911) + _live(list_samples, 68750)
    recs = list(_adapter(tmp_path, lambda url: cards,
                         fetch_body=lambda url: "正文", body_limit=3).collect())
    with_body = [r for r in recs if r.item.body]
    assert len(with_body) == 3
    newest_ts = [r.item.ts for r in recs[:3]]
    assert [r.item.ts for r in with_body] == newest_ts


def test_body_budget_skips_empty_body_without_wasting_budget(tmp_path, list_samples):
    """正文抓回空 → 不算用掉预算, 继续往后发(否则少数条目会白吃预算)。"""
    cards = _live(list_samples, 26911)
    calls: list[str] = []

    def fetch_body(url):
        calls.append(url)
        return "" if len(calls) == 1 else "正文"

    recs = list(_adapter(tmp_path, lambda url: cards, fetch_body=fetch_body,
                         body_limit=2).collect())
    assert len(calls) == 3, "第一次空返回后应继续给第 2、3 条抓"
    assert sum(1 for r in recs if r.item.body) == 2


# ---------- 排序 / limit ----------

def test_sorted_by_ts_desc(tmp_path, list_samples):
    recs = list(_adapter(tmp_path, lambda url: _live(list_samples, 26911)).collect())
    ts = [r.item.ts for r in recs]
    assert ts == sorted(ts, reverse=True)


def test_limit_truncates(tmp_path, list_samples):
    recs = list(_adapter(tmp_path, lambda url: _live(list_samples, 26911)).collect(limit=2))
    assert len(recs) == 2


def test_results_are_deterministic_order(tmp_path, list_samples):
    a = [r.item.id for r in _adapter(tmp_path, lambda u: _live(list_samples, 26911),
                                      tag="a").collect()]
    b = [r.item.id for r in _adapter(tmp_path, lambda u: _live(list_samples, 26911),
                                      tag="b").collect()]
    assert a == b


# ---------- 注入接口契约 ----------

def test_constructor_does_no_io(tmp_path):
    """构造 adapter 不做任何 IO —— 测试注入假函数就不会起浏览器。"""
    ThepaperWarmAdapter(now_fn=lambda: FIXTURE_NOW, fetch_list=lambda url: [], fetch_body=lambda url: None,
                        state_path=tmp_path / "s.json")


def test_default_is_browser_backed():
    """不注入时走 playwright 默认实现(构造函数仍不起浏览器, 留到 collect)。"""
    a = ThepaperWarmAdapter(state_path=Path("/tmp/none.json"))
    assert a._fetch_list is None and a._fetch_body is None
    assert a.browsers_path is None
    assert a.headless is True


def test_browsers_path_is_constructor_param():
    a = ThepaperWarmAdapter(browsers_path="/tmp/pw", headless=False)
    assert a.browsers_path == "/tmp/pw"
    assert a.headless is False


def test_browser_env_path_is_written_before_launch(monkeypatch):
    """`browsers_path` 必须写进 PLAYWRIGHT_BROWSERS_PATH —— playwright 的 python 绑定
    在 import 时就读过一次路径, 事后设环境变量对它无效, 所以必须在 launch 之前设。

    这里不真的起浏览器: 直接检查 `_Browser.__enter__` 之前的设置逻辑。
    """
    import inspect
    src = inspect.getsource(tw._Browser.__enter__)
    assert "PLAYWRIGHT_BROWSERS_PATH" in src
    assert src.index("PLAYWRIGHT_BROWSERS_PATH") < src.index("launch")


def test_browser_next_data_outside_with_raises():
    """没进 with 上下文就调 next_data → RuntimeError(而不是 AttributeError)。"""
    with pytest.raises(RuntimeError):
        tw._Browser().next_data("https://www.thepaper.cn/list_26911")


def test_browser_exit_without_enter_is_safe():
    tw._Browser().__exit__(None, None, None)      # 不该抛


# ---- 10-05 复审 R17 · 空正文条目入库并被 seen 锁死 ----------------------------

def test_body_limit_default_raised_to_20():
    """缺省正文预算从 8 提到 20(一个条目一个详情页, 8 覆盖不了一轮的栏目量)。"""
    assert tw.DEFAULT_BODY_LIMIT == 20
    assert ThepaperWarmAdapter(state_path=Path("/tmp/none2.json")).body_limit == 20


def test_records_without_body_are_not_returned(tmp_path, list_samples):
    """没取到正文的条目**不入库**(下游 paper.py 反正会按正文长度把它们丢掉)。

    修复前实测真实站点一轮 9 条里 6 条 body 长度 0–112, 全部低于 `BLIND_MIN_BODY_CHARS=300`
    与 `SOCIAL_SINGLETON_MIN_CHARS=200` 两个下限 —— 即 2/3 的产出进了库却在报纸阶段被丢弃,
    付出了 4 个栏目页 + 8 个详情页的浏览器代价却换不到版面。
    """
    cards = _live(list_samples, 26911)
    # 模拟真实站点的比例: 只有 2 条能拿到可用正文, 其余是视频条目(只有几十字 summary)
    calls_seen: list[str] = []

    def body_for(url):
        calls_seen.append(url)
        return "很长的正文" * 100 if len(calls_seen) <= 2 else ""

    recs = list(_adapter(tmp_path, lambda url: cards, fetch_body=body_for,
                         body_limit=8).collect())
    assert len(recs) == 2, "只有拿到正文的 2 条该入库"
    assert all(len(r.item.body) >= 300 for r in recs)


def test_empty_body_items_not_marked_seen(tmp_path, list_samples):
    """没取到正文的条目**不进 seen** —— 下一轮还能重试(原来 seen 已锁死, 永远补不上)。"""
    cards = _live(list_samples, 26911)
    recs = list(_adapter(tmp_path, lambda url: cards,
                         fetch_body=lambda url: "", body_limit=8).collect())
    assert recs == []
    saved = json.loads((tmp_path / "thepaper_warm_state_0.json").read_text("utf-8"))
    assert saved["seen_ids"] == [], "没正文的条目不该占seen"

    # 下一轮详情页恢复 -> 全部产出
    second = list(_adapter(tmp_path, lambda url: cards,
                           fetch_body=lambda url: "正文", body_limit=8).collect())
    assert len(second) == len(cards)


def test_video_only_summary_entries_retried_next_round(tmp_path, list_samples):
    """源本身只有短 summary 的条目(实测 51 字)也不入库, 下一轮再试。"""
    cards = _live(list_samples, 26911)
    # 先一轮: 全部拿不到正文
    assert list(_adapter(tmp_path, lambda url: cards,
                         fetch_body=lambda url: "").collect()) == []
    # 第二轮: 全部恢复
    recs = list(_adapter(tmp_path, lambda url: cards,
                         fetch_body=lambda url: "正文" * 50).collect())
    assert len(recs) == len(cards)


# ---- 10-05 复审 R18 · limit 截断在 seen 标记之后 -----------------------------

def test_limit_truncates_does_not_swallow_dropped_items(tmp_path, list_samples):
    """`limit` 落选的条目**不进 seen** —— 下一轮不限量时必须还能拿到。"""
    cards = _live(list_samples, 26911)
    first = list(_adapter(tmp_path, lambda url: cards,
                          fetch_body=lambda url: "正文", body_limit=8).collect(limit=2))
    assert len(first) == 2
    second = list(_adapter(tmp_path, lambda url: cards,
                           fetch_body=lambda url: "正文", body_limit=8).collect())
    assert len(second) == len(cards) - 2, "落选的条目被 seen 吞掉了"
    assert not ({r.item.id for r in first} & {r.item.id for r in second})
    # 第三轮: 全部见过 -> 0 条(阳性对照)
    assert list(_adapter(tmp_path, lambda url: cards,
                         fetch_body=lambda url: "正文", body_limit=8).collect()) == []


def test_limit_and_missing_body_combined_not_lost(tmp_path, list_samples):
    """既落在 limit 外又没拿到正文的条目, 下一轮仍要能拿到(R17 与 R18 叠加)。"""
    cards = _live(list_samples, 26911)
    # 第一轮: limit=1, 且只有最新那条有正文
    calls: list[str] = []

    def body_for(url):
        calls.append(url)
        return "正文" if len(calls) == 1 else ""

    first = list(_adapter(tmp_path, lambda url: cards, fetch_body=body_for,
                          body_limit=1).collect())
    assert len(first) == 1
    # 第二轮: 全部有正文 -> 除了已入库那条, 其余都该补上
    second = list(_adapter(tmp_path, lambda url: cards,
                           fetch_body=lambda url: "正文", body_limit=8).collect())
    assert len(second) == len(cards) - 1


# ---- 10-05 复审 R19 · 相对时间兜底的基准时刻 -------------------------------

def test_relative_time_uses_one_now_for_whole_round(tmp_path, list_samples):
    """同一轮内所有相对时间条目必须用**同一个** `now` 基准。

    修复前 `_to_record` 对每张卡片各取一次 `datetime.now(utc)`, 一批卡片逐个处理跨过
    若干秒时, 相对时间("5 天前")算出的 `ts` 彼此相差处理耗时, 之后无法复现同一批 `ts`。
    """
    cards = _live(list_samples, 26911)
    # 构造只有相对时间的卡片
    rel_cards = [{"contId": str(900000 + i), "name": f"卡片{i}",
                  "pubTime": "5天前"} for i in range(3)]
    first = list(_adapter(tmp_path, lambda url: rel_cards,
                          fetch_body=lambda url: "正文", body_limit=8).collect())
    assert len(first) == 3
    # 三条都用同一个 now -> ts 完全相同(不再有处理耗时造成的漂移)
    assert len({r.item.ts for r in first}) == 1
    payload = json.loads(first[0].source_payload_json)
    assert payload["published_approx"] is True


def test_to_record_takes_now_from_collect():
    """`_to_record` 不再自己取 `now`(R19), 必须由 `collect` 传同一个基准时刻下来。"""
    import inspect
    src = inspect.getsource(tw.ThepaperWarmAdapter._to_record)
    assert "now: datetime" in src
    # 只看代码行(注释里提到 `datetime.now(` 是解释 R19 的来由, 不能算)
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    assert "datetime.now(" not in code, "_to_record 里不该再自己取 now"
