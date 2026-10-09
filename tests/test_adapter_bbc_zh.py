from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from personal_intel_loop.adapters.bbc_zh import (
    BBCZhAdapter,
    _extract_body,
    _extract_og_meta,
    _strip_tags,
)


ARTICLE_HTML = """<!DOCTYPE html><html>
<head>
<meta property="og:title" content="台湾移工政策争议" />
<meta property="og:description" content="这场争议揭示了台湾面对的三重困境" />
<meta property="og:image" content="https://ichef.bbci.co.uk/images/ic/hero.jpg" />
</head>
<body>
<nav><p>导航</p></nav>
<article>
<p>这是第一段文章正文，足够长以通过最小长度过滤器，包含实质内容。</p>
<p>这是第二段，同样足够长，讲述台湾劳工政策的背景和现状，内容非常详细。</p>
<p>第三段继续分析政策争议的核心矛盾，包括产业缺工和社会偏见两个维度。</p>
</article>
<footer><p>BBC 版权所有</p></footer>
</body>
</html>"""

RSS_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>BBC Chinese</title>
<item>
  <title>Article One</title>
  <link>https://www.bbc.com/zhongwen/articles/abc123/simp</link>
  <guid>https://www.bbc.com/zhongwen/articles/abc123/simp</guid>
  <pubDate>Mon, 21 Apr 2026 08:00:00 +0000</pubDate>
  <description>Article one summary</description>
</item>
<item>
  <title>Article Two</title>
  <link>https://www.bbc.com/zhongwen/articles/def456/simp</link>
  <guid>https://www.bbc.com/zhongwen/articles/def456/simp</guid>
  <pubDate>Mon, 21 Apr 2026 09:00:00 +0000</pubDate>
  <description>Article two summary</description>
</item>
</channel>
</rss>""".encode("utf-8")


def test_extract_body_pulls_paragraphs():
    body = _extract_body(ARTICLE_HTML)
    assert "第一段文章正文" in body
    assert "第二段" in body
    assert "第三段" in body


def test_extract_body_filters_short_paras():
    html = "<p>短</p><p>这是足够长的段落，超过最小长度要求，包含实质内容。</p>"
    body = _extract_body(html)
    assert "短" not in body
    assert "实质内容" in body


def test_extract_og_meta_title():
    result = _extract_og_meta(ARTICLE_HTML, "title")
    assert result == "台湾移工政策争议"


def test_extract_og_meta_image():
    result = _extract_og_meta(ARTICLE_HTML, "image")
    assert result == "https://ichef.bbci.co.uk/images/ic/hero.jpg"


def test_extract_og_meta_missing():
    assert _extract_og_meta("<html></html>", "title") is None


def _make_mock_response(content: bytes, status: int = 200) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status
    mock.content = content
    mock.text = content.decode("utf-8", errors="replace")
    mock.raise_for_status = MagicMock()
    return mock


def test_collect_fetches_article_body():
    rss_resp = _make_mock_response(RSS_FEED_XML)
    article_resp = _make_mock_response(ARTICLE_HTML.encode())

    adapter = BBCZhAdapter(
        rss_feeds=[("simp", "https://fake.bbc.co.uk/simp.xml")],
        fetch_delay=0,
        max_articles_per_feed=5,
    )

    call_count = [0]

    def fake_get(url, timeout=15):
        call_count[0] += 1
        if "fake.bbc.co.uk" in url:
            return rss_resp
        return article_resp

    with patch.object(adapter._get_session(), "get", side_effect=fake_get):
        # rebuild session so patch takes effect
        adapter._session = None
        with patch("personal_intel_loop.adapters.bbc_zh.requests.Session") as MockSession:
            mock_session = MagicMock()
            MockSession.return_value = mock_session
            mock_session.get.side_effect = fake_get
            mock_session.headers = MagicMock()
            mock_session.headers.update = MagicMock()

            records = list(adapter.collect())

    assert len(records) == 2
    r0 = records[0]
    assert r0.item.source == "bbc_zh:simp"
    assert "第一段文章正文" in r0.item.body
    assert r0.item.lang == "zh"
    payload = json.loads(r0.source_payload_json)
    assert payload["script"] == "simp"
    assert payload["body_len"] > 50


def test_collect_skips_on_404():
    rss_resp = _make_mock_response(RSS_FEED_XML)

    adapter = BBCZhAdapter(
        rss_feeds=[("simp", "https://fake.bbc.co.uk/simp.xml")],
        fetch_delay=0,
    )

    def fake_get(url, timeout=15):
        if "fake.bbc.co.uk" in url:
            return rss_resp
        m = MagicMock()
        m.status_code = 404
        m.content = b""
        m.text = ""
        m.raise_for_status = MagicMock()
        return m

    with patch("personal_intel_loop.adapters.bbc_zh.requests.Session") as MockSession:
        mock_session = MagicMock()
        MockSession.return_value = mock_session
        mock_session.get.side_effect = fake_get
        mock_session.headers = MagicMock()
        mock_session.headers.update = MagicMock()

        records = list(adapter.collect())

    assert records == []
