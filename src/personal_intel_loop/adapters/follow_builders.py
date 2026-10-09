from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests

from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

DEFAULT_FEED_SOURCES: dict[str, str] = {
    "x": "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main/feed-x.json",
    "blogs": "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main/feed-blogs.json",
    "podcasts": "https://raw.githubusercontent.com/zarazhangrui/follow-builders/main/feed-podcasts.json",
}

TITLE_MAX = 80


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def _slug(value: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return lowered or "source"


# 10-05 验收 G2-NEW1：上游在单集刚发布时给的是频道/播放列表页而不是单集页。
# 这类 url 对同一节目下所有集都相同，`compute_item_id(source, url=url)` 会把它们挤成一个
# 「槽位」，新一集覆盖旧一集（实证 MAD 节目直接观察到标题被整体改写）。
_CHANNEL_URL_RES = (
    re.compile(r"^https?://[^/]+/@[^/?#]+/?$"),          # youtube.com/@Channel
    re.compile(r"^https?://[^/]+/@[^/?#]+/videos/?$"),   # youtube.com/@Channel/videos
    re.compile(r"^https?://[^/]+/playlist\?[^#]*list="),# youtube.com/playlist?list=...
)


def _is_episode_url(url: str) -> bool:
    """url 是不是单集页（而不是频道/播放列表页）。"""
    value = str(url or "").strip()
    if not value:
        return False
    return not any(pattern.match(value) for pattern in _CHANNEL_URL_RES)


def _derive_title(text: str, *, fallback: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    if not cleaned:
        return fallback
    if len(cleaned) <= TITLE_MAX:
        return cleaned
    return cleaned[: TITLE_MAX - 1].rstrip() + "…"


def _is_missing(raw: object) -> bool:
    """raw 时间是否缺失/不可解析 —— 决定 ts 是不是兜底来的（10-05 验收 G203）。"""
    if not raw:
        return True
    value = str(raw).strip()
    if not value:
        return True
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        datetime.fromisoformat(value)
        return False
    except ValueError:
        pass
    for fmt in ("%b %d, %Y", "%Y-%m-%d"):
        try:
            datetime.strptime(value, fmt)
            return False
        except ValueError:
            continue
    return True


def _parse_timestamp(raw: object, *, fallback: datetime) -> datetime:
    if not raw:
        return fallback
    value = str(raw).strip()
    if not value:
        return fallback
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return fallback


def _load_json_source(source: str | Path, *, session: requests.Session, timeout: float) -> dict:
    if isinstance(source, Path):
        return json.loads(source.read_text("utf-8"))
    raw = str(source).strip()
    if raw.startswith(("http://", "https://")):
        response = session.get(raw, timeout=timeout)
        response.raise_for_status()
        return response.json()
    return json.loads(Path(raw).read_text("utf-8"))


class FollowBuildersAdapter:
    name = "follow_builders"

    def __init__(
        self,
        *,
        feed_sources: dict[str, str | Path] | None = None,
        timeout: float = 20.0,
        session: requests.Session | None = None,
    ) -> None:
        self.feed_sources: dict[str, str | Path] = {**DEFAULT_FEED_SOURCES, **(feed_sources or {})}
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "personal-intel-loop/0.1 (+https://github.com/zarazhangrui/follow-builders)")
        # 10-05 验收 C1/G201: 本轮失败原因列表, 每轮 collect 开头清空, 由 cli 读取
        self.last_errors: list[str] = []

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        since_utc = since.astimezone(timezone.utc) if since else None
        self.last_errors = []  # 10-05 验收 C1: last_errors 只反映本轮
        records: dict[str, ItemRecord] = {}

        x_payload = self._load_feed("x")
        if x_payload:
            records.update(self._collect_x(x_payload, since_utc=since_utc))

        blog_payload = self._load_feed("blogs")
        if blog_payload:
            records.update(self._collect_blogs(blog_payload, since_utc=since_utc))

        podcast_payload = self._load_feed("podcasts")
        if podcast_payload:
            records.update(self._collect_podcasts(podcast_payload, since_utc=since_utc))

        ordered = sorted(records.values(), key=lambda record: record.item.ts, reverse=True)
        return ordered[:limit] if limit is not None else ordered

    def _load_feed(self, kind: str) -> dict:
        source = self.feed_sources.get(kind)
        if not source:
            return {}
        try:
            payload = _load_json_source(source, session=self.session, timeout=self.timeout)
        except Exception as exc:
            # 10-05 验收 G201: 取数异常不再吞成静默 0 条——记 warning 并进失败原因,
            # 由 cli 带进运行记录(x 源 24h 滚动窗口, 失败日静默即永久漏抓)。
            reason = f"{kind}: {type(exc).__name__}/{exc}"
            logger.warning("follow_builders: %s fetch failed (%s)", kind, reason)
            self.last_errors.append(reason)
            return {}
        return payload if isinstance(payload, dict) else {}

    def _collect_x(self, payload: dict, *, since_utc: datetime | None) -> dict[str, ItemRecord]:
        generated_at = _parse_timestamp(payload.get("generatedAt"), fallback=datetime.now(timezone.utc))
        lookback_hours = payload.get("lookbackHours")
        stats = payload.get("stats") or {}
        records: dict[str, ItemRecord] = {}

        for builder in payload.get("x") or []:
            if not isinstance(builder, dict):
                continue
            handle = str(builder.get("handle") or "").strip()
            builder_name = str(builder.get("name") or handle or "").strip()
            if not handle and not builder_name:
                continue
            source = f"{self.name}:x:{_slug(handle or builder_name)}"
            bio = str(builder.get("bio") or "").strip()

            for tweet in builder.get("tweets") or []:
                if not isinstance(tweet, dict):
                    continue
                tweet_text = str(tweet.get("text") or "").strip()
                tweet_id = str(tweet.get("id") or "").strip()
                if not tweet_text or not tweet_id:
                    continue
                url = str(tweet.get("url") or "").strip() or f"https://x.com/{handle}/status/{tweet_id}"
                ts = _parse_timestamp(tweet.get("createdAt"), fallback=generated_at)
                if since_utc is not None and ts < since_utc:
                    continue

                item = Item(
                    id=compute_item_id(source=source, url=url),
                    source=source,
                    url=url,
                    title=_derive_title(tweet_text, fallback=f"{handle or builder_name} tweet"),
                    body=tweet_text[:100000],
                    author=builder_name or handle or None,
                    ts=ts,
                    lang="unknown",
                    summary=tweet_text[:2000] or None,
                    tags=["x", _slug(handle or builder_name)],
                )
                source_payload = {
                    "kind": "x",
                    "generated_at": payload.get("generatedAt"),
                    "lookback_hours": lookback_hours,
                    "stats": stats,
                    "builder_name": builder_name,
                    "handle": handle,
                    "bio": bio,
                    "tweet_id": tweet_id,
                    "created_at": tweet.get("createdAt"),
                    # 10-05 验收 G203：兜底来的 ts 不能覆盖库里已有的真实时间。
                    "ts_is_fallback": _is_missing(tweet.get("createdAt")),
                    "likes": tweet.get("likes"),
                    "retweets": tweet.get("retweets"),
                    "replies": tweet.get("replies"),
                    "is_quote": tweet.get("isQuote"),
                    "quoted_tweet_id": tweet.get("quotedTweetId"),
                    "upstream_source": tweet.get("source") or builder.get("source"),
                }
                records[item.id] = ItemRecord(
                    item=item,
                    adapter_name=self.name,
                    source_payload_json=json.dumps(source_payload, ensure_ascii=False, default=str),
                    media_urls=[],
                )
        return records

    def _collect_blogs(self, payload: dict, *, since_utc: datetime | None) -> dict[str, ItemRecord]:
        generated_at = _parse_timestamp(payload.get("generatedAt"), fallback=datetime.now(timezone.utc))
        lookback_hours = payload.get("lookbackHours")
        stats = payload.get("stats") or {}
        records: dict[str, ItemRecord] = {}

        for entry in payload.get("blogs") or []:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").strip()
            url = str(entry.get("url") or "").strip()
            if not title or not url:
                continue
            publication = str(entry.get("name") or entry.get("author") or "blog").strip()
            source = f"{self.name}:blog:{_slug(publication)}"
            content = str(entry.get("content") or "").strip()
            description = str(entry.get("description") or "").strip()
            body = content or description
            ts = _parse_timestamp(entry.get("publishedAt"), fallback=generated_at)
            if since_utc is not None and ts < since_utc:
                continue
            item = Item(
                id=compute_item_id(source=source, url=url),
                source=source,
                url=url,
                title=title,
                body=body[:100000],
                author=str(entry.get("author") or publication).strip() or None,
                ts=ts,
                lang="unknown",
                summary=(description or body[:2000] or title)[:2000] or None,
                tags=["blog", _slug(publication)],
            )
            source_payload = {
                "kind": "blog",
                "generated_at": payload.get("generatedAt"),
                "lookback_hours": lookback_hours,
                "stats": stats,
                "publication": publication,
                "published_at_raw": entry.get("publishedAt"),
                # 10-05 验收 G203：兜底来的 ts 不能覆盖库里已有的真实时间。
                "ts_is_fallback": _is_missing(entry.get("publishedAt")),
                "description": description,
                "content_len": len(content),
                "upstream_source": entry.get("source"),
            }
            records[item.id] = ItemRecord(
                item=item,
                adapter_name=self.name,
                source_payload_json=json.dumps(source_payload, ensure_ascii=False, default=str),
                media_urls=[],
            )
        return records

    def _collect_podcasts(self, payload: dict, *, since_utc: datetime | None) -> dict[str, ItemRecord]:
        generated_at = _parse_timestamp(payload.get("generatedAt"), fallback=datetime.now(timezone.utc))
        lookback_hours = payload.get("lookbackHours")
        stats = payload.get("stats") or {}
        records: dict[str, ItemRecord] = {}

        for entry in payload.get("podcasts") or []:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").strip()
            guid = str(entry.get("guid") or "").strip()
            url = str(entry.get("url") or "").strip() or (f"https://follow-builders.local/podcast/{guid}" if guid else "")
            if not title or not url:
                continue
            show_name = str(entry.get("name") or "podcast").strip()
            source = f"{self.name}:podcast:{_slug(show_name)}"
            transcript = str(entry.get("transcript") or "").strip()
            ts = _parse_timestamp(entry.get("publishedAt"), fallback=generated_at)
            if since_utc is not None and ts < since_utc:
                continue
            body = (transcript or title)[:100000]
            # 10-05 验收 G2-NEW1：url 是频道/播放列表页时身份里拼 guid，避免新一集覆盖旧一集。
            # item.url 保持上游给的原值，只有身份键变化 —— 单集 url 正常的条目身份完全不变
            # （不让存量约 55 条正常单集换 id 重新入库）。
            # 身份后缀走 query 而不是 `#guid`：canonicalize_url 会把 fragment 丢掉。
            identity_url = url
            if guid and not _is_episode_url(url):
                joiner = "&" if "?" in url else "?"
                identity_url = f"{url}{joiner}_ep={guid}"
            item = Item(
                id=compute_item_id(source=source, url=identity_url),
                source=source,
                url=url,
                title=title,
                body=body,
                author=show_name or None,
                ts=ts,
                lang="unknown",
                transcript=transcript[:500000] or None,
                summary=(transcript[:2000] or title)[:2000] or None,
                tags=["podcast", _slug(show_name)],
            )
            source_payload = {
                "kind": "podcast",
                "generated_at": payload.get("generatedAt"),
                "lookback_hours": lookback_hours,
                "stats": stats,
                "show_name": show_name,
                "guid": guid,
                "published_at_raw": entry.get("publishedAt"),
                # 10-05 验收 G203：兜底来的 ts 不能覆盖库里已有的真实时间。
                "ts_is_fallback": _is_missing(entry.get("publishedAt")),
                "transcript_len": len(transcript),
                "upstream_source": entry.get("source"),
            }
            records[item.id] = ItemRecord(
                item=item,
                adapter_name=self.name,
                source_payload_json=json.dumps(source_payload, ensure_ascii=False, default=str),
                media_urls=[],
            )
        return records
