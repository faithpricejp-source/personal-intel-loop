"""weibo_home adapter：字段映射 / 翻页停止 / 去重 / 风控 paused 与 6 小时跳过 / 长文回退 / 广告跳过。"""
from __future__ import annotations

import json
from pathlib import Path

from personal_intel_loop.adapters.weibo_home import WeiboHomeAdapter, parse_weibo_ts

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "platforms"


def _fixture() -> dict:
    return json.loads((FIXTURES / "weibo_home_friendstimeline.json").read_text("utf-8"))


def _adapter(tmp_path, pages, **kw):
    """pages: list[dict]，每次 fetch_page 弹一个；不够就复用最后一个。"""
    calls: list[int] = []
    box = list(pages)

    def fetch_page(max_id: int) -> dict:
        calls.append(max_id)
        if len(box) > 1:
            return box.pop(0)
        return box[0] if box else {"ok": 1, "statuses": [], "max_id": 0}

    kw.setdefault("sleep_s", 0)
    kw.setdefault("fetch_long_text", lambda _mblogid: "")
    return WeiboHomeAdapter(fetch_page=fetch_page, state_path=tmp_path / "state.json", **kw), calls


def test_fixture_maps_fields(tmp_path):
    payload = _fixture()
    adapter, _ = _adapter(tmp_path, [payload], max_pages=1)
    records = list(adapter.collect())

    by_url = {r.item.url: r for r in records}
    # fixture 里14 条，其中 1 条广告（isAd=1 且 mblogtype=1）
    assert len(records) == 13

    first = by_url["https://weibo.com/71669736/示例mblogiddf59f5"]
    assert first.item.source == "weibo_home:71669736"
    assert first.item.author == "示例screen_name090e11"
    assert first.item.ts.isoformat() == "2026-10-04T08:06:57+00:00"  # 16:06:57+0800
    assert first.item.body.startswith("示例text_rawa75552")
    assert first.item.title == " ".join(first.item.body.split())[:60]
    # 6 张图 → media_urls，优先 large
    payload_media = json.loads(first.source_payload_json)["media_urls"]
    assert payload_media == first.media_urls
    assert len(payload_media) == 6
    assert "original" not in payload_media[0]
    assert payload_media[0].endswith("d30b15")  # fixture里的 large.url


def test_ads_are_skipped(tmp_path):
    payload = _fixture()
    ad_ids = {s["idstr"] for s in payload["statuses"] if s.get("isAd") or s.get("mblogtype") == 1}
    assert ad_ids == {"87446"}

    adapter, _ = _adapter(tmp_path, [payload], max_pages=1)
    records = list(adapter.collect())
    assert not (ad_ids & {r.item.url.split("/")[-1] for r in records})


def test_retweet_appends_original_and_records_mentioned(tmp_path):
    payload = _fixture()
    adapter, _ = _adapter(tmp_path, [payload], max_pages=1)
    records = {r.item.url: r for r in adapter.collect()}

    rt = records["https://weibo.com/4999513/示例mblogid8c9b3f"]
    # 转发者正文 + 「//转发自 @原作者：原文」
    assert "//转发自 @示例screen_nameff60d9：示例text_raw0687ed。" in rt.item.body
    pl = json.loads(rt.source_payload_json)
    assert pl["is_retweet"] is True
    assert pl["retweeted_by"] == "示例screen_nameff60d9"
    # 被转发者账号进 mentioned_accounts，供推荐关注
    assert pl["mentioned_accounts"] == [
        {"platform": "weibo", "account_id": "3460713", "label": "示例screen_nameff60d9"}
    ]


def test_long_text_fetched_when_is_long_text(tmp_path):
    payload = _fixture()
    calls: list[str] = []

    def long_text(mblogid: str) -> str:
        calls.append(mblogid)
        return "<p>完整的长微博正文</p>"

    adapter, _ = _adapter(tmp_path, [payload], max_pages=1, fetch_long_text=long_text)
    records = {r.item.url: r for r in adapter.collect()}

    long_url = "https://weibo.com/6110139/示例mblogid77e4ff"  # isLongText=True
    assert long_url in records
    assert records[long_url].item.body == "完整的长微博正文"
    assert json.loads(records[long_url].source_payload_json)["long_text_fetched"] is True
    assert "示例mblogid77e4ff" in calls


def test_long_text_failure_falls_back_to_text_raw(tmp_path):
    payload = _fixture()

    def boom(_mblogid: str) -> str:
        raise RuntimeError("longtext 挂了")

    adapter, _ = _adapter(tmp_path, [payload], max_pages=1, fetch_long_text=boom)
    records = {r.item.url: r for r in adapter.collect()}

    long_url = "https://weibo.com/6110139/示例mblogid77e4ff"
    assert records[long_url].item.body == "示例text_raw6f4f47"
    pl = json.loads(records[long_url].source_payload_json)
    assert pl["is_long_text"] is True
    assert pl["long_text_fetched"] is False


def test_pagination_uses_returned_max_id_and_stops(tmp_path):
    page1 = {
        "ok": 1,
        "statuses": [_status("1", "a", minute=1)],
        "max_id": 4623443301452524,
    }
    page2 = {
        "ok": 1,
        "statuses": [_status("2", "b", minute=2)],
        "max_id": 0,  # 到底
    }
    adapter, calls = _adapter(tmp_path, [page1, page2], max_pages=5)
    records = list(adapter.collect())

    assert calls == [0, 4623443301452524]  # 首次0，之后用返回的 max_id
    assert [r.item.title for r in records] == ["b", "a"]  # 按 ts倒序


