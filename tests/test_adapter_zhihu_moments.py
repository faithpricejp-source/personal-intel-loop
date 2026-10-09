"""zhihu_moments adapter：字段映射 / 翻页停止 / 去重 / 风控 paused 与 6 小时跳过 / 广告跳过 /
同 target 多人赞同合并。"""
from __future__ import annotations

import json
from pathlib import Path

from personal_intel_loop.adapters.zhihu_moments import ZhihuMomentsAdapter, html_to_text

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "platforms"


def _fixture() -> dict:
    return json.loads((FIXTURES / "zhihu_moments.json").read_text("utf-8"))


def _adapter(tmp_path, pages, **kw):
    calls: list[str | None] = []
    box = list(pages)

    def fetch(url: str | None) -> dict:
        calls.append(url)
        if len(box) > 1:
            return box.pop(0)
        return box[0] if box else {"data": [], "paging": {"is_end": True, "next": ""}}

    kw.setdefault("sleep_s", 0)
    return ZhihuMomentsAdapter(fetch=fetch, state_path=tmp_path / "state.json", **kw), calls


def test_fixture_maps_answer_article_pin(tmp_path):
    payload = _fixture()
    adapter, _ = _adapter(tmp_path, [payload], max_pages=1)
    records = list(adapter.collect())

    by_url = {r.item.url: r for r in records}
    # fixture：12 条，1 条 feed_advert，1 条 target=question（不收）→ 10 条
    assert len(records) == 10

    answer = by_url["https://www.zhihu.com/question/78773616/answer/98276427"]
    assert answer.item.source.startswith("zhihu_moments:")
    assert answer.item.source == "zhihu_moments:示例url_token0a56cb"
    assert answer.item.author == "示例name6e620e"
    # 标题取问题标题，不是回答正文
    assert answer.item.title == "示例titled670aa"
    assert answer.item.body.startswith("示例content7e0cdd")
    assert answer.item.ts.isoformat() == "2026-04-01T05:35:42+00:00"  # created_time 秒级

    article = by_url["https://zhuanlan.zhihu.com/p/97257499"]
    assert article.item.title == "示例titlefd40e9"
    assert article.item.ts.year == 2125  # created 是毫秒级，换算后落在未来也不该炸

    pin = by_url["https://www.zhihu.com/pin/5434261"]
    assert pin.item.title == " ".join(pin.item.body.split())[:60]
    # pin 的 content 是 [{content:…}] 列表，取块里的正文；不能把 Python repr 塞进日报
    assert pin.item.body.startswith("示例content2b196e")
    assert "[{" not in pin.item.body


def test_feed_advert_skipped(tmp_path):
    payload = _fixture()
    advert = next(e for e in payload["data"] if e["type"] == "feed_advert")
    adapter, _ = _adapter(tmp_path, [payload], max_pages=1)
    list(adapter.collect())
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    # 广告不进 seen：下一轮它还在页面上也不会被当成「已见过」
    assert advert["id"] not in state["seen_ids"]


def test_question_target_not_collected(tmp_path):
    payload = _fixture()
    q_entry = next(
        e for e in payload["data"] if (e.get("target") or {}).get("type") == "question"
    )
    adapter, _ = _adapter(tmp_path, [payload], max_pages=1)
    records = list(adapter.collect())
    assert q_entry["target"]["id"] not in {r.item.url.rsplit("/", 1)[-1] for r in records}


def test_same_target_endorsed_by_merged(tmp_path):
    payload = _fixture()
    answer = next(e for e in payload["data"] if e["target"]["id"] == "98276427")
    twin = json.loads(json.dumps(answer))
    twin["id"] = "另一个entry_id"
    twin["action_text"] = "李四赞同了回答"
    twin["actors"] = [{"id": "x", "name": "李四"}]
    payload["data"] = [answer, twin]

    adapter, _ = _adapter(tmp_path, [payload], max_pages=1)
    records = list(adapter.collect())
    hits = [r for r in records if r.item.url.endswith("/answer/98276427")]

    assert len(hits) == 1  # 同一target 只收一次
    pl = json.loads(hits[0].source_payload_json)
    assert "示例action_textb62c42" in pl["endorsed_by"]
    assert "李四赞同了回答" in pl["endorsed_by"]
    assert "李四" in pl["endorsed_by"]


def test_pagination_uses_paging_next(tmp_path):
    page1 = {
        "data": [_answer_entry("e1", "a1", 111)],
        "paging": {"is_end": False, "next": "https://example.com/next?p=2"},
    }
    page2 = {
        "data": [_answer_entry("e2", "a2", 222)],
        "paging": {"is_end": True, "next": ""},
    }
    adapter, calls = _adapter(tmp_path, [page1, page2], max_pages=3)
    records = list(adapter.collect())

    assert calls == [None, "https://example.com/next?p=2"]  # 首次 None，之后 paging.next
    assert len(records) == 2


