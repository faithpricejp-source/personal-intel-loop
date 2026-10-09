"""10-05 验收 · aihot 搬家(1) + BBC 简体源(2) + BBC 正文容器(3)。样本离线, fixtures/ 为实抓。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from personal_intel_loop.adapters import aihot as aihot_mod
from personal_intel_loop.adapters.bbc_zh import RSS_FEEDS, BBCZhAdapter, _extract_body
from personal_intel_loop.schemas import compute_item_id

FIXTURES = Path(__file__).parent / "fixtures"

AIHOT_ITEMS = json.loads((FIXTURES / "aihot_items.json").read_text(encoding="utf-8"))
BBC_SIMP_RSS = (FIXTURES / "bbc_simp_rss.xml").read_bytes()
BBC_SIMP_ARTICLE = (FIXTURES / "bbc_article_simp.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 编号 1 · aihot

def test_aihot_new_api_field_mapping_and_identity():
    item0 = AIHOT_ITEMS["items"][0]
    last_page = dict(AIHOT_ITEMS, page={"count": 0, "hasMore": False, "nextCursor": None})
    recs = list(aihot_mod.AihotAdapter(pages=1, fetch=lambda c: last_page).collect())
    assert len(recs) == 50
    r0 = next(r for r in recs if r.item.url == item0["links"]["original"])
    assert r0.item.title == item0["title"]                      # 标题用新接口 title
    assert r0.item.summary == item0["summary"]
    assert r0.item.body.startswith(item0["summary"][:20])       # 摘要进 body
    assert r0.item.author == item0["source"]["name"]            # 来源名
    assert r0.item.ts == datetime(2026, 10, 5, 0, 24, 2, tzinfo=timezone.utc)
    # item 身份: 原文链接不变 → id 口径与旧接口一致
    assert r0.item.id == compute_item_id("aihot:whatever", url=item0["links"]["original"])
    assert r0.item.source.startswith("aihot:") and r0.item.source != "aihot:unknown"
    payload = json.loads(r0.source_payload_json)
    assert payload["title_original"] == item0["originalTitle"]  # originalTitle 进 payload
    assert payload["source_name"] == item0["source"]["name"]
    assert payload["ai_selected"] == item0["selected"]
    assert payload["ai_selected_reason"] == item0["reason"]
    assert payload["final_score"] == item0["score"]


def test_aihot_cursor_pagination_uses_page_nextcursor():
    page2 = {"items": [], "page": {"count": 0, "hasMore": False, "nextCursor": None}}
    seen = []

    def fake_fetch(cursor):
        seen.append(cursor)
        return dict(AIHOT_ITEMS) if cursor is None else page2

    recs = list(aihot_mod.AihotAdapter(pages=3, fetch=fake_fetch).collect())
    assert len(recs) == 50
    assert seen[0] is None
    assert seen[1] == AIHOT_ITEMS["page"]["nextCursor"]         # 不透明字符串游标原样传回
    assert len(seen) == 2                                        # hasMore=False 即停


def test_aihot_default_fetch_hits_new_endpoint(monkeypatch):
    calls = []

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"items": [], "page": {"count": 0, "hasMore": False, "nextCursor": None}}

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return Resp()

    monkeypatch.setattr(aihot_mod.requests, "get", fake_get)
    list(aihot_mod.AihotAdapter(pages=1).collect())
    assert calls[0].startswith("https://aihot.news/api/v1/items?")
    assert "mode=selected" in calls[0] and "window=7d" in calls[0]


def test_aihot_non_200_warns_and_sets_last_errors(monkeypatch, caplog):
    class Resp:
        status_code = 404

    monkeypatch.setattr(aihot_mod.requests, "get", lambda url, headers=None, timeout=None: Resp())
    adapter = aihot_mod.AihotAdapter(pages=1)
    with caplog.at_level(logging.WARNING, logger="personal_intel_loop.adapters.aihot"):
        recs = list(adapter.collect())
    assert recs == []
    assert adapter.last_errors and "404" in " ".join(str(e) for e in adapter.last_errors)
    assert any("404" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


# ------------------------------------------------------------------ 编号 2 · BBC 简体

def _mock_resp(content: bytes, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.content = content
    m.text = content.decode("utf-8", errors="replace")
    m.raise_for_status = MagicMock()
    return m


def _collect_bbc(article_html: str, max_articles: int = 2) -> list:
    adapter = BBCZhAdapter(fetch_delay=0, max_articles_per_feed=max_articles)

    def fake_get(url, timeout=15):
        if "feeds.bbci.co.uk" in url:
            return _mock_resp(BBC_SIMP_RSS)
        return _mock_resp(article_html.encode("utf-8"))

    with patch("personal_intel_loop.adapters.bbc_zh.requests.Session") as MockSession:
        sess = MagicMock()
        MockSession.return_value = sess
        sess.get.side_effect = fake_get
        sess.headers = MagicMock()
        sess.headers.update = MagicMock()
        return list(adapter.collect())


def test_bbc_only_one_feed_and_it_is_simp():
    assert len(RSS_FEEDS) == 1
    script, url = RSS_FEEDS[0]
    assert script == "simp" and url == "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml"
    assert all("trad" not in u for _, u in RSS_FEEDS)


def test_bbc_trad_links_rewritten_to_simp_and_title_from_simp_page():
    recs = _collect_bbc(BBC_SIMP_ARTICLE)
    assert len(recs) == 2
    for r in recs:
        assert "/simp" in r.item.url and not r.item.url.endswith("/trad")
    r0 = recs[0]
    assert r0.item.url == "https://www.bbc.com/zhongwen/articles/cw5yn53n4y7ko/simp"
    # 身份 = 简体链接
    assert r0.item.id == compute_item_id("bbc_zh:simp", url=r0.item.url)
    # 标题用简体正文页 <h1>, 不用繁体 feed 标题
    assert r0.item.title == "美国华裔房产经纪涉监视赖清德儿子被捕 控充当中国代理人"
    assert "美國華裔房產經紀" not in r0.item.title


def test_bbc_title_falls_back_to_feed_title_when_page_has_none():
    page = "<html><body><main><p>这段正文足够长超过最小长度过滤器的阈值，用于回退测试。</p></main></body></html>"
    recs = _collect_bbc(page)
    assert recs[0].item.title == "美國華裔房產經紀涉監視賴清德兒子 被FBI拘捕"


# ------------------------------------------------------------------ 编号 3 · BBC 正文

def test_bbc_body_keeps_lead_paragraph_drops_footer_and_clip_list():
    body = _extract_body(BBC_SIMP_ARTICLE)
    assert "美国联邦调查局（FBI）在洛杉矶国际机场逮捕" in body      # 首段进正文
    assert "使用条款" not in body                                   # 页脚固定文案
    assert "节目全长" not in body                                   # 短片列表
    assert "跳过此内容" not in body
