"""ensure_fulltext / fetch_fulltext: 已处理跳过、失败不抛、可续跑、截断。"""
from __future__ import annotations

import pytest

from personal_intel_loop.fulltext import FULLTEXT_MAX_CHARS, ensure_fulltext, fetch_fulltext
from personal_intel_loop.store import connect_db, ensure_schema, upsert_item
from tests.conftest import make_item


@pytest.fixture
def conn(tmp_path):
    c = connect_db(tmp_path / "db.sqlite")
    ensure_schema(c)
    try:
        yield c
    finally:
        c.close()


def _add_items(conn, item_ids):
    for item_id in item_ids:
        upsert_item(
            conn,
            make_item(item_id=item_id, url=f"https://example.com/{item_id}"),
            adapter_name="rss_briefing",
            source_payload_json="{}",
        )
    conn.commit()


def test_ensure_fulltext_writes_rows_and_fails_soft(conn):
    _add_items(conn, ["a1", "a2", "a3"])

    def fetcher(url):
        if "a2" in url:
            raise RuntimeError("network down")
        return "这是正文。", "ok"

    fetched = ensure_fulltext(conn, ["a1", "a2", "a3"], fetcher=fetcher, sleep_s=0)
    assert fetched == 3
    rows = {row["item_id"]: (row["text"], row["status"]) for row in conn.execute("SELECT item_id, text, status FROM item_fulltext")}
    assert rows["a1"] == ("这是正文。", "ok")
    assert rows["a2"] == (None, "failed")
    assert rows["a3"] == ("这是正文。", "ok")


def test_ensure_fulltext_rerun_skips_existing(conn):
    _add_items(conn, ["a1", "a2"])
    calls = []

    def fetcher(url):
        calls.append(url)
        return "正文", "ok"

    ensure_fulltext(conn, ["a1", "a2"], fetcher=fetcher, sleep_s=0)
    assert len(calls) == 2
    ensure_fulltext(conn, ["a1", "a2"], fetcher=fetcher, sleep_s=0)
    assert len(calls) == 2  # 重跑: 全部已有记录, 一个都不再抓


def test_ensure_fulltext_resume_only_missing(conn):
    """模拟上次中断: a1 已完成并落库, 重跑只补剩下的。"""
    _add_items(conn, ["a1", "a2", "a3"])
    conn.execute(
        "INSERT INTO item_fulltext (item_id, text, status, fetched_at) VALUES ('a1', '旧正文', 'ok', '2026-10-03T00:00:00Z')"
    )
    conn.commit()
    calls = []

    def fetcher(url):
        calls.append(url)
        return "新正文", "ok"

    ensure_fulltext(conn, ["a1", "a2", "a3"], fetcher=fetcher, sleep_s=0)
    assert calls == ["https://example.com/a2", "https://example.com/a3"]
    row = conn.execute("SELECT text FROM item_fulltext WHERE item_id='a1'").fetchone()
    assert row["text"] == "旧正文"  # 已有记录不被覆盖


def test_ensure_fulltext_unknown_item_skipped(conn):
    def fetcher(url):
        raise AssertionError("unknown item 不应触发抓取")

    assert ensure_fulltext(conn, ["ghost"], fetcher=fetcher, sleep_s=0) == 0


def test_fetch_fulltext_truncates_to_limit():
    text, status = fetch_fulltext(
        "https://x.example.com/a",
        http_get=lambda url: "<html>长文</html>",
        extract=lambda html: "字" * (FULLTEXT_MAX_CHARS + 500),
    )
    assert status == "ok"
    assert len(text) == FULLTEXT_MAX_CHARS


def test_fetch_fulltext_extract_empty_is_failed():
    text, status = fetch_fulltext(
        "https://x.example.com/b",
        http_get=lambda url: "<html></html>",
        extract=lambda html: None,
    )
    assert (text, status) == (None, "failed")


def test_fetch_fulltext_http_error_is_failed():
    def boom(url):
        raise RuntimeError("refused")

    text, status = fetch_fulltext("https://x.example.com/c", http_get=boom, extract=lambda html: "x")
    assert (text, status) == (None, "failed")


def test_fetch_fulltext_injectable_happy_path():
    seen = {}

    def http_get(url):
        seen["url"] = url
        return "HTML"

    def extract(html):
        seen["html"] = html
        return "正文内容"

    text, status = fetch_fulltext("https://x.example.com/d", http_get=http_get, extract=extract)
    assert (text, status) == ("正文内容", "ok")
    assert seen == {"url": "https://x.example.com/d", "html": "HTML"}


def test_wallstreetcn_article_uses_api_not_spa_shell():
    """华尔街见闻文章页是 JS 空壳(10-07 36/36 failed): 走公开接口取 content, 段落保留。"""
    import json

    from personal_intel_loop import fulltext

    calls = []

    def http_get(url):
        calls.append(url)
        return json.dumps({"code": 20000, "data": {
            "content": "<p>第一段&amp;要点</p><p>第二段<br/>续行</p>",
            "image": {"uri": "https://wpimg-wscn.awtmt.com/x.png"},
        }})

    url = "https://wallstreetcn.com/articles/3783019"
    text, status = fetch_fulltext(url, http_get=http_get, extract=lambda h: pytest.fail("不该走 trafilatura"))
    assert status == "ok"
    assert text == "第一段&要点\n第二段\n续行"
    assert calls == ["https://api-one-wscn.awtmt.com/apiv1/content/articles/3783019?extract=0"]
    assert fulltext._LAST_IMAGE.pop(url) == "https://wpimg-wscn.awtmt.com/x.png"


def test_wallstreetcn_api_empty_or_bad_json_is_failed():
    for body in ("not json", '{"code": 40400, "data": null}', '{"data": {"content": ""}}'):
        assert fetch_fulltext("https://wallstreetcn.com/articles/1", http_get=lambda u, b=body: b) == (None, "failed")
