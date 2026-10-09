from __future__ import annotations

from datetime import datetime, timezone

from personal_intel_loop.adapters.nikkei_cn import (
    NikkeiCnAdapter,
    _extract_body,
    _extract_title,
    _parse_article_refs,
)


HOMEPAGE_SNIPPET = """
<html><body>
<a href="/politicsaeconomy/commodity/62077-2026-04-20-05-00-30.html">铁矿石定价</a>
<a href="https://cn.nikkei.com/industry/tech/61987-2026-04-01-08-49-05.html">科技新闻</a>
<a href="/top/20250403.html">旧栏目页(不是文章)</a>
<a href="/politicsaeconomy/commodity/62077-2026-04-20-05-00-30.html">重复链接</a>
</body></html>
"""

ARTICLE_HTML = """<!DOCTYPE html><html><head>
<title>中国在澳铁矿石定价谈判中话语权变强 | 日经中文网</title>
<meta property="og:title" content="中国在澳铁矿石定价谈判中话语权变强">
<meta property="og:description" content="必和必拓受到中国国有企业减少购买的影响">
<meta property="og:image" content="https://cdn.example.com/hero.jpg">
</head><body>
<div id="article" class="articleBody">
<p>澳大利亚的大型矿业企业在铁矿石定价方面正受到来自中国的压力。必和必拓集团受到影响。</p>
<p>中国媒体报道称，必和必拓和中国政府旗下贸易公司中国矿产资源集团（CMRG）从2025年6月起持续展开谈判。</p>
<p>短段落</p>
</div></div>
</body></html>
"""


def test_parse_article_refs_dedups_and_sorts_recent_first():
    refs = _parse_article_refs(HOMEPAGE_SNIPPET)
    urls = {r.url for r in refs}
    assert "https://cn.nikkei.com/politicsaeconomy/commodity/62077-2026-04-20-05-00-30.html" in urls
    assert "https://cn.nikkei.com/industry/tech/61987-2026-04-01-08-49-05.html" in urls
    # the 旧栏目页 / top/20250403.html should not match
    assert len(urls) == 2
    # timestamps are converted from JST to UTC (JST is UTC+9)
    ref = next(r for r in refs if r.category == "politicsaeconomy")
    assert ref.published == datetime(2026, 4, 19, 20, 0, 30, tzinfo=timezone.utc)


def test_parse_article_refs_drops_links_more_than_24h_in_the_future():
    html = """
    <a href="/politicsaeconomy/politicsasociety/60860-2026-12-29-05-00-05.html">
      美国对台军售交货延迟严重
    </a>
    """
    refs = _parse_article_refs(
        html,
        now_utc=datetime(2026, 7, 24, 0, 0, 0, tzinfo=timezone.utc),
    )
    assert refs == []


def test_extract_title_prefers_og_title():
    assert _extract_title(ARTICLE_HTML) == "中国在澳铁矿石定价谈判中话语权变强"


def test_extract_body_pulls_paragraphs_from_article_container():
    body = _extract_body(ARTICLE_HTML)
    assert "澳大利亚的大型矿业企业" in body
    assert "中国矿产资源集团" in body
    # 短段落 < 20 chars is dropped
    assert "短段落" not in body


def test_collect_end_to_end_with_fakes(monkeypatch):
    """Stub the HTTP layer; verify full collect() pipeline builds an ItemRecord."""

    class _FakeSession:
        def __init__(self):
            self.headers = {}
            self.cookies = None

        def get(self, url, timeout):
            class _R:
                status_code = 200
                text = ARTICLE_HTML

            if url.endswith("/") or "/index.html" in url:
                _R.text = HOMEPAGE_SNIPPET
            return _R()

    adapter = NikkeiCnAdapter(session=_FakeSession(), fetch_delay=0.0)
    records = list(adapter.collect(limit=5))
    assert records, "expected at least one record"
    rec = records[0]
    assert rec.adapter_name == "nikkei_cn"
    assert rec.item.source.startswith("nikkei_cn:")
    assert rec.item.author == "日经中文网"
    assert rec.item.lang == "zh"
    assert rec.item.id.startswith("nikkei:")
    assert rec.item.tags[0] in {"politicsaeconomy", "industry"}
    assert rec.media_urls == ["https://cdn.example.com/hero.jpg"]


def test_since_filter_skips_older_items():
    """Articles older than `since` are dropped."""

    class _FakeSession:
        def __init__(self):
            self.headers = {}
            self.cookies = None

        def get(self, url, timeout):
            class _R:
                status_code = 200
                text = ARTICLE_HTML if url.endswith(".html") and "index.html" not in url else HOMEPAGE_SNIPPET

            return _R()

    adapter = NikkeiCnAdapter(session=_FakeSession(), fetch_delay=0.0)
    future_cutoff = datetime(2030, 1, 1, tzinfo=timezone.utc)
    records = list(adapter.collect(since=future_cutoff))
    assert records == []
