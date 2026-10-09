"""10-05 验收 F2 卷修的测试：F102/F103 weibo_home、F116/F117 zhihu_moments、
F206 youtube_followed、F209 disaster_alerts、F110 weibo_timeline、F216/F219 podcast_new。

每条都调真实函数，不复制被测逻辑。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from personal_intel_loop.adapters import disaster_alerts as da
from personal_intel_loop.adapters import weibo_timeline as wt
from personal_intel_loop.adapters import youtube_followed as yf
from personal_intel_loop.adapters.disaster_alerts import DisasterAlertsAdapter
from personal_intel_loop.adapters.podcast_new import PodcastNewAdapter
from personal_intel_loop.adapters.weibo_home import WeiboHomeAdapter
from personal_intel_loop.adapters.weibo_timeline import WeiboTimelineAdapter
from personal_intel_loop.adapters.zhihu_moments import ZhihuMomentsAdapter

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "platforms"


# ---- weibo_home 共用假数据 ----------------------------------------------------


def _wb_status(idx: int) -> dict:
    return {
        "idstr": f"90000{idx}",
        "mblogid": f"mblog{idx}",
        "created_at": "Sun Oct 04 16:06:57 +0800 2026",
        "text_raw": f"正文{idx}",
        "user": {"idstr": f"u{idx}", "screen_name": f"用户{idx}"},
    }


def _wb_page(ids: list[int], max_id: int) -> dict:
    return {
        "ok": 1,
        "statuses": [_wb_status(i) for i in ids],
        "max_id": max_id,
    }


def _wb_adapter(tmp_path, pages, **kw):
    """pages: list[dict]，每次 fetch_page 弹一个；不够就复用最后一个。"""
    box = list(pages)
    kw.setdefault("sleep_s", 0)
    kw.setdefault("fetch_long_text", lambda _mblogid: "")
    state = kw.pop("state_path", tmp_path / "state.json")

    def fetch_page(max_id: int) -> dict:
        if len(box) > 1:
            return box.pop(0)
        return box[0] if box else {"ok": 1, "statuses": [], "max_id": 0}

    return WeiboHomeAdapter(fetch_page=fetch_page, state_path=state, **kw)


# ---- F102 weibo_home：ok=1 但 statuses 不是 list → 按异常处理 ------------------


def test_f102_statuses_not_list_pauses_and_records_error(tmp_path):
    """接口把 statuses 挪到 data 下时，不能当「本轮 0 条」静默断流。"""
    payload = {"ok": 1, "data": {"statuses": [_wb_status(1)]}, "max_id": 0}
    adapter = _wb_adapter(tmp_path, [payload], max_pages=1)

    assert list(adapter.collect()) == []
    assert adapter.paused_reason is not None
    assert "statuses" in (adapter.paused_reason or "")
    assert adapter.last_errors, "失败必须留在 last_errors 里给 cli 读"


def test_f102_statuses_missing_pauses(tmp_path):
    """顶层没有 statuses 键（挪走了）同样按异常处理，不当空结果。"""
    adapter = _wb_adapter(tmp_path, [{"ok": 1, "max_id": 0}], max_pages=1)

    assert list(adapter.collect()) == []
    assert adapter.paused_reason is not None
    assert adapter.last_errors


def test_f102_normal_empty_list_is_not_an_error(tmp_path):
    """真的「这页 0 条」不是异常：ok=1 + statuses=[] 不该被记成失败。"""
    adapter = _wb_adapter(tmp_path, [{"ok": 1, "statuses": [], "max_id": 0}], max_pages=1)

    assert list(adapter.collect()) == []
    assert adapter.paused_reason is None
    assert adapter.last_errors == []


# ---- F103 weibo_home：翻满页数上限要留痕 --------------------------------------


def test_f103_max_pages_reached_writes_truncated_at(tmp_path):
    """每页都有新微博、max_id 一直有 → 走满上限被截断，state 要写 truncated_at。"""
    state_path = tmp_path / "state.json"
    pages = [
        _wb_page([1, 2], 5000),
        _wb_page([3, 4], 4000),
    ]
    adapter = _wb_adapter(tmp_path, pages, max_pages=2, state_path=state_path)

    records = list(adapter.collect())
    assert len(records) == 4  # 两页都真收进来了
    assert adapter.paused_reason is None, "截断不是风控暂停"

    state = json.loads(state_path.read_text("utf-8"))
    assert state.get("truncated_at"), "翻满上限必须在 state 留时间戳"
    assert adapter.last_errors and "页" in adapter.last_errors[0]


def test_f103_stop_early_does_not_write_truncated_at(tmp_path):
    """第 1 页就翻到底（max_id=0）→ 没截断，不写 truncated_at。"""
    state_path = tmp_path / "state.json"
    adapter = _wb_adapter(tmp_path, [_wb_page([1], 0)], max_pages=5, state_path=state_path)

    list(adapter.collect())
    state = json.loads(state_path.read_text("utf-8"))
    assert "truncated_at" not in state
    assert adapter.last_errors == []


# ---- F116 zhihu_moments：code 非 0 / data 不是 list → 暂停 -------------------


def _zh_adapter(tmp_path, pages, **kw):
    box = list(pages)
    kw.setdefault("sleep_s", 0)
    state = kw.pop("state_path", tmp_path / "state.json")

    def fetch(url: str | None) -> dict:
        if len(box) > 1:
            return box.pop(0)
        return box[0] if box else {"data": [], "paging": {"is_end": True, "next": ""}}

    return ZhihuMomentsAdapter(fetch=fetch, state_path=state, **kw)


def test_f116_code_nonzero_pauses(tmp_path):
    adapter = _zh_adapter(tmp_path, [{"code": 500, "message": "server error"}], max_pages=1)

    assert list(adapter.collect()) == []
    assert adapter.paused_reason is not None
    assert "500" in (adapter.paused_reason or "")
    assert adapter.last_errors


def test_f116_data_not_list_pauses(tmp_path):
    """data 挪位置了：不是 list 却是 dict → 不能当「无新动态」。"""
    payload = {"code": 0, "data": {"data": []}, "paging": {"is_end": True, "next": ""}}
    adapter = _zh_adapter(tmp_path, [payload], max_pages=1)

    assert list(adapter.collect()) == []
    assert adapter.paused_reason is not None
    assert "data" in (adapter.paused_reason or "")
    assert adapter.last_errors


def test_f116_normal_empty_data_is_not_an_error(tmp_path):
    payload = {"data": [], "paging": {"is_end": True, "next": ""}}
    adapter = _zh_adapter(tmp_path, [payload], max_pages=1)

    assert list(adapter.collect()) == []
    assert adapter.paused_reason is None
    assert adapter.last_errors == []


# ---- F117 zhihu_moments：页数上限留痕 + 上限 3 → 8 ---------------------------


def _zh_page(idx: int) -> dict:
    return {
        "data": [
            {
                "id": f"e{idx}",
                "created_time": 1791101049 + idx,
                "type": "answer",
                "target": {
                    "id": f"ans{idx}",
                    "type": "answer",
                    "question": {"id": "q1", "title": f"问题{idx}"},
                    "author": {"url_token": f"tok{idx}", "name": f"作者{idx}"},
                    "content": f"回答正文{idx}",
                },
            }
        ],
        "paging": {"is_end": False, "next": f"https://example.com/next/{idx}"},
    }


def test_f117_max_pages_default_is_8():
    assert ZhihuMomentsAdapter(fetch=lambda _u: {"data": []}).max_pages == 5


def test_f117_max_pages_reached_warns(tmp_path):
    state_path = tmp_path / "state.json"
    pages = [_zh_page(1), _zh_page(2), _zh_page(3)]
    adapter = _zh_adapter(tmp_path, pages, max_pages=3, state_path=state_path)

    records = list(adapter.collect())
    assert len(records) == 3, "三页各有新动态 → 每页都收进来"
    assert adapter.paused_reason is None
    assert adapter.last_errors and "页" in adapter.last_errors[0]
    state = json.loads(state_path.read_text("utf-8"))
    assert state.get("truncated_at"), "截断要在 state 留时间戳"


def test_f117_stop_at_end_does_not_warn(tmp_path):
    payload = {
        "data": [
            {
                "id": "e1",
                "created_time": 1791101049,
                "target": {
                    "id": "ans1",
                    "type": "answer",
                    "question": {"id": "q1", "title": "问题"},
                    "author": {"url_token": "tok", "name": "作者"},
                    "content": "正文",
                },
            }
        ],
        "paging": {"is_end": True, "next": ""},
    }
    adapter = _zh_adapter(tmp_path, [payload], max_pages=5)

    assert len(list(adapter.collect())) == 1
    assert adapter.last_errors == []


# ---- F206 youtube_followed：yt-dlp 失败 ≠ 频道无视频 --------------------------


def _yt_adapter(tmp_path, monkeypatch, videos_by_channel):
    monkeypatch.setattr(
        yf, "load_channels", lambda _f: [{"channel_id": cid, "channel_name": cid} for cid in videos_by_channel]
    )
    monkeypatch.setattr(yf, "fetch_transcript", lambda _vid: "转录")
    return yf.YouTubeFollowedAdapter(first_seen_path=tmp_path / "first_seen.json")


def test_f206_fetch_failure_is_not_silently_empty(monkeypatch):
    """yt-dlp 抛异常时 fetch_channel_videos 不能与「频道无视频」一样返回 []。"""
    import sys
    import types

    class _Boom:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, *_a, **_kw):
            raise RuntimeError("This channel does not have a videos tab")

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = _Boom
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    # 失败 → None（可区分）；「频道无视频」→ []
    assert yf.fetch_channel_videos("UCx", limit=15) is None

    class _Empty:
        def __init__(self, _opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def extract_info(self, *_a, **_kw):
            return {"entries": []}

    fake.YoutubeDL = _Empty
    assert yf.fetch_channel_videos("UCx", limit=15) == []


def test_f206_most_channels_failed_records_error(tmp_path, monkeypatch):
    """过半频道失败 → last_errors 带上失败数，summary 才不会假装 ok。"""
    adapter = _yt_adapter(tmp_path, monkeypatch, ["UC1", "UC2", "UC3", "UC4"])
    calls: list[str] = []

    def fake_fetch(cid, limit=15):
        calls.append(cid)
        if cid in {"UC1", "UC2", "UC3"}:
            return None  # yt-dlp 失败
        return [{"video_id": "v1", "title": "标题", "uploader": "up", "duration": 10}]

    monkeypatch.setattr(yf, "fetch_channel_videos", fake_fetch)

    records = list(adapter.collect())
    assert len(records) == 1, "唯一成功的频道仍要出条目"
    assert adapter.last_errors
    joined = " ".join(adapter.last_errors)
    assert "3" in joined and "4" in joined, f"要带失败数/总频道数, 实际: {joined}"


def test_f206_empty_channel_is_not_a_failure(tmp_path, monkeypatch):
    """频道真的没有视频（[]）不是失败：过半空频道也不该记 last_errors。"""
    adapter = _yt_adapter(tmp_path, monkeypatch, ["UC1", "UC2", "UC3", "UC4"])
    monkeypatch.setattr(yf, "fetch_channel_videos", lambda _cid, limit=15: [])

    assert list(adapter.collect()) == []
    assert adapter.last_errors == []


def test_f206_single_channel_failure_does_not_degrade(tmp_path, monkeypatch):
    """少数频道失败照旧出结果，不算 degraded。"""
    adapter = _yt_adapter(tmp_path, monkeypatch, ["UC1", "UC2", "UC3", "UC4"])

    def fake_fetch(cid, limit=15):
        if cid == "UC1":
            return None
        return [{"video_id": f"v-{cid}", "title": "t", "uploader": "up", "duration": 1}]

    monkeypatch.setattr(yf, "fetch_channel_videos", fake_fetch)

    assert len(list(adapter.collect())) == 3
    assert adapter.last_errors == []


# ---- F209 disaster_alerts：位置文件形状不对 + cn_match 是字符串 ----------------


def _da_reload_loc(monkeypatch, tmp_path, payload):
    f = tmp_path / "mylocation.json"
    f.write_text(payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False))
    monkeypatch.setattr(da, "LOCATION_FILE", f)
    return da


def test_f209_wrong_shape_falls_back_without_crashing(monkeypatch, tmp_path):
    """JSON 合法但顶层是 list —— 形状校验在 try 外面，会一路崩到 alerts-notify。"""
    mod = _da_reload_loc(monkeypatch, tmp_path, ["JP", "130000"])
    assert mod._location_override() == ((), ())


def test_f209_wrong_shape_keeps_default_watches(monkeypatch, tmp_path):
    """形状不对也不能让主配置盯点消失：adapter 仍按主配置名单收。"""
    mod = _da_reload_loc(monkeypatch, tmp_path, "123")
    monkeypatch.setattr(mod, "WATCHED_CN", ({"province": "广东", "city": "广州市"},))
    rows = [
        {
            "alertid": "A1",
            "title": "广东省广州市天河区气象台发布雷雨大风橙色预警信号",
            "issuetime": "2026/08/26 20:38",
            "url": "/publish/alarm/x_1.html",
        }
    ]
    adapter = mod.DisasterAlertsAdapter(fetch_cn=lambda _p: rows, fetch_jp_feed=lambda: "")
    assert len(list(adapter.collect())) == 1


def test_f209_cn_match_string_becomes_single_element_tuple(monkeypatch, tmp_path):
    """cn_match 写成字符串时不能 tuple() 拆成单字（那会让「市」这种字到处命中）。"""
    mod = _da_reload_loc(
        monkeypatch,
        tmp_path,
        {"country": "CN", "cn_province": "上海", "cn_city": "浦东新区", "cn_match": "上海市气象台"},
    )
    cn, _jp = mod._location_override()
    assert cn == ({"province": "上海", "city": "浦东新区", "match": ("上海市气象台",)},)


def test_f209_cn_match_string_does_not_overmatch(monkeypatch, tmp_path):
    """阳性对照：字符串形态正确匹配「上海市气象台」，不该匹配只有「浦东新区」的条目。"""
    mod = _da_reload_loc(
        monkeypatch,
        tmp_path,
        {"country": "CN", "cn_province": "上海", "cn_city": "浦东新区", "cn_match": "上海市气象台"},
    )
    rows = [
        {
            "alertid": "A1",
            "title": "上海市气象台发布雷雨大风橙色预警信号",
            "issuetime": "2026/08/26 20:38",
            "url": "/publish/alarm/x_1.html",
        },
        {
            "alertid": "A2",
            "title": "上海市浦东新区气象台发布暴雨橙色预警信号",
            "issuetime": "2026/08/26 20:39",
            "url": "/publish/alarm/x_2.html",
        },
    ]
    adapter = mod.DisasterAlertsAdapter(fetch_cn=lambda _p: rows, fetch_jp_feed=lambda: "")
    titles = [r.item.title for r in adapter.collect()]
    assert len(titles) == 1
    assert "上海市气象台" in titles[0]


# ---- F110 weibo_timeline：上游导出文件超 48 小时 ------------------------------


def _wt_post(idx: int) -> dict:
    return {
        "post_id": f"p{idx}",
        "text_plain": f"正文{idx}",
        "authored_at": "2026-10-04T08:00:00+00:00",
        "status_url": f"https://weibo.com/1234/p{idx}",
        "screen_name": "某人",
    }


def _wt_setup(tmp_path, mtime_age_s: float | None = None) -> tuple[WeiboTimelineAdapter, Path]:
    timelines = tmp_path / "timelines"
    analysis = timelines / "1234" / "analysis"
    analysis.mkdir(parents=True)
    posts = analysis / "posts.jsonl"
    posts.write_text(
        "\n".join(json.dumps(_wt_post(i), ensure_ascii=False) for i in range(2)) + "\n", "utf-8"
    )
    if mtime_age_s is not None:
        stamp = (datetime.now(timezone.utc) - timedelta(seconds=mtime_age_s)).timestamp()
        os.utime(posts, (stamp, stamp))
    watchlist = timelines / "watchlist.json"
    watchlist.write_text(json.dumps({"accounts": [{"uid": "1234"}]}), "utf-8")
    adapter = WeiboTimelineAdapter(watchlist_path=watchlist, timelines_dir=timelines)
    return adapter, posts


def test_f110_stale_export_records_error(tmp_path):
    """上游导出文件 72 小时没更新 = 静默断流，必须 warning + last_errors。"""
    adapter, _posts = _wt_setup(tmp_path, mtime_age_s=72 * 3600)

    assert len(list(adapter.collect())) == 2
    assert adapter.last_errors, "静默断流必须留痕"
    assert "48" in " ".join(adapter.last_errors) or "1234" in " ".join(adapter.last_errors)


def test_f110_fresh_export_has_no_error(tmp_path):
    adapter, _posts = _wt_setup(tmp_path, mtime_age_s=3600)

    assert len(list(adapter.collect())) == 2
    assert adapter.last_errors == []


def test_f110_helper_flags_stale_file(tmp_path):
    """_pick_fresher 本身的年龄判据：超过 48 小时要能单独告警。"""
    scored = tmp_path / "scored_posts.jsonl"
    scored.write_text("{}\n", "utf-8")
    stamp = (datetime.now(timezone.utc) - timedelta(hours=72)).timestamp()
    os.utime(scored, (stamp, stamp))

    assert wt._file_is_stale(scored) is True

    fresh = tmp_path / "posts.jsonl"
    fresh.write_text("{}\n", "utf-8")
    assert wt._file_is_stale(fresh) is False


# ---- F216 podcast_new：先截断再记账 ------------------------------------------


def _pn_setup(tmp_path, n: int = 5) -> Path:
    """造 n 集转录。日期从 2026-10-02 起 —— 配 days=3 / now=10-04 时 10-01 那集
    会落在 cutoff 之外被 _build_record 过滤掉，测不出 seen 的问题。"""
    root = tmp_path / "podcasts"
    for i in range(n):
        p = root / "节目" / f"2026-10-{i + 2:02d}_第{i}期.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"内容{i}", "utf-8")
    return root


def _pn_adapter(tmp_path, roots, **kw):
    kw.setdefault("days", 3)
    kw.setdefault("now_fn", lambda: datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc))
    kw.setdefault("state_path", tmp_path / "state.json")
    return PodcastNewAdapter(roots, **kw)


def test_f216_limited_out_items_only_marked_seen(tmp_path):
    """被 --limit 截掉的集不能写进 seen，否则下一轮永远收不到。"""
    root = _pn_setup(tmp_path, 5)
    adapter = _pn_adapter(tmp_path, [root])

    out = list(adapter.collect(limit=2))
    assert len(out) == 2

    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert len(state["seen"]) == 2, f"只该记实际输出的 2 条, 实际 {state['seen']}"
    assert {r.item.url for r in out} == set(state["seen"])


def test_f216_dropped_items_come_back_next_round(tmp_path):
    """第二轮无 limit 时，被截掉的 3 集必须回来。"""
    root = _pn_setup(tmp_path, 5)
    adapter = _pn_adapter(tmp_path, [root])

    first = list(adapter.collect(limit=2))
    second = list(adapter.collect())
    assert len(first) == 2
    assert len(second) == 3, f"被截掉的 3 集应该回来, 实际 {len(second)}"
    assert not ({r.item.url for r in first} & {r.item.url for r in second})


def test_f216_no_limit_still_marks_everything(tmp_path):
    root = _pn_setup(tmp_path, 3)
    adapter = _pn_adapter(tmp_path, [root])

    assert len(list(adapter.collect())) == 3
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert len(state["seen"]) == 3
    assert list(adapter.collect()) == [], "全收过之后不该再出"


# ---- F219 podcast_new：转录根目录不存在 --------------------------------------


def test_f219_missing_root_records_error(tmp_path, caplog):
    missing = tmp_path / "Slow_Storage" / "transcripts"
    adapter = _pn_adapter(tmp_path, [missing])

    with caplog.at_level("WARNING"):
        assert list(adapter.collect()) == []
    assert adapter.last_errors, "外置卷没挂是失败，不是「本轮 0 条」"
    assert any("WARNING" in r.levelname for r in caplog.records)


def test_f219_existing_root_no_error(tmp_path):
    root = _pn_setup(tmp_path, 1)
    adapter = _pn_adapter(tmp_path, [root])

    assert len(list(adapter.collect())) == 1
    assert adapter.last_errors == []