def test_max_pages_caps_pagination(tmp_path):
    pages = [
        {"ok": 1, "statuses": [_status(str(i), f"t{i}")], "max_id": 100 + i}
        for i in range(10)
    ]
    adapter, calls = _adapter(tmp_path, pages, max_pages=3)
    list(adapter.collect())
    assert len(calls) == 3


def test_seen_ids_stop_pagination(tmp_path):
    payload = _fixture()
    adapter, _ = _adapter(tmp_path, [payload], max_pages=5)
    first = list(adapter.collect())
    assert len(first) == 13

    # 第二轮：状态里已见过全部 id，第一页「无新微博」→ 立刻停，只调一次 fetch
    adapter2, calls = _adapter(tmp_path, [payload], max_pages=5)
    second = list(adapter2.collect())
    assert second == []
    assert len(calls) == 1

    # 第三轮：老 id 全部跳过（去重），只收新来的那条
    payload2 = json.loads(json.dumps(payload))
    payload2["statuses"].append(_status("999999", "新的一条", minute=7))
    payload2["max_id"] = 777
    adapter3, calls3 = _adapter(tmp_path, [payload2, payload2], max_pages=5)
    third = {r.item.title for r in adapter3.collect()}
    assert third == {"新的一条"}
    assert len(calls3) == 2  # 第一页有新东西 → 继续翻；第二页无新东西 → 停


def test_risk_ok_not_1_pauses_and_keeps_partial(tmp_path):
    page1 = {"ok": 1, "statuses": [_status("1", "a")], "max_id": 555}
    page2 = {"ok": 0, "statuses": [], "max_id": 0}
    adapter, _ = _adapter(tmp_path, [page1, page2], max_pages=5)
    records = list(adapter.collect())

    assert [r.item.title for r in records] == ["a"]  # 已拿到的照常产出
    assert adapter.paused_reason and "ok!=1" in adapter.paused_reason
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["paused"] is True
    assert "ok!=1" in state["reason"]
    assert state["seen_ids"] == ["1"]


def test_risk_exception_pauses(tmp_path):
    boom_calls: list[int] = []

    def fetch_page(max_id: int) -> dict:
        boom_calls.append(max_id)
        raise RuntimeError("HTTP 403")

    adapter = WeiboHomeAdapter(
        fetch_page=fetch_page,
        state_path=tmp_path / "state.json",
        sleep_s=0,
        fetch_long_text=lambda _m: "",
    )
    assert list(adapter.collect()) == []
    assert len(boom_calls) == 1  # 立即停，不重试
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["paused"] is True
    assert "HTTP 403" in state["reason"]


def test_paused_within_6h_skips_whole_run(tmp_path):
    from datetime import datetime, timedelta, timezone

    state_path = tmp_path / "state.json"
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    state_path.write_text(
        json.dumps(
            {
                "seen_ids": ["1"],
                "paused": True,
                "reason": "第1页 ok!=1",
                "paused_at": recent.isoformat(),
            }
        ),
        "utf-8",
    )
    adapter, calls = _adapter(tmp_path, [{"ok": 1, "statuses": [_status("9", "新")], "max_id": 1}])
    adapter.state_path = state_path
    assert list(adapter.collect()) == []
    assert calls == []  # 根本没发请求


def test_paused_older_than_6h_runs_again(tmp_path):
    from datetime import datetime, timedelta, timezone

    state_path = tmp_path / "state.json"
    old = datetime.now(timezone.utc) - timedelta(hours=7)
    state_path.write_text(
        json.dumps({"paused": True, "reason": "上次风控", "paused_at": old.isoformat()}),
        "utf-8",
    )
    adapter, calls = _adapter(tmp_path, [{"ok": 1, "statuses": [_status("9", "新")], "max_id": 0}])
    adapter.state_path = state_path
    assert [r.item.title for r in adapter.collect()] == ["新"]
    assert len(calls) == 1


def test_since_filters_old_items(tmp_path):
    from datetime import datetime, timezone

    payload = _fixture()
    adapter, _ = _adapter(tmp_path, [payload], max_pages=1)
    cutoff = datetime(2026, 10, 4, 9, 0, tzinfo=timezone.utc)  # fixture 里最新的ts 是 08:06
    assert list(adapter.collect(since=cutoff)) == []


def test_parse_weibo_ts_rejects_garbage():
    assert parse_weibo_ts("not a date") is None
    assert parse_weibo_ts("") is None
    assert parse_weibo_ts(None) is None


def test_limit_caps_output(tmp_path):
    payload = _fixture()
    adapter, _ = _adapter(tmp_path, [payload], max_pages=1)
    assert len(list(adapter.collect(limit=3))) == 3


def _status(weibo_id: str, text: str, *, minute: int = 6) -> dict:
    return {
        "created_at": f"Sun Oct 04 16:{minute:02d}:57 +0800 2026",
        "idstr": weibo_id,
        "mblogid": f"mb{weibo_id}",
        "isLongText": False,
        "isAd": False,
        "mblogtype": 0,
        "text": text,
        "text_raw": text,
        "user": {"idstr": "71669736", "screen_name": "示例screen_name090e11"},
        "pic_infos": {},
    }