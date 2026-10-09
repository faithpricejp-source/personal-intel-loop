"""weread_mp adapter：字段映射 / 每号只取第1 页 / seen url 去重 / 正文预算 / 空壳页限流 /
单号失败不影响其它号。"""
from __future__ import annotations

import json

from personal_intel_loop.adapters.weread_mp import (
    SHELL_PAGE_MIN_CHARS,
    WereadMpAdapter,
    is_shell_page,
)


def _adapter(tmp_path, accounts, articles, bodies=None, **kw):
    """articles: {bookId: [article,...]}；bodies: {url: str|None}（缺None）。"""
    calls: list[tuple[str, int]] = []
    body_calls: list[str] = []

    def list_accounts() -> list[dict]:
        return accounts

    def list_articles(book_id: str, offset: int) -> list[dict]:
        calls.append((book_id, offset))
        value = articles.get(book_id)
        if isinstance(value, Exception):
            raise value
        return value or []

    def fetch_body(url: str) -> str | None:
        body_calls.append(url)
        value = (bodies or {}).get(url)
        if isinstance(value, Exception):
            raise value
        return value

    kw.setdefault("sleep_s", 0)
    return (
        WereadMpAdapter(
            list_accounts=list_accounts,
            list_articles=list_articles,
            fetch_body=fetch_body,
            state_path=tmp_path / "state.json",
            **kw,
        ),
        calls,
        body_calls,
    )


def _article(title: str, url: str, time: int = 1_788_000_000, digest: str = "") -> dict:
    return {"title": title, "url": url, "time": str(time), "digest": digest}


def test_maps_fields(tmp_path):
    accounts = [{"bookId": "b1", "title": "示例公众号"}]
    articles = {"b1": [_article("标题一", "https://mp.weixin.qq.com/s/a1", digest="摘要一")]}
    adapter, calls, body_calls = _adapter(
        tmp_path, accounts, articles, {"https://mp.weixin.qq.com/s/a1": "正文一"}
    )
    records = list(adapter.collect())

    assert len(records) == 1
    item = records[0].item
    assert item.source == "weread_mp:b1"
    assert item.author == "示例公众号"
    assert item.title == "标题一"
    assert item.body == "正文一"
    assert item.url == "https://mp.weixin.qq.com/s/a1"
    assert item.ts.isoformat() == "2026-08-29T10:40:00+00:00"  # time 秒级 epoch
    assert calls == [("b1", 0)]  # 每个号只取第 1 页
    assert body_calls == ["https://mp.weixin.qq.com/s/a1"]


def test_falls_back_to_digest_when_body_fails(tmp_path):
    articles = {"b1": [_article("标题一", "https://mp.weixin.qq.com/s/u1", digest="只有摘要")]}

    adapter, _c, _b = _adapter(tmp_path, [{"bookId": "b1", "title": "号"}], articles, {"https://mp.weixin.qq.com/s/u1": None})
    records = list(adapter.collect())
    assert records[0].item.body == "只有摘要"
    pl = json.loads(records[0].source_payload_json)
    assert pl["used_digest"] is True
    assert pl["fetched_body"] is False


def test_body_fetch_exception_falls_back_to_digest(tmp_path):
    articles = {"b1": [_article("标题一", "https://mp.weixin.qq.com/s/u1", digest="摘要兜底")]}
    adapter, _c, _b = _adapter(
        tmp_path, [{"bookId": "b1", "title": "号"}], articles, {"https://mp.weixin.qq.com/s/u1": RuntimeError("502")}
    )
    records = list(adapter.collect())
    assert records[0].item.body == "摘要兜底"


def test_shell_page_throttles_and_keeps_partial(tmp_path):
    shell = "<html>" + "window.cgiData=" + ("x" * (SHELL_PAGE_MIN_CHARS + 500)) + "</html>"
    accounts = [
        {"bookId": "b1", "title": "号一"},
        {"bookId": "b2", "title": "号二"},
    ]
    articles = {
        "b1": [_article("一", "https://mp.weixin.qq.com/s/u1", time=1_788_000_001), _article("二", "https://mp.weixin.qq.com/s/u2", time=1_788_000_002)],
        "b2": [_article("三", "https://mp.weixin.qq.com/s/u3", time=1_788_000_003)],
    }
    bodies = {"https://mp.weixin.qq.com/s/u1": "正常正文", "https://mp.weixin.qq.com/s/u2": shell, "https://mp.weixin.qq.com/s/u3": "不该被抓"}
    adapter, calls, body_calls = _adapter(tmp_path, accounts, articles, bodies)

    records = list(adapter.collect())

    assert len(records) == 1  # 已拿到的照常产出
    assert records[0].item.body == "正常正文"
    assert adapter.throttled is True
    assert "https://mp.weixin.qq.com/s/u3" not in body_calls  # 号二整个不再抓
    assert [c[0] for c in calls] == ["b1"]  # 号二连列表都不拉了

    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert state["throttled"] is True
    # 触发限流的那篇没被记进 seen，下轮还能重试
    assert "https://mp.weixin.qq.com/s/u2" not in state["seen_urls"]


def test_is_shell_page():
    assert is_shell_page("<html>window.cgiData=" + "x" * 30_000 + "</html>") is True
    assert is_shell_page("<html>window.cgiData=</html>") is False  # 体积不够
    assert is_shell_page("<html>" + "x" * 30_000 + "</html>") is False  # 没有 cgiData
    assert is_shell_page(None) is False
    assert is_shell_page("") is False
    # 有正文容器就不是空壳
    real = "<html>window.cgiData=" + "x" * 30_000 + '<div id="js_content">正文</div></html>'
    assert is_shell_page(real) is False


