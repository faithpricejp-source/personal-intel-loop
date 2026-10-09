from __future__ import annotations

import calendar
import html
import json
import logging
import re
from urllib.parse import urlsplit
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import feedparser
import requests

from personal_intel_loop import CONFIG_DIR
from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

DEFAULT_FEEDS_FILE = CONFIG_DIR / "rss_feeds.json"
LOOKBACK_HOURS = 26


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def load_feed_configs(path: Path = DEFAULT_FEEDS_FILE) -> list[dict]:
    return json.loads(path.read_text("utf-8"))


def feed_slug(feed_name: str, url: str = "") -> str:
    """源名 → source key 后缀。

    2026-08-26 修:原实现只保留 [a-z0-9],纯 CJK 名字一律回落成 "feed",于是端传媒/界面新闻/
    東洋経済/ダイヤモンド 等全被塞进同一个 `rss_briefing:feed` 桶(实测 451 条混在一起),
    source_trust 学的是一锅粥。现在保留 CJK 字符;若仍为空(纯符号名)再回落到 URL 域名。
    """
    cleaned = re.sub(r"[^a-z0-9\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]+", "_", feed_name.lower()).strip("_")
    if cleaned:
        return cleaned
    host = re.sub(r"^www\.", "", urlsplit(url).netloc.lower())
    host = re.sub(r"[^a-z0-9]+", "_", host).strip("_")
    return host or "feed"


def _strip_html(raw_html: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", raw_html or "")
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _extract_content_html(entry: object) -> str:
    """Prefer content:encoded (full post HTML) over summary (subtitle only)."""
    content = None
    if isinstance(entry, dict):
        content = entry.get("content")
    else:
        content = getattr(entry, "content", None)
    if content and isinstance(content, list) and content[0]:
        item = content[0]
        value = item.get("value") if isinstance(item, dict) else getattr(item, "value", None)
        if value:
            return str(value).strip()
    # fallback: summary field (subtitle-level, often 30-200 chars for Substack)
    if isinstance(entry, dict):
        return str(entry.get("summary") or "").strip()
    return str(getattr(entry, "summary", "") or "").strip()


# Substack (and some other platforms) truncate paywalled posts in RSS and append
# a "Read more" anchor pointing back to the canonical post URL. This is the most
# reliable truncation signal — it appears on paid posts and not on free full posts.
_READ_MORE_RE = re.compile(
    r'href=["\']([^"\']+)["\'][^>]*>\s*Read more\s*</a>',
    re.IGNORECASE | re.DOTALL,
)


def _detect_truncation(content_html: str, canonical_url: str) -> bool:
    """Return True if content_html is a truncated preview of canonical_url."""
    tail = content_html[-800:]
    for m in _READ_MORE_RE.finditer(tail):
        href = m.group(1).split("?")[0].rstrip("/")
        canon = canonical_url.split("?")[0].rstrip("/")
        if href == canon or canon.endswith(href) or href.endswith(canon.split("/")[-1]):
            return True
    return False


def _extract_guid(entry: object) -> str | None:
    if isinstance(entry, dict):
        return str(entry.get("id") or entry.get("guid") or "").strip() or None
    return str(getattr(entry, "id", "") or getattr(entry, "guid", "")).strip() or None


def _extract_timestamp(entry: object, fallback_now: datetime) -> tuple[datetime, bool]:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc), False
    if isinstance(entry, dict):
        for attr in ("published_parsed", "updated_parsed"):
            parsed = entry.get(attr)
            if parsed:
                return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc), False
    return fallback_now, True


def _extract_media_urls(entry: object) -> list[str]:
    urls: list[str] = []

    def add(candidate: str | None) -> None:
        if candidate and candidate not in urls:
            urls.append(candidate)

    if isinstance(entry, dict):
        media_content = entry.get("media_content", []) or []
        for item in media_content:
            if isinstance(item, dict):
                add(str(item.get("url") or "").strip() or None)
        media_thumbnail = entry.get("media_thumbnail", []) or []
        for item in media_thumbnail:
            if isinstance(item, dict):
                add(str(item.get("url") or "").strip() or None)
        for link in entry.get("links", []) or []:
            if isinstance(link, dict) and str(link.get("rel", "")).lower() == "enclosure":
                add(str(link.get("href") or "").strip() or None)

    for attr in ("media_content", "media_thumbnail", "links", "enclosures"):
        value = getattr(entry, attr, None)
        if not value:
            continue
        for item in value:
            if hasattr(item, "get"):
                add(str(item.get("url") or item.get("href") or "").strip() or None)

    return urls


BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


