"""日经中文网 (cn.nikkei.com) adapter.

No RSS feed exists. We scrape the homepage + a small set of category landing
pages, both of which list articles whose URLs embed the publish timestamp:

    /{category}/{subcategory}/{id}-{YYYY-MM-DD}-{HH-MM-SS}.html

For each article URL we fetch the HTML (reusing the user's Safari cookies for
.nikkei.com so paywalled/logged-in content also works) and extract the title +
body paragraphs.

trust scope key: `nikkei_cn:{category_slug}` (per-category so, e.g., `industry`
vs `career` can diverge based on feedback).
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests

from personal_intel_loop.schemas import Item, canonicalize_url, compute_item_id, sha1_hex

BASE_URL = "https://cn.nikkei.com"
CATEGORY_PATHS = [
    "/",
    "/industry/index.html",
    "/politicsaeconomy/index.html",
    "/columnviewpoint/index.html",
    "/china/index.html",
    "/trend/index.html",
    "/career/index.html",
]

ARTICLE_URL_RE = re.compile(
    r'href="(?P<href>(?:https?://cn\.nikkei\.com)?/'
    r'(?P<cat>[a-zA-Z]+)/'
    r'(?P<sub>[a-zA-Z0-9_-]+)/'
    r'(?P<id>\d+)-'
    r'(?P<date>\d{4}-\d{2}-\d{2})-'
    r'(?P<time>\d{2}-\d{2}-\d{2})'
    r'\.html)"'
)

TITLE_RE = re.compile(r"<title>\s*(?P<title>[^<]+?)(?:\s*[|｜]\s*[^<]*)?</title>", re.IGNORECASE)
META_OG_TITLE_RE = re.compile(r'<meta[^>]+property="og:title"[^>]*content="(?P<title>[^"]+)"', re.IGNORECASE)
META_OG_DESC_RE = re.compile(r'<meta[^>]+property="og:description"[^>]*content="(?P<desc>[^"]+)"', re.IGNORECASE)
META_OG_IMAGE_RE = re.compile(r'<meta[^>]+property="og:image"[^>]*content="(?P<url>[^"]+)"', re.IGNORECASE)
# 10-05 验收 G115: 实抓页面(未登录)的正文容器是 contentDiv 里的 <div class="newsText fix">，
# 相关新闻/推荐列表也仍在 contentDiv 内，只能取 newsText；旧 ARTICLE_BODY_RE 从未命中。
ARTICLE_CONTAINER_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r'<div[^>]+class="[^"]*newsText[^"]*"[^>]*>',
        r'<div[^>]+id="contentDiv"[^>]*>',
        r'<div[^>]+(?:id="article"|class="[^"]*article[Bb]ody[^"]*")[^>]*>',
    )
)
DIV_TAG_RE = re.compile(r"<(/?)div\b[^>]*>", re.IGNORECASE)
# 10-05 验收 G122: 未登录时正文后紧跟会员注册引导
PAYWALL_MARKER_RE = re.compile(r"敬请登录以便观看全部文章|如果您还不是日经中文网会员")
PARAGRAPH_RE = re.compile(r"<p[^>]*>(?P<text>.*?)</p>", re.DOTALL)
TAG_STRIP_RE = re.compile(r"<[^>]+>")

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)
REQUEST_TIMEOUT = 15
FETCH_DELAY = 0.4
MAX_ARTICLES_PER_RUN = 40

logger = logging.getLogger(__name__)


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


@dataclass(frozen=True)
class _ArticleRef:
    url: str
    category: str
    subcategory: str
    article_id: str
    published: datetime


def _load_safari_cookies():
    """Return a RequestsCookieJar with Safari's .nikkei.com cookies, or None."""
    try:
        import browser_cookie3
    except Exception as exc:
        logger.debug("browser_cookie3 unavailable: %s", exc)
        return None
    try:
        return browser_cookie3.safari(domain_name="nikkei.com")
    except Exception as exc:
        logger.warning("safari cookie load failed: %s", exc)
        return None


def _parse_article_refs(
    html: str,
    *,
    now_utc: datetime | None = None,
) -> list[_ArticleRef]:
    observed_at = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    future_cutoff = observed_at + timedelta(hours=24)
    refs: dict[str, _ArticleRef] = {}
    for match in ARTICLE_URL_RE.finditer(html):
        href = match.group("href")
        if href.startswith("/"):
            href = BASE_URL + href
        try:
            published = datetime.strptime(
                f"{match.group('date')} {match.group('time').replace('-', ':')}",
                "%Y-%m-%d %H:%M:%S",
            )
        except ValueError:
            continue
        # Nikkei CN local timestamps are JST; normalize to UTC.
        published_utc = (published - timedelta(hours=9)).replace(tzinfo=timezone.utc)
        if published_utc > future_cutoff:
            logger.warning(
                "skip future-dated Nikkei URL: published=%s observed_at=%s url=%s",
                published_utc.isoformat(),
                observed_at.isoformat(),
                href,
            )
            continue
        ref = _ArticleRef(
            url=href,
            category=match.group("cat"),
            subcategory=match.group("sub"),
            article_id=match.group("id"),
            published=published_utc,
        )
        refs.setdefault(ref.url, ref)
    return list(refs.values())


def _strip_tags(raw_html: str) -> str:
    no_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
    text = TAG_STRIP_RE.sub(" ", no_scripts)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_title(html: str) -> str:
    og = META_OG_TITLE_RE.search(html)
    if og:
        return og.group("title").strip()
    match = TITLE_RE.search(html)
    if not match:
        return ""
    title = match.group("title").strip()
    # Nikkei tails " 日经中文网" — already stripped by our non-greedy regex, be defensive
    for suffix in (" 日经中文网", " 日經中文網", " NIKKEI"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].rstrip()
    return title


