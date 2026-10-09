"""BBC 中文网 adapter.

BBC 中文 RSS feeds deliver only ~80-char subtitle summaries. This adapter
re-fetches each article URL to extract full body paragraphs. No login or
cookies required — articles are publicly accessible.

RSS feed used:  # 10-05 验收 2
  simplified: https://feeds.bbci.co.uk/zhongwen/simp/rss.xml
  注意: 该 feed 实际返回繁体内容(<language>zh-hant</language>, 链接以 /trad 结尾),
  与 trad feed 是同一组文章。故只抓这一个 feed, 把条目链接的 /trad 改写为 /simp
  再抓简体正文页, item 身份用简体链接; 库里旧 trad 条目不做迁移。

Source key: `bbc_zh:simp`
"""
from __future__ import annotations

import html as html_mod
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import feedparser
import requests

from personal_intel_loop.schemas import Item, compute_item_id

# 10-05 验收 2 · 只留 simp 一个 feed, trad 那一路删掉(两 feed 同一组文章)
RSS_FEEDS = [
    ("simp", "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml"),
]

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)
REQUEST_TIMEOUT = 15
FETCH_DELAY = 0.5
MAX_ARTICLES_PER_FEED = 15

# BBC article body lives in <p> tags; filter out nav/footer by requiring minimum length.
MIN_PARA_LEN = 15

logger = logging.getLogger(__name__)


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def _strip_tags(raw: str) -> str:
    no_script = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", no_script)
    text = html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_body(article_html: str) -> str:
    # 10-05 验收 3 (审计 G132) · 只取文章主体容器(<main>, 退化时 <article>)内的段落,
    # 页脚/相关文章/短片列表(均在 main/article 之外)不再混入正文。
    # BBC Next.js pages embed article text in <p> tags inside the rendered HTML.
    m = re.search(r"<main[^>]*>.*?</main>", article_html, re.DOTALL | re.IGNORECASE)
    if m is None:
        m = re.search(r"<article[^>]*>.*?</article>", article_html, re.DOTALL | re.IGNORECASE)
    scope = m.group(0) if m is not None else article_html
    paras = []
    for m in re.finditer(r"<p[^>]*>(.*?)</p>", scope, re.DOTALL):
        text = _strip_tags(m.group(1))
        if len(text) >= MIN_PARA_LEN:
            paras.append(text)
    return "\n\n".join(paras[:80])[:100_000]


def _extract_og_meta(article_html: str, prop: str) -> str | None:
    m = re.search(
        rf'<meta[^>]+property="og:{re.escape(prop)}"[^>]+content="([^"]+)"',
        article_html,
        re.IGNORECASE,
    )
    if m:
        return html_mod.unescape(m.group(1)).strip()
    m = re.search(
        rf'<meta[^>]+content="([^"]+)"[^>]+property="og:{re.escape(prop)}"',
        article_html,
        re.IGNORECASE,
    )
    return html_mod.unescape(m.group(1)).strip() if m else None


def _extract_h1(article_html: str) -> str | None:  # 10-05 验收 2 · 简体页标题
    m = re.search(r"<h1[^>]*>(.*?)</h1>", article_html, re.DOTALL | re.IGNORECASE)
    if m:
        text = _strip_tags(m.group(1))
        return text or None
    return None


class BBCZhAdapter:
    name = "bbc_zh"

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_UA,
        max_articles_per_feed: int = MAX_ARTICLES_PER_FEED,
        fetch_delay: float = FETCH_DELAY,
        rss_feeds: list[tuple[str, str]] | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.max_articles_per_feed = max_articles_per_feed
        self.fetch_delay = fetch_delay
        self.rss_feeds = rss_feeds if rss_feeds is not None else RSS_FEEDS
        self._session: requests.Session | None = None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            s = requests.Session()
            s.headers.update({
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,en;q=0.7",
            })
            self._session = s
        return self._session

    def _get(self, url: str) -> str | None:
        try:
            r = self._get_session().get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            logger.warning("fetch failed %s: %s", url, exc)
            return None
        if r.status_code != 200:
            logger.info("non-200 %s -> %s", url, r.status_code)
            return None
        return r.text

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        since_utc = since.astimezone(timezone.utc) if since else None
        seen_urls: set[str] = set()
        records: list[ItemRecord] = []

        for script, rss_url in self.rss_feeds:
            source = f"{self.name}:{script}"
            try:
                r = self._get_session().get(rss_url, timeout=REQUEST_TIMEOUT)
                r.raise_for_status()
                feed = feedparser.parse(r.content)
            except Exception as exc:
                logger.warning("RSS fetch failed %s: %s", rss_url, exc)
                continue

            count = 0
            for entry in feed.entries:
                if count >= self.max_articles_per_feed:
                    break
                if limit is not None and len(records) >= limit:
                    break

                link = str(entry.get("link") or "").split("?")[0].rstrip("/")
                if link.endswith("/trad"):  # 10-05 验收 2 · feed 给繁体链接, 改写为简体再抓, 身份用简体链接
                    link = link[: -len("/trad")] + "/simp"
                if not link or link in seen_urls:
                    continue
                seen_urls.add(link)

                title = str(entry.get("title") or "").strip()
                if not title:
                    continue

                # Timestamp from RSS
                import calendar
                ts_utc = datetime.now(timezone.utc)
                for attr in ("published_parsed", "updated_parsed"):
                    parsed = getattr(entry, attr, None) or entry.get(attr)
                    if parsed:
                        ts_utc = datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
                        break

                if since_utc is not None and ts_utc < since_utc:
                    continue

                # Fetch article HTML for full body
                article_html = self._get(link)
                time.sleep(self.fetch_delay)
                if article_html is None:
                    continue

                # 10-05 验收 2 · 标题用简体正文页 <h1> 或 og:title, 取不到才退回 feed 标题(繁体)
                title = _extract_h1(article_html) or _extract_og_meta(article_html, "title") or title

                body = _extract_body(article_html)
                og_image = _extract_og_meta(article_html, "image")
                og_desc = _extract_og_meta(article_html, "description")

                guid = str(entry.get("id") or entry.get("guid") or "").strip() or None
                item = Item(
                    id=compute_item_id(source, url=link, guid=guid),
                    source=source,
                    url=link,
                    title=title,
                    body=body,
                    author="BBC 中文",
                    ts=ts_utc,
                    lang="zh",
                    summary=(og_desc or body)[:2000] or None,
                    tags=[script],
                )
                payload = {
                    "script": script,
                    "rss_url": rss_url,
                    "og_image": og_image,
                    "og_desc": og_desc,
                    "body_len": len(body),
                }
                records.append(ItemRecord(
                    item=item,
                    adapter_name=self.name,
                    source_payload_json=json.dumps(payload, ensure_ascii=False),
                    media_urls=[og_image] if og_image else [],
                ))
                count += 1

        records.sort(key=lambda r: r.item.ts, reverse=True)
        return records[:limit] if limit is not None else records
