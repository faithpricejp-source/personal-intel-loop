from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import sys
from pathlib import Path
from typing import Iterable, Iterator

from personal_intel_loop import APP_HOME
from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

#: 上游微博时间线导出目录(每个账号一个 JSON + watchlist.json), 由你自己的抓取工具生成。
#: 环境变量 PIL_WEIBO_TIMELINES_DIR 覆盖。
UPSTREAM_TIMELINES_DIR = Path(
    os.environ.get("PIL_WEIBO_TIMELINES_DIR") or (APP_HOME / "weibo_timelines")
).expanduser()
UPSTREAM_WATCHLIST = UPSTREAM_TIMELINES_DIR / "watchlist.json"

TITLE_MAX = 80
PASSTHROUGH_SCORE_FIELDS = (
    "priority_score",
    "useful_score",
    "judgement_score",
    "emotion_score",
    "noise_penalty",
    "engagement_score",
    "engagement_boost",
    "feedback_boost",
    "recency_boost",
    "reading_cost",
)
PASSTHROUGH_GROUP_FIELDS = (
    "matched_useful_groups",
    "matched_useful_keywords",
    "matched_judgement_groups",
    "matched_judgement_keywords",
    "matched_emotion_groups",
    "matched_emotion_keywords",
    "matched_noise_groups",
    "matched_noise_keywords",
    "reason_tags",
)


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


@dataclass
class _SkipStat:
    missing_text: int = 0
    truncated_long_text: int = 0
    missing_timestamp: int = 0
    missing_url: int = 0
    bad_record: int = 0


def _load_watchlist_uids(watchlist_path: Path) -> list[str]:
    if not watchlist_path.exists():
        return []
    data = json.loads(watchlist_path.read_text("utf-8"))
    uids: list[str] = []
    for account in data.get("accounts", []):
        uid = str(account.get("uid") or "").strip()
        if uid and uid not in uids:
            uids.append(uid)
    return uids


# 上游打分脚本若停跑, scored_posts.jsonl 会一直存在但内容陈旧, 且全程无报错
# (任务 exit 0、日志无错), 会连续几个月喂旧内容。所以判据必须是"哪个更新", 不是"哪个存在"。
_STALE_TOLERANCE_S = 3600

# 10-05 验收 F110: 绝对新鲜度门。只比两个文件的相对新旧不够 —— 上游整个停跑时
# 两个文件一起变旧，_pick_fresher 照样选"较新"的那个，lane 新增为 0 而 summary 仍 ok
# (静默断流)。超过 48 小时就当上游没在跑。
_MAX_FILE_AGE_S = 48 * 3600


def _file_is_stale(path: Path, *, max_age_s: float = _MAX_FILE_AGE_S) -> bool:
    """文件 mtime 距今超过 max_age_s → 上游大概率停跑了。"""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return (datetime.now(timezone.utc).timestamp() - mtime) > max_age_s


def _uid_analysis_path(timelines_dir: Path, uid: str) -> Path | None:
    """该 uid 实际要读的 analysis 文件（挑过 fresher）。没有可用文件返回 None。"""
    uid_dir = timelines_dir / uid
    if not uid_dir.is_dir():
        return None
    scored = uid_dir / "analysis" / "scored_posts.jsonl"
    plain = uid_dir / "analysis" / "posts.jsonl"
    path = _pick_fresher(scored, plain, uid)
    return path if path is not None and path.exists() else None


def _pick_fresher(scored: Path, plain: Path, uid: str) -> Path | None:
    if not scored.exists():
        return plain if plain.exists() else None
    if not plain.exists():
        return scored
    if scored.stat().st_mtime < plain.stat().st_mtime - _STALE_TOLERANCE_S:
        print(
            f"[weibo_timeline] WARN uid={uid}: scored_posts.jsonl 比 posts.jsonl 旧 "
            f"({int((plain.stat().st_mtime - scored.stat().st_mtime) / 86400)} 天), "
            f"上游打分未跟上 — 本轮回落 posts.jsonl(丢失评分, 但内容是新的)",
            file=sys.stderr,
        )
        return plain
    return scored


def _iter_uid_posts(timelines_dir: Path, uid: str) -> Iterator[dict]:
    path = _uid_analysis_path(timelines_dir, uid)
    if path is None:
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _derive_title(text_plain: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text_plain or "").strip())
    if not cleaned:
        return ""
    if len(cleaned) <= TITLE_MAX:
        return cleaned
    return cleaned[: TITLE_MAX - 1].rstrip() + "…"


def _coerce_ts(raw: object) -> datetime | None:
    if not raw:
        return None
    value = str(raw).strip()
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return None