def _find_article_container(html: str):
    for pattern in ARTICLE_CONTAINER_PATTERNS:
        opener = pattern.search(html)
        if opener is not None:
            return opener
    return None


def _container_block(html: str, opener) -> str:
    # 10-05 验收 G115(含 G116 机制): 按 div 嵌套深度找容器闭合，避免非贪婪提前截断。
    depth = 1  # opener 本身已算一层（10-05 复审 R01：原从 0 起算，遇嵌套 div 提前截断或越过容器）
    for tag in DIV_TAG_RE.finditer(html, opener.end()):
        if tag.group(1):
            depth -= 1
            if depth == 0:
                return html[opener.end():tag.start()]
        else:
            depth += 1
    return html[opener.end():]


def _extract_body_parts(html: str) -> tuple[str, bool, bool]:
    """Return (body, used_full_page_fallback, paywall_truncated)."""
    candidates: list[str] = []
    opener = _find_article_container(html)
    if opener is not None:
        block = _container_block(html, opener)
        paywall = PAYWALL_MARKER_RE.search(block)
        paywalled = paywall is not None
        if paywalled:
            cut = block.rfind("<div", 0, paywall.start())
            block = block[:cut if cut != -1 else paywall.start()]
            logger.info(
                "nikkei_cn article body truncated by paywall (paywalled); "
                "registration box stripped from extracted body"
            )
        for pm in PARAGRAPH_RE.finditer(block):
            text = _strip_tags(pm.group("text"))
            if len(text) >= 20:
                candidates.append(text)
        if not candidates:
            return _fallback_body(html), True, paywalled
        return "\n\n".join(candidates[:60])[:100000], False, paywalled
    return _fallback_body(html), True, False


def _fallback_body(html: str) -> str:
    # 10-05 验收 G115: 兜底路径命中时记 warning，调用方在 payload 标 body_fallback。
    logger.warning(
        "nikkei_cn article body container not matched; falling back to whole-page <p> extraction"
    )
    candidates = []
    for pm in PARAGRAPH_RE.finditer(html):
        text = _strip_tags(pm.group("text"))
        if len(text) >= 30:
            candidates.append(text)
    return "\n\n".join(candidates[:60])[:100000]


def _extract_body(html: str) -> str:
    return _extract_body_parts(html)[0]


def _extract_meta_description(html: str) -> str | None:
    m = META_OG_DESC_RE.search(html)
    return m.group("desc").strip() if m else None


def _extract_hero_image(html: str) -> str | None:
    m = META_OG_IMAGE_RE.search(html)
    return m.group("url").strip() if m else None


class NikkeiCnAdapter:
    name = "nikkei_cn"

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        user_agent: str = DEFAULT_UA,
        max_articles: int = MAX_ARTICLES_PER_RUN,
        fetch_delay: float = FETCH_DELAY,
    ) -> None:
        self.user_agent = user_agent
        self.max_articles = max_articles
        self.fetch_delay = fetch_delay
        self._session = session
        self._session_owned = session is None

    def _build_session(self) -> requests.Session:
        if self._session is not None:
            return self._session
        session = requests.Session()
        cookies = _load_safari_cookies()
        if cookies is not None:
            session.cookies = cookies
        session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        self._session = session
        return session

    def _get(self, url: str) -> str | None:
        session = self._build_session()
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("fetch failed %s: %s", url, exc)
            return None
        if response.status_code != 200:
            logger.info("non-200 %s -> %s", url, response.status_code)
            return None
        return response.text

    def _discover_refs(self) -> list[_ArticleRef]:
        seen: dict[str, _ArticleRef] = {}
        for path in CATEGORY_PATHS:
            html = self._get(BASE_URL + path if path != "/" else BASE_URL + "/")
            if html is None:
                continue
            for ref in _parse_article_refs(html):
                seen.setdefault(ref.url, ref)
        return sorted(seen.values(), key=lambda r: r.published, reverse=True)

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        refs = self._discover_refs()
        since_utc = since.astimezone(timezone.utc) if since else None
        max_items = min(limit, self.max_articles) if limit else self.max_articles

        records: list[ItemRecord] = []
        for ref in refs:
            if len(records) >= max_items:
                break
            if since_utc is not None and ref.published < since_utc:
                continue
            record = self._build_record(ref)
            if record is not None:
                records.append(record)
            time.sleep(self.fetch_delay)
        return records

    def _build_record(self, ref: _ArticleRef) -> ItemRecord | None:
        html = self._get(ref.url)
        if html is None:
            return None
        title = _extract_title(html)
        body, body_fallback, paywalled = _extract_body_parts(html)
        if not title or len(body) < 60:
            return None
        hero_image = _extract_hero_image(html)
        description = _extract_meta_description(html)
        summary_text = (description or body)[:2000]

        source = f"{self.name}:{ref.category}"
        url = canonicalize_url(ref.url)
        item_id = f"nikkei:{sha1_hex(url)}"

        item = Item(
            id=item_id,
            source=source,
            url=url,
            title=title,
            body=body,
            author="日经中文网",
            ts=ref.published,
            lang="zh",
            summary=summary_text or None,
            tags=[ref.category, ref.subcategory],
        )
        payload = {
            "category": ref.category,
            "subcategory": ref.subcategory,
            "article_id": ref.article_id,
            "hero_image": hero_image,
            "description": description,
            "published_local_path": ref.url,
            # 10-05 验收 G115 / G122: 正文容器退化与付费墙截断都要留痕
            "body_fallback": body_fallback,
            "paywalled": paywalled,
        }
        return ItemRecord(
            item=item,
            adapter_name=self.name,
            source_payload_json=json.dumps(payload, ensure_ascii=False),
            media_urls=[hero_image] if hero_image else [],
        )
