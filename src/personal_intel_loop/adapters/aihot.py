"""AI HOT (aihot.news) adapter — AI 资讯聚合。

带来的是 PIL 其他 adapter 没有的一手技术源: Claude Blog / HuggingFace Daily Papers /
LMSYS / DeepSeek API 更新日志 / Mistral / Meta Engineering / LangChain / OpenRouter 公告 / IT之家。

API 事实(10-05 搬家):  # 10-05 验收 1
  旧接口 https://aihot.virxact.com/api/public/feed 自 09-28 起 404; 站点迁至 https://aihot.news,
  旧域名 2026-10-31 停用。新接口 GET https://aihot.news/api/v1/items —— 默认 mode=selected(精选)、
  window=7d; cursor 为不透明字符串, 原样作为查询参数传回翻页, page.hasMore=False 即停。
  条目字段: title(中文)/originalTitle/summary/source.name/links.original/publishedAt(可为 null,
  回落 discoveredAt)/score/selected/reason/category。
  旧接口同内容条目的原文链接不变, item id 口径(url 派生)保持不变。

Source key: `aihot:{source}` —— 按上游源分, 不塞进一个桶, 好让 source_trust 能区分。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import requests

from personal_intel_loop.schemas import Item, compute_item_id

FEED_URL = "https://aihot.news/api/v1/items"  # 10-05 验收 1
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20
DEFAULT_PAGES = 3      # ~120 条; 8 页即覆盖 7 天全窗
MAX_PAGES = 8
BODY_LIMIT = 20_000

logger = logging.getLogger(__name__)


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def _default_fetch(
    cursor: Any,
    *,
    user_agent: str,
    errors: list[str] | None = None,
) -> dict[str, Any] | None:
    """取一页。网络/解析失败一律返回 None, 由调用方跳过, 不向上抛。"""
    params: dict[str, str] = {"mode": "selected", "window": "7d"}  # 10-05 验收 1
    if cursor:
        params["cursor"] = str(cursor)  # 不透明游标原样传回
    url = FEED_URL + "?" + urllib.parse.urlencode(params)
    try:
        r = requests.get(url, headers={"User-Agent": user_agent}, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("aihot fetch failed: %s", exc)
        if errors is not None:
            errors.append(f"aihot fetch error: {type(exc).__name__}: {exc}")
        return None
    if r.status_code != 200:
        logger.warning("aihot non-200: %s %s", r.status_code, url)  # 10-05 验收 1
        if errors is not None:
            errors.append(f"aihot http {r.status_code}")
        return None
    try:
        return r.json()
    except ValueError as exc:
        logger.warning("aihot non-json body: %s", exc)
        if errors is not None:
            errors.append(f"aihot non-json body: {exc}")
        return None


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


class AihotAdapter:
    name = "aihot"

    def __init__(
        self,
        *,
        pages: int = DEFAULT_PAGES,
        min_score: int = 0,
        user_agent: str = DEFAULT_UA,
        fetch: Callable[..., dict[str, Any] | None] | None = None,
    ) -> None:
        self.pages = max(1, min(pages, MAX_PAGES))
        self.min_score = min_score
        self.user_agent = user_agent
        self.last_errors: list[str] = []  # 10-05 验收 1 · cli 侧读取判断采集降级
        self._fetch = fetch or (
            lambda cursor: _default_fetch(cursor, user_agent=user_agent, errors=self.last_errors)
        )

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        since_utc = since.astimezone(timezone.utc) if since else None
        self.last_errors = []  # 10-05 验收 C1: last_errors 只反映本轮
        records: list[ItemRecord] = []
        skipped = 0
        cursor: dict | None = None

        for page in range(self.pages):
            payload = self._fetch(cursor)
            if not isinstance(payload, dict):
                logger.warning("aihot page %d unavailable, stopping", page + 1)
                break
            items = payload.get("items") or []
            for raw in items:
                if limit is not None and len(records) >= limit:
                    break
                rec = self._to_record(raw, since_utc=since_utc)
                if rec is None:
                    skipped += 1
                    continue
                records.append(rec)
            if limit is not None and len(records) >= limit:
                break
            # 10-05 验收 1 · 新接口分页在 page{hasMore,nextCursor}; 旧接口 hasNext 形态仅兼容既有注入式测试
            page_info = payload.get("page") if isinstance(payload.get("page"), dict) else payload
            if not page_info.get("hasMore") and not page_info.get("hasNext"):
                break
            cursor = page_info.get("nextCursor")
            if not cursor:
                break

        if skipped:
            logger.info("aihot skipped %d items (missing fields / below min_score / older than since)", skipped)
        records.sort(key=lambda r: r.item.ts, reverse=True)
        return records[:limit] if limit is not None else records

    def _to_record(self, raw: Any, *, since_utc: datetime | None) -> ItemRecord | None:
        if not isinstance(raw, dict):
            return None
        if isinstance(raw.get("links"), dict):
            return self._to_record_v1(raw, since_utc=since_utc)  # 10-05 验收 1 · /api/v1/items 形态
        url = str(raw.get("url") or "").strip()
        ts_utc = _parse_ts(raw.get("publishedAt"))
        if not url or ts_utc is None:
            return None
        if since_utc is not None and ts_utc < since_utc:
            return None
        score = raw.get("finalScore")
        if self.min_score and isinstance(score, int) and score < self.min_score:
            return None

        src = raw.get("source") if isinstance(raw.get("source"), dict) else {}
        src_id = str(src.get("id") or "unknown").strip() or "unknown"
        source = f"{self.name}:{src_id}"

        title_zh = str(raw.get("titleZh") or "").strip()
        title = title_zh or str(raw.get("title") or "").strip()
        if not title:
            return None
        summary_zh = str(raw.get("summaryZh") or "").strip()
        tags = [str(t.get("tag")) for t in (raw.get("aiTags") or []) if isinstance(t, dict) and t.get("tag")]

        item = Item(
            id=compute_item_id(source, url=url, guid=str(raw.get("id") or "") or None),
            source=source,
            url=url,
            title=title[:512],
            body=summary_zh[:BODY_LIMIT],
            author=str(raw.get("author") or src.get("name") or "")[:256],
            ts=ts_utc,
            lang="zh" if (title_zh or summary_zh) else "en",
            summary=summary_zh[:2000] or None,
            tags=tags[:16],
        )
        payload = {
            "aihot_id": raw.get("id"),
            "title_original": raw.get("title"),
            "source_name": src.get("name"),
            "source_kind": src.get("kind"),
            "ai_selected": raw.get("aiSelected"),
            "ai_selected_reason": raw.get("aiSelectedReason"),
            "final_score": score,
            "ai_tags": tags,
            "duplicate_count": raw.get("duplicateCount"),
            "duplicate_sources": raw.get("duplicateSources"),
        }
        return ItemRecord(
            item=item,
            adapter_name=self.name,
            source_payload_json=json.dumps(payload, ensure_ascii=False),
            media_urls=[],
        )

    # 10-05 验收 1 · 新接口 GET https://aihot.news/api/v1/items 条目 → Item, 语义对齐旧接口
    def _to_record_v1(self, raw: dict[str, Any], *, since_utc: datetime | None) -> ItemRecord | None:
        links = raw.get("links") or {}
        url = str(links.get("original") or "").strip()
        ts_utc = _parse_ts(raw.get("publishedAt") or raw.get("discoveredAt"))
        if not url or ts_utc is None:
            return None
        if since_utc is not None and ts_utc < since_utc:
            return None
        score = raw.get("score")
        if self.min_score and isinstance(score, (int, float)) and score < self.min_score:
            return None

        src = raw.get("source") if isinstance(raw.get("source"), dict) else {}
        src_name = str(src.get("name") or "").strip()
        source = f"{self.name}:{_slug(src_name) or src_name or 'unknown'}"

        title = str(raw.get("title") or "").strip() or str(raw.get("originalTitle") or "").strip()
        if not title:
            return None
        summary = str(raw.get("summary") or "").strip()

        item = Item(
            id=compute_item_id(source, url=url, guid=str(raw.get("id") or "") or None),
            source=source,
            url=url,
            title=title[:512],
            body=summary[:BODY_LIMIT],
            author=src_name[:256],
            ts=ts_utc,
            lang="zh" if (summary or raw.get("title") != raw.get("originalTitle")) else "en",
            summary=summary[:2000] or None,
            tags=[],
        )
        payload = {
            "aihot_id": raw.get("id"),
            "title_original": raw.get("originalTitle"),
            "source_name": src_name,
            "category": raw.get("category"),
            "ai_selected": raw.get("selected"),
            "ai_selected_reason": raw.get("reason"),
            "final_score": score,
            "attribution": raw.get("attribution"),
        }
        return ItemRecord(
            item=item,
            adapter_name=self.name,
            source_payload_json=json.dumps(payload, ensure_ascii=False),
            media_urls=[],
        )
