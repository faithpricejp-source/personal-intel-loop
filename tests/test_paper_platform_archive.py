"""6.2 全部来源: FTS5 检索(标题+正文)、翻页、过滤、来源列表、same_day_url、reindex。"""
from __future__ import annotations

import pytest

from personal_intel_loop.paper_api import get_archive, get_archive_sources, get_item
from personal_intel_loop.store import reindex_fts, upsert_item
from tests.conftest import make_item

NOW = "2026-10-04T01:00:00Z"


def _seed(conn):
    rows = [
        ("rss_briefing:feed_a", "央行加息 25 基点", "正文提到加息与通胀"),
        ("rss_briefing:feed_a", "央行按兵不动", "正文完全没提别的"),
        ("rss_briefing:feed_b", "台风逼近关东", "正文提到大雨与大风"),
        ("weibo_timeline:user1", "微博转发: 央行加息", "加息评论区吵起来了"),
    ]
    for index, (source, title, body) in enumerate(rows):
        upsert_item(
            conn,
            make_item(item_id=f"item:{index:02d}", source=source, url=f"https://example.com/{index}", title=title, body=body, ts=NOW),
            adapter_name=source.split(":", 1)[0],
            source_payload_json='{"feed_name": "测试源"}',
        )
    conn.commit()


def test_fts_search_hits_title_and_body(db_conn):
    _seed(db_conn)
    result = get_archive(db_conn, q="加息")
    titles = [entry["title"] for entry in result["items"]]
    assert "央行加息 25 基点" in titles and "微博转发: 央行加息" in titles, "标题与正文都能命中"
    assert result["next_before"] is None or len(result["items"]) < 50

    body_hit = get_archive(db_conn, q="大雨")["items"]
    assert [entry["title"] for entry in body_hit] == ["台风逼近关东"], "正文词命中"


def test_archive_filters_and_pagination(db_conn):
    _seed(db_conn)
    by_source = get_archive(db_conn, source="rss_briefing:feed_a")["items"]
    assert {entry["source"] for entry in by_source} == {"rss_briefing:feed_a"}

    page1 = get_archive(db_conn, limit=2)
    assert len(page1["items"]) == 2 and page1["next_before"] is not None
    page2 = get_archive(db_conn, limit=2, before=page1["next_before"])
    ids1 = {entry["item_id"] for entry in page1["items"]}
    ids2 = {entry["item_id"] for entry in page2["items"]}
    assert ids1 & ids2 == set(), "游标翻页不重复"
    assert page2["next_before"] is not None
    page3 = get_archive(db_conn, limit=10, before=page2["next_before"])
    assert page3["next_before"] is None, "最后一页 next_before 为 null"

    # date 过滤: 该条本地日期(东京)
    archive = get_archive(db_conn, date_local="2026-10-04")
    assert len(archive["items"]) == 4
    assert get_archive(db_conn, date_local="2026-01-01")["items"] == []


def test_archive_item_shape(db_conn):
    _seed(db_conn)
    entry = get_archive(db_conn, q="台风")["items"][0]
    assert set(entry.keys()) == {
        "item_id", "title", "url", "source", "source_label", "author_label", "published_at", "one_liner", "on_paper",
    }
    assert entry["source_label"] == "测试源"
    assert entry["on_paper"] is None
    # 检索词经 fts_match_query 逐词加引号消毒, 畸形 FTS 语法进不到 SQLite(不 500)
    assert get_archive(db_conn, q='"坏掉 AND (')["items"] == []
    # 消毒后没有任何可检索词 → 400
    with pytest.raises(ValueError):
        get_archive(db_conn, q="???")
    with pytest.raises(ValueError):
        get_archive(db_conn, date_local="not-a-date")


def test_archive_sources_endpoint(db_conn):
    _seed(db_conn)
    result = get_archive_sources(db_conn, now_utc=NOW)
    by_source = {entry["source"]: entry for entry in result["sources"]}
    assert set(by_source.keys()) == {"rss_briefing:feed_a", "rss_briefing:feed_b", "weibo_timeline:user1"}
    assert by_source["rss_briefing:feed_a"]["items_7d"] == 2
    assert by_source["rss_briefing:feed_b"]["items_7d"] == 1
    assert set(by_source["rss_briefing:feed_a"].keys()) == {"source", "source_label", "items_7d", "latest_at"}
    assert by_source["rss_briefing:feed_a"]["latest_at"].startswith("2026-10-04")


def test_same_day_url_in_item_output(db_conn):
    _seed(db_conn)
    item = get_item(db_conn, "item:00")
    assert item["same_day_url"] == "/paper/#/archive?source=rss_briefing%3Afeed_a&date=2026-10-04"


def test_reindex_rebuilds_fts(db_conn):
    _seed(db_conn)
    db_conn.execute("DELETE FROM items_fts")
    db_conn.commit()
    assert get_archive(db_conn, q="加息")["items"] == []
    assert reindex_fts(db_conn) == 4
    assert len(get_archive(db_conn, q="加息")["items"]) == 2