class WeiboTimelineAdapter:
    name = "weibo_timeline"

    def __init__(
        self,
        *,
        watchlist_path: Path = UPSTREAM_WATCHLIST,
        timelines_dir: Path = UPSTREAM_TIMELINES_DIR,
        require_hydrated: bool = True,
    ) -> None:
        self.watchlist_path = watchlist_path
        self.timelines_dir = timelines_dir
        self.require_hydrated = require_hydrated
        self.last_skip_stat = _SkipStat()
        #: 10-05 验收 F110: 本轮的失败原因, 供 cli 读（项目里没有"失败原因"约定）
        self.last_errors: list[str] = []

    def _uids(self, explicit_uids: list[str] | None) -> list[str]:
        if explicit_uids:
            return [str(uid).strip() for uid in explicit_uids if str(uid).strip()]
        uids = _load_watchlist_uids(self.watchlist_path)
        if uids:
            return uids
        if self.timelines_dir.is_dir():
            return sorted(
                child.name
                for child in self.timelines_dir.iterdir()
                if child.is_dir() and child.name.isdigit()
            )
        return []

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
        uids: list[str] | None = None,
    ) -> Iterable[ItemRecord]:
        stat = _SkipStat()
        since_utc = since.astimezone(timezone.utc) if since else None
        records: dict[str, ItemRecord] = {}
        self.last_errors = []

        for uid in self._uids(uids):
            source = f"{self.name}:{uid}"
            # 10-05 验收 F110: 上游导出文件超过 48 小时没更新 = 上游停跑, 静默断流。
            # 内容照收(旧帖子已在库里, upsert 只算 updated), 但必须留痕。
            path = _uid_analysis_path(self.timelines_dir, uid)
            if path is not None and _file_is_stale(path):
                reason = (
                    f"uid={uid} 上游导出 {path.name} 已超过 "
                    f"{_MAX_FILE_AGE_S // 3600} 小时未更新(疑似上游停跑)"
                )
                logger.warning("weibo_timeline: %s", reason)
                self.last_errors.append(reason)
            for post in _iter_uid_posts(self.timelines_dir, uid):
                try:
                    record = self._build_record(post, uid=uid, source=source, stat=stat, since_utc=since_utc)
                except Exception:
                    stat.bad_record += 1
                    continue
                if record is None:
                    continue
                records[record.item.id] = record

        sorted_records = sorted(records.values(), key=lambda record: record.item.ts, reverse=True)
        self.last_skip_stat = stat
        return sorted_records[:limit] if limit is not None else sorted_records

    def _build_record(
        self,
        post: dict,
        *,
        uid: str,
        source: str,
        stat: _SkipStat,
        since_utc: datetime | None,
    ) -> ItemRecord | None:
        post_id = str(post.get("post_id") or post.get("id") or post.get("mid") or "").strip()
        if not post_id:
            stat.bad_record += 1
            return None

        text_plain = str(post.get("text_plain") or "").strip()
        if not text_plain:
            stat.missing_text += 1
            return None

        is_long_text = bool(post.get("is_long_text"))
        text_source = str(post.get("text_source") or "").strip()
        if self.require_hydrated and is_long_text and text_source == "text_html":
            stat.truncated_long_text += 1
            return None

        ts = _coerce_ts(post.get("authored_at") or post.get("created_at_iso"))
        if ts is None:
            stat.missing_timestamp += 1
            return None
        if since_utc is not None and ts < since_utc:
            return None

        url = str(post.get("status_url") or "").strip()
        if not url:
            stat.missing_url += 1
            return None

        title = _derive_title(text_plain)
        if not title:
            stat.missing_text += 1
            return None

        media_urls = [str(value).strip() for value in (post.get("pic_urls") or []) if str(value).strip()]
        author = str(post.get("screen_name") or "").strip() or None

        payload: dict[str, object] = {
            "uid": uid,
            "post_id": post_id,
            "screen_name": author,
            "is_long_text": is_long_text,
            "text_source": text_source,
            "text_truncated_in_snapshot": bool(post.get("text_truncated_in_snapshot")),
            "text_length": int(post.get("text_length") or len(text_plain)),
            "media_urls": media_urls,
            "pic_count": int(post.get("pic_count") or len(media_urls)),
            "upstream_source_name": post.get("source"),
            "retweeted_status_id": post.get("retweeted_status_id"),
        }
        for key in PASSTHROUGH_SCORE_FIELDS:
            if key in post and post[key] is not None:
                payload[key] = post[key]
        for key in PASSTHROUGH_GROUP_FIELDS:
            value = post.get(key)
            if value:
                payload[key] = value

        tags: list[str] = []
        for candidate in (payload.get("matched_useful_groups") or []):
            tag = str(candidate).strip()
            if tag and tag not in tags:
                tags.append(tag)
        for candidate in (payload.get("matched_judgement_groups") or []):
            tag = str(candidate).strip()
            if tag and tag not in tags:
                tags.append(tag)
        tags = tags[:32]

        item = Item(
            id=compute_item_id(source, uid=uid, post_id=post_id),
            source=source,
            url=url,
            title=title,
            body=text_plain[:100000],
            author=author,
            ts=ts,
            lang="zh",
            summary=text_plain[:2000] or None,
            tags=tags,
        )
        return ItemRecord(
            item=item,
            adapter_name=self.name,
            source_payload_json=json.dumps(payload, ensure_ascii=False, default=str),
            media_urls=media_urls,
        )