class RSSBriefingAdapter:
    name = "rss_briefing"
    # 10-05 验收 G137: 发布时间比当前时刻晚超过 1 小时的条目视为上游日程/时钟错误, 直接跳过
    max_future_skew = timedelta(hours=1)

    def __init__(self, feeds_file: Path = DEFAULT_FEEDS_FILE, max_entries_per_feed: int = 20):
        self.feeds_file = feeds_file
        self.lookback_hours = LOOKBACK_HOURS
        self.max_entries_per_feed = max_entries_per_feed
        # 10-05 验收 C1/G134: 本轮失败源清单(「源名: 错误类型/状态码」), 每轮 collect 开头清空
        self.last_errors: list[str] = []

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        fallback_now = datetime.now(timezone.utc)
        self.last_errors = []  # 10-05 验收 C1: last_errors 只反映本轮
        self.skipped_future = 0  # 10-05 验收 G137: 本轮因发布时间在未来而跳过的条数
        since_utc = since.astimezone(timezone.utc) if since else fallback_now - timedelta(hours=self.lookback_hours)
        records: dict[str, ItemRecord] = {}

        for feed in load_feed_configs(self.feeds_file):
            feed_name = str(feed["name"]).strip()
            feed_url = str(feed.get("url") or "")
            source = f"{self.name}:{feed_slug(feed_name, feed_url)}"
            category = str(feed.get("category") or "").strip()
            try:
                # 10-05：带浏览器 UA——Good News Network、Marginal Revolution、BoE 对 python-requests 默认 UA 恒 403
                response = requests.get(feed["url"], timeout=20, headers={"User-Agent": BROWSER_UA})
                response.raise_for_status()
                parsed = feedparser.parse(response.content)
            except Exception as exc:
                # 10-05 验收 G134: 逐源抓取异常不再静默吞掉——记 warning 并进失败源清单,
                # 由 cli 带进运行记录; 部分源失败但有条目时 status 仍为 ok。
                reason = f"{feed_name}: {type(exc).__name__}"
                status = getattr(getattr(exc, "response", None), "status_code", None)
                reason += f"/{status}" if status is not None else f"/{exc}"
                logger.warning("rss_briefing: feed %s fetch failed (%s)", feed_name, reason)
                self.last_errors.append(reason)
                continue

            for entry in parsed.entries[:self.max_entries_per_feed]:
                title = str(entry.get("title") or "").strip()
                link = str(entry.get("link") or "").strip()
                if not title or not link:
                    continue

                ts_utc, ts_is_fallback = _extract_timestamp(entry, fallback_now)
                # 10-05 验收 G137: 发布时间在(1h+)未来的条目是日程/时钟错误——跳过并计数,
                # 不再用「当前时刻」填充(BoC 未来日程曾每轮被刷成当前时刻、反复进摘要)。
                if ts_utc > fallback_now + self.max_future_skew:
                    self.skipped_future += 1
                    continue
                ts_was_future = False  # 旧「未来→当前时刻」改写已移除, 键保留以稳定 payload 形状
                if ts_utc < since_utc:
                    continue

                content_html = _extract_content_html(entry)
                truncated = _detect_truncation(content_html, link)
                body = ("" if truncated else _strip_html(content_html))[:100000]
                guid = _extract_guid(entry)
                media_urls = _extract_media_urls(entry)
                tags = [value for value in [category, feed_slug(feed_name, feed_url)] if value]
                item = Item(
                    id=compute_item_id(source, url=link, guid=guid),
                    source=source,
                    url=link,
                    title=title,
                    body=body,
                    author=feed_name,
                    ts=ts_utc,
                    lang="unknown",
                    summary=body[:2000] or None,
                    tags=tags,
                )
                payload = {
                    "feed_name": feed_name,
                    "feed_url": feed["url"],
                    "category": category,
                    "guid": guid,
                    "content_html": content_html,
                    "content_len": len(content_html),
                    "truncated": truncated,
                    "media_urls": media_urls,
                    "ts_is_fallback": ts_is_fallback,
                    "ts_was_future": ts_was_future,
                }
                records[item.id] = ItemRecord(
                    item=item,
                    adapter_name=self.name,
                    source_payload_json=json.dumps(payload, ensure_ascii=False),
                    media_urls=media_urls,
                )

        if self.skipped_future:
            logger.warning(
                "rss_briefing: skipped %d entries dated more than %dh in the future",
                self.skipped_future,
                max(1, int(self.max_future_skew.total_seconds() // 3600)),
            )
        sorted_records = sorted(records.values(), key=lambda record: record.item.ts, reverse=True)
        return sorted_records[:limit] if limit is not None else sorted_records
