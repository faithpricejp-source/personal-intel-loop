from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from personal_intel_loop.adapters.rss_briefing import (
    RSSBriefingAdapter,
    _detect_truncation,
    _extract_content_html,
    _strip_html,
    feed_slug,
)


# ---------------------------------------------------------------------------
# _extract_content_html
# ---------------------------------------------------------------------------

def test_extract_content_html_prefers_content_field():
    # feedparser returns entry.content as a list of dicts
    entry = {"content": [{"value": "<p>Full article body</p>"}], "summary": "Short subtitle"}
    result = _extract_content_html(entry)
    assert "Full article body" in result


def test_extract_content_html_falls_back_to_summary():
    entry = {"content": [], "summary": "Just a subtitle"}
    result = _extract_content_html(entry)
    assert result == "Just a subtitle"


def test_extract_content_html_dict_entry():
    entry = {"content": [{"value": "<p>Dict full body</p>"}], "summary": "Dict subtitle"}
    result = _extract_content_html(entry)
    assert "Dict full body" in result


# ---------------------------------------------------------------------------
# _detect_truncation
# ---------------------------------------------------------------------------

FREE_POST_HTML = """
<p>This is a free full post with lots of great content.</p>
<p>Second paragraph with more substance.</p>
<p>Subscribe <a href="https://example.substack.com/subscribe">here</a> to support us.</p>
"""

TRUNCATED_POST_HTML = """
<p>Here is the teaser paragraph that makes you want to subscribe.</p>
<p>The second tantalizing paragraph ends here and then...</p>
      <p>
          <a href="https://example.substack.com/p/my-post">
              Read more
          </a>
      </p>
"""

TRUNCATED_WITH_UTM_HTML = """
<p>Premium insight teaser.</p>
      <p>
          <a href="https://newsletter.doomberg.com/p/backwards-looking?utm_source=substack">
              Read more
          </a>
      </p>
"""


def test_detect_truncation_free_post_not_flagged():
    assert not _detect_truncation(FREE_POST_HTML, "https://example.substack.com/p/free-post")


def test_detect_truncation_paywall_post_flagged():
    assert _detect_truncation(TRUNCATED_POST_HTML, "https://example.substack.com/p/my-post")


def test_detect_truncation_utm_stripped_match():
    assert _detect_truncation(
        TRUNCATED_WITH_UTM_HTML,
        "https://newsletter.doomberg.com/p/backwards-looking",
    )


def test_detect_truncation_unrelated_url_not_flagged():
    # "Read more" links to a different URL — should not trigger
    html = '<p>Intro.</p><p><a href="https://other.com/p/other-post">Read more</a></p>'
    assert not _detect_truncation(html, "https://example.substack.com/p/my-post")


# ---------------------------------------------------------------------------
# RSSBriefingAdapter.collect() integration (feed fetch mocked)
# ---------------------------------------------------------------------------

FEED_XML_FREE = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>A free full article</title>
      <link>https://test.substack.com/p/free</link>
      <guid>https://test.substack.com/p/free</guid>
      <pubDate>Mon, 21 Apr 2026 08:00:00 +0000</pubDate>
      <description>Short subtitle only</description>
      <content:encoded><![CDATA[<p>This is the full free article body with substantial content spanning multiple paragraphs. It does not end with a Read more link pointing back to the canonical URL.</p><p>Second paragraph here.</p>]]></content:encoded>
    </item>
  </channel>
</rss>"""

FEED_XML_TRUNCATED = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>A paywalled article</title>
      <link>https://test.substack.com/p/paid</link>
      <guid>https://test.substack.com/p/paid</guid>
      <pubDate>Mon, 21 Apr 2026 09:00:00 +0000</pubDate>
      <description>Short subtitle only</description>
      <content:encoded><![CDATA[<p>Teaser paragraph to entice you.</p>
      <p>
          <a href="https://test.substack.com/p/paid">
              Read more
          </a>
      </p>]]></content:encoded>
    </item>
  </channel>
</rss>"""


FEED_XML_FUTURE_DATED = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>A future dated article</title>
      <link>https://test.substack.com/p/future</link>
      <guid>https://test.substack.com/p/future</guid>
      <pubDate>Mon, 21 Apr 2099 09:00:00 +0000</pubDate>
      <description>Future dated body</description>
    </item>
  </channel>
</rss>"""


def _make_feeds_file(tmp_path: Path) -> Path:
    feeds = [{"name": "Test Feed", "url": "https://test.substack.com/feed", "category": "tech"}]
    f = tmp_path / "feeds.json"
    f.write_text(json.dumps(feeds))
    return f


def test_collect_free_post_has_body(tmp_path):
    feeds_file = _make_feeds_file(tmp_path)
    adapter = RSSBriefingAdapter(feeds_file=feeds_file)
    adapter.lookback_hours = 9999

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.content = FEED_XML_FREE

    with patch("personal_intel_loop.adapters.rss_briefing.requests.get", return_value=mock_resp):
        records = list(adapter.collect())

    assert len(records) == 1
    payload = json.loads(records[0].source_payload_json)
    assert payload["truncated"] is False
    assert payload["content_len"] > 100
    assert "full free article body" in records[0].item.body


def test_collect_truncated_post_body_is_empty(tmp_path):
    feeds_file = _make_feeds_file(tmp_path)
    adapter = RSSBriefingAdapter(feeds_file=feeds_file)
    adapter.lookback_hours = 9999

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.content = FEED_XML_TRUNCATED

    with patch("personal_intel_loop.adapters.rss_briefing.requests.get", return_value=mock_resp):
        records = list(adapter.collect())

    assert len(records) == 1
    payload = json.loads(records[0].source_payload_json)
    assert payload["truncated"] is True
    assert records[0].item.body == ""


def test_feed_slug_preserves_cjk_and_falls_back_to_domain():
    """2026-08-26: 原实现只留 [a-z0-9], 纯 CJK 名一律回落 'feed',
    端传媒/界面新闻/東洋経済/ダイヤモンド 451 条被混进同一个桶, source_trust 学成一锅粥。"""
    from personal_intel_loop.adapters.rss_briefing import feed_slug

    assert feed_slug("東洋経済オンライン", "https://toyokeizai.net/x") == "東洋経済オンライン"
    assert feed_slug("界面新闻", "https://feedx.net/y") == "界面新闻"
    assert feed_slug("端传媒", "https://theinitium.com/feed") == "端传媒"
    assert feed_slug("WSJ Markets", "https://x") == "wsj_markets"
    # 纯符号名 → URL 域名, 不再撞成 'feed'
    assert feed_slug("···", "https://www.example.com/f") == "example_com"
    assert feed_slug("", "") == "feed"
    # 不同 CJK 源不再互撞
    assert feed_slug("界面新闻", "") != feed_slug("端传媒", "")