def test_max_pages_caps_pagination(tmp_path):
    pages = [
        {
            "data": [_answer_entry(f"e{i}", f"a{i}", 100 + i)],
            "paging": {"is_end": False, "next": f"https://example.com/next?p={i + 2}"},
        }
        for i in range(10)
    ]
    adapter, calls = _adapter(tmp_path, pages, max_pages=2)
    list(adapter.collect())
    assert len(calls) == 2


def test_seen_ids_stop_pagination(tmp_path):
    page = {
        "data": [_answer_entry("e1", "a1", 111)],
        "paging": {"is_end": False, "next": "https://example.com/next?p=2"},
    }
    adapter, _ = _adapter(tmp_path, [page], max_pages=3)
    assert len(list(adapter.collect())) == 1

    adapter2, calls = _adapter(tmp_path, [page], max_pages=3)
    assert list(adapter2.collect()) == []
    assert len(calls) == 1  # 第一页无新动态 → 立刻停


def test_403_pauses_and_keeps_partial(tmp_path):
    page1 = {"data": [_answer_entry("e1", "a1", 111)], "paging": {"is_end": False, "next": "u2"}}
    page2 = {"code": 40352, "error": {"message": "系统监测到您的请求过于频繁"}}
    adapter, _ = _adapter(tmp_path, [page1, page2], max_pages=3)
    records = list(adapter.collect())

    assert len(records) == 1
    assert adapter.paused_reason and "40352" in adapter.paused_reason
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["paused"] is True
    assert "40352" in state["reason"]


def test_401_exception_pauses(tmp_path):
    calls: list[str | None] = []

    def fetch(url: str | None) -> dict:
        calls.append(url)
        raise RuntimeError("HTTP 401")

    adapter = ZhihuMomentsAdapter(fetch=fetch, state_path=tmp_path / "state.json", sleep_s=0)
    assert list(adapter.collect()) == []
    assert len(calls) == 1  # 不重试
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["paused"] is True
    assert "HTTP 401" in state["reason"]


def test_paused_within_6h_skips_whole_run(tmp_path):
    from datetime import datetime, timedelta, timezone

    state_path = tmp_path / "state.json"
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    state_path.write_text(
        json.dumps({"paused": True, "reason": "风控", "paused_at": recent.isoformat()}), "utf-8"
    )
    adapter, calls = _adapter(tmp_path, [{"data": [_answer_entry("e9", "新", 9)], "paging": {"is_end": True}}])
    adapter.state_path = state_path
    assert list(adapter.collect()) == []
    assert calls == []


def test_paused_older_than_6h_runs_again(tmp_path):
    from datetime import datetime, timedelta, timezone

    state_path = tmp_path / "state.json"
    old = datetime.now(timezone.utc) - timedelta(hours=7)
    state_path.write_text(
        json.dumps({"paused": True, "reason": "风控", "paused_at": old.isoformat()}), "utf-8"
    )
    adapter, calls = _adapter(tmp_path, [{"data": [_answer_entry("e9", "新", 9)], "paging": {"is_end": True}}])
    adapter.state_path = state_path
    assert len(list(adapter.collect())) == 1
    assert len(calls) == 1


def test_since_filters_old(tmp_path):
    from datetime import datetime, timezone

    page = {
        "data": [_answer_entry("e1", "a1", 100)],
        "paging": {"is_end": True, "next": ""},
    }
    adapter, _ = _adapter(tmp_path, [page], max_pages=1)
    # created_time=100 → 1970 年，任何合理 since 都把它滤掉
    assert list(adapter.collect(since=datetime(2020, 1, 1, tzinfo=timezone.utc))) == []


def test_limit_caps_output(tmp_path):
    adapter, _ = _adapter(tmp_path, [_fixture()], max_pages=1)
    assert len(list(adapter.collect(limit=4))) == 4


def test_html_to_text_keeps_paragraphs():
    got = html_to_text("<p>第一段</p><p>第二段<br>换行</p>")
    assert got == "第一段\n\n第二段\n换行"
    assert html_to_text("") == ""
    assert "&amp;" not in html_to_text("<p>a&amp;b</p>")


def _answer_entry(entry_id: str, target_id: str, created: int) -> dict:
    return {
        "id": entry_id,
        "type": "feed",
        "created_time": created,
        "action_text": "示例action_textb62c42",
        "verb": "MEMBER_VOTE_ANSWER",
        "actors": [{"id": "示例id435b19", "name": "示例name9b5ff9"}],
        "target": {
            "id": target_id,
            "type": "answer",
            "created_time": created,
            "content": "<p>示例content。这是虚构正文。</p>",
            "question": {"id": "78773616", "title": "示例title"},
            "author": {"id": "示例id330e85", "name": "示例name6e620e", "url_token": "示例url_token0a56cb"},
            "voteup_count": "514",
            "comment_count": "114",
        },
    }