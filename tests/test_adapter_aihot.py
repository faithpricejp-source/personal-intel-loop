"""aihot adapter: 游标翻页 / 字段映射 / 容错。全部离线, fetch 注入。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from personal_intel_loop.adapters.aihot import AihotAdapter


def _item(**over):
    base = {
        "id": "cmt96t9tm0augrolyx2c3vi84",
        "url": "https://www.langchain.com/blog/a",
        "title": "Making Data Ingestion Production Ready",
        "titleZh": "让数据摄取达到生产级就绪",
        "summaryZh": "LangChain 与 Airbyte 的集成方案。",
        "author": None,
        "publishedAt": "2026-08-25T21:12:53.000Z",
        "aiSelected": True,
        "aiSelectedReason": "把调度与嵌入接入 LangChain",
        "finalScore": 61,
        "aiTags": [{"tag": "RAG"}, {"tag": "部署/工程"}],
        "source": {"id": "rss-langchain-blog", "name": "LangChain：Blog（RSS）", "kind": "rss"},
        "duplicateCount": 0,
        "duplicateSources": [],
    }
    base.update(over)
    return base


PAGE1 = {"items": [_item(), _item(id="x2", url="https://claude.ai/b", titleZh="", title="Claude Blog Post",
                         source={"id": "web-claude-blog", "name": "Claude：Blog（网页）", "kind": "web_list"})],
         "hasNext": True, "nextCursor": {"at": 1787160262858, "id": "cur1"}}
PAGE2 = {"items": [_item(id="x3", url="https://hf.co/c", finalScore=80)], "hasNext": False, "nextCursor": None}


def test_two_page_cursor_paging_and_field_mapping():
    seen_cursors = []

    def fake_fetch(cursor):
        seen_cursors.append(cursor)
        return PAGE1 if cursor is None else PAGE2

    recs = list(AihotAdapter(pages=3, fetch=fake_fetch).collect())
    assert len(recs) == 3
    assert seen_cursors == [None, {"at": 1787160262858, "id": "cur1"}]
    sources = {r.item.source for r in recs}
    assert sources == {"aihot:rss-langchain-blog", "aihot:web-claude-blog"}
    lc = next(r for r in recs if r.item.source == "aihot:rss-langchain-blog")
    assert lc.item.title == "让数据摄取达到生产级就绪"        # titleZh 优先
    assert lc.item.tags == ["RAG", "部署/工程"]
    payload = json.loads(lc.source_payload_json)
    assert payload["final_score"] == 61 and payload["ai_selected_reason"].startswith("把调度")
    assert payload["title_original"] == "Making Data Ingestion Production Ready"
    assert payload["source_kind"] == "rss"
    cb = next(r for r in recs if r.item.source == "aihot:web-claude-blog")
    assert cb.item.title == "Claude Blog Post"               # titleZh 空则回落 title


def test_item_without_url_or_ts_is_skipped_not_raised():
    page = {"items": [_item(url=""), _item(id="ok", url="https://ok/1"), _item(publishedAt=None, url="https://x/2"),
                      "not-a-dict"], "hasNext": False}
    recs = list(AihotAdapter(pages=1, fetch=lambda c: page).collect())
    assert [r.item.url for r in recs] == ["https://ok/1"]


def test_page2_failure_keeps_page1():
    def flaky(cursor):
        return PAGE1 if cursor is None else None      # 第二页拿不到

    recs = list(AihotAdapter(pages=3, fetch=flaky).collect())
    assert len(recs) == 2


def test_min_score_and_since_filters():
    page = {"items": [_item(finalScore=50), _item(id="hi", url="https://hi/1", finalScore=80)], "hasNext": False}
    recs = list(AihotAdapter(pages=1, min_score=60, fetch=lambda c: page).collect())
    assert [r.item.url for r in recs] == ["https://hi/1"]
    old = datetime(2026, 8, 26, tzinfo=timezone.utc)
    assert list(AihotAdapter(pages=1, fetch=lambda c: page).collect(since=old)) == []


def test_pages_clamped_to_max():
    assert AihotAdapter(pages=99).pages == 8
    assert AihotAdapter(pages=0).pages == 1