def test_seen_urls_skipped_next_round(tmp_path):
    articles = {"b1": [_article("标题一", "https://mp.weixin.qq.com/s/u1")]}
    accounts = [{"bookId": "b1", "title": "号"}]
    bodies = {"https://mp.weixin.qq.com/s/u1": "正文一"}

    adapter, _c, body_calls = _adapter(tmp_path, accounts, articles, bodies)
    assert len(list(adapter.collect())) == 1

    adapter2, _c2, body_calls2 = _adapter(tmp_path, accounts, articles, bodies)
    assert list(adapter2.collect()) == []
    assert body_calls2 == []  # 连正文都没抓


def test_max_articles_budget_leaves_rest_for_next_round(tmp_path):
    articles = {
        "b1": [_article(f"标题{i}", f"https://mp.weixin.qq.com/s/u{i}", time=1_788_000_000 + i) for i in range(5)]
    }
    accounts = [{"bookId": "b1", "title": "号"}]
    bodies = {f"https://mp.weixin.qq.com/s/u{i}": f"正文{i}" for i in range(5)}

    adapter, _c, _b = _adapter(tmp_path, accounts, articles, bodies, max_articles=2)
    first = list(adapter.collect())
    assert len(first) == 2  # 按 ts 倒序，最新的两篇

    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert len(state["seen_urls"]) == 2

    adapter2, _c2, _b2 = _adapter(tmp_path, accounts, articles, bodies, max_articles=2)
    second = list(adapter2.collect())
    assert len(second) == 2  # 剩下 3 篇里再取 2 篇
    assert {r.item.url for r in first} & {r.item.url for r in second} == set()


def test_budget_skipped_urls_not_marked_seen(tmp_path):
    """超预算的 url 不能进 seen，否则下一轮就永远补不回来了。"""
    articles = {
        "b1": [_article(f"标题{i}", f"https://mp.weixin.qq.com/s/u{i}", time=1_788_000_000 + i) for i in range(4)]
    }
    accounts = [{"bookId": "b1", "title": "号"}]
    bodies = {f"https://mp.weixin.qq.com/s/u{i}": f"正文{i}" for i in range(4)}

    adapter, _c, _b = _adapter(tmp_path, accounts, articles, bodies, max_articles=1)
    list(adapter.collect())
    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    assert len(state["seen_urls"]) == 1

    adapter2, _c2, _b2 = _adapter(tmp_path, accounts, articles, bodies, max_articles=10)
    second = list(adapter2.collect())
    assert len(second) == 3


def test_one_account_failure_does_not_affect_others(tmp_path):
    accounts = [{"bookId": "b1", "title": "坏号"}, {"bookId": "b2", "title": "好号"}]
    articles = {
        "b1": RuntimeError("这个号的列表炸了"),
        "b2": [_article("好号的文章", "https://mp.weixin.qq.com/s/u2")],
    }
    bodies = {"https://mp.weixin.qq.com/s/u2": "正文"}
    adapter, calls, _b = _adapter(tmp_path, accounts, articles, bodies)
    records = list(adapter.collect())

    assert len(records) == 1
    assert records[0].item.author == "好号"
    assert [c[0] for c in calls] == ["b1", "b2"]  # 坏号之后继续走


def test_shelf_failure_returns_empty(tmp_path):
    def boom() -> list[dict]:
        raise RuntimeError("书架 403")

    adapter = WereadMpAdapter(
        list_accounts=boom,
        list_articles=lambda _b, _o: [],
        fetch_body=lambda _u: None,
        state_path=tmp_path / "state.json",
        sleep_s=0,
    )
    assert list(adapter.collect()) == []


def test_since_filters_old_articles(tmp_path):
    from datetime import datetime, timezone

    articles = {"b1": [_article("老文章", "https://mp.weixin.qq.com/s/u1", time=1_600_000_000)]}
    adapter, _c, _b = _adapter(tmp_path, [{"bookId": "b1", "title": "号"}], articles, {"https://mp.weixin.qq.com/s/u1": "正文"})
    since = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert list(adapter.collect(since=since)) == []


def test_limit_caps_output(tmp_path):
    articles = {
        "b1": [_article(f"标题{i}", f"https://mp.weixin.qq.com/s/u{i}", time=1_788_000_000 + i) for i in range(5)]
    }
    bodies = {f"https://mp.weixin.qq.com/s/u{i}": f"正文{i}" for i in range(5)}
    adapter, _c, _b = _adapter(
        tmp_path, [{"bookId": "b1", "title": "号"}], articles, bodies, max_articles=10
    )
    assert len(list(adapter.collect(limit=2))) == 2

def test_dirty_url_does_not_break_the_round(tmp_path):
    """列表里混进相对路径 /非 http 的脏数据时，跳过它，其余照常收。"""
    articles = {
        "b1": [
            _article("坏 url", "/s/relative"),
            _article("正常", "https://mp.weixin.qq.com/s/ok"),
        ]
    }
    bodies = {"https://mp.weixin.qq.com/s/ok": "正文"}
    adapter, _c, body_calls = _adapter(tmp_path, [{"bookId": "b1", "title": "号"}], articles, bodies)

    records = list(adapter.collect())
    assert [r.item.title for r in records] == ["正常"]
    assert body_calls == ["https://mp.weixin.qq.com/s/ok"]
