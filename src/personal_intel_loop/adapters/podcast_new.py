"""播客/小宇宙/视频新节目 adapter (`podcast_new`)。

输入不是网络而是**本机已有的转录产物**：一个或多个转录根目录（构造参数），每集一个
`.md` 或 `.txt`，同目录可能有同名 `.json` 元数据。

三种目录形态都要认：
    <root>/<节目名>/2026-10-02_标题.md          + 2026-10-02_标题.json  ← 有元数据
    <root>/<节目名>/2026-10-02_标题.md                             ← 名字里带日期
    <root>/<节目名>/标题.md                                         ← 日期缺失用文件 mtime

只收 `pub_date` 在最近 `days` 天内的集；已收过的（按 url / 合成 url）跳过。转录全文进
`Item.transcript`，`body` 只放前 2000 字——日报版面用不上几十万字，但全文要留着给搜索和
讨论综述。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import os

from personal_intel_loop import APP_HOME, DATA_DIR
from personal_intel_loop.schemas import Item, compute_item_id, sha1_hex

logger = logging.getLogger(__name__)

# ---- 缺省常量--------------------------------------------------------------
#: 本机播客转录产物根目录（构造参数可覆盖；测试传 tmp_path）。
#: 环境变量 PIL_PODCAST_ROOTS 覆盖, 多个目录用 os.pathsep(冒号)分隔。
DEFAULT_ROOTS: tuple[Path, ...] = tuple(
    Path(p).expanduser() for p in (os.environ.get("PIL_PODCAST_ROOTS") or str(APP_HOME / "transcripts" / "podcast")).split(os.pathsep) if p
)
DEFAULT_STATE_PATH = DATA_DIR / "cache" / "podcast_new_state.json"
DEFAULT_DAYS = 3

# ---- 行为常量--------------------------------------------------------------
SEEN_LIMIT = 4000
BODY_CHARS = 2000
TRANSCRIPT_MAX = 500000
TITLE_MAX = 512
TRANSCRIPT_SUFFIXES = (".md", ".txt")
METADATA_SUFFIX = ".json"
EPISODE_URL_PREFIX = "https://local.intel-loop/podcast/"

DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[_\-\s]+(.+)$")
DATE_ONLY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def read_text(path: Path) -> str:
    try:
        return path.read_text("utf-8")
    except UnicodeDecodeError:
        return path.read_text("utf-8", errors="replace")


def load_metadata(transcript_path: Path) -> dict | None:
    """读同目录同名 `.json` 元数据；没有/坏了返回 None。"""
    meta_path = transcript_path.with_suffix(METADATA_SUFFIX)
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.info("podcast_new: 元数据不可用 %s: %s", meta_path, exc)
        return None
    return data if isinstance(data, dict) else None


def parse_from_filename(transcript_path: Path) -> tuple[str, datetime | None]:
    """从文件名解析 (集标题, 日期)。认 `<日期>_<标题>` 与 `<标题>`（无日期）。"""
    stem = transcript_path.stem.strip()
    ts: datetime | None = None
    title = stem

    match = DATE_PREFIX_RE.match(stem)
    if match:
        year, month, day, rest = match.groups()
        try:
            # 文件名只有日期没有时刻，按当地中午落地，避免时区把日期掰到前一天
            naive = datetime(int(year), int(month), int(day), 12, 0, 0)
            ts = naive.astimezone()
        except ValueError:
            ts = None
        title = rest.strip() or stem
    elif DATE_ONLY_RE.match(stem):
        try:
            year, month, day = stem.split("-")
            ts = datetime(int(year), int(month), int(day), 12, 0, 0).astimezone()
        except ValueError:
            ts = None
        title = stem

    return title, ts.astimezone(timezone.utc) if ts else None


def coerce_ts(value: object) -> datetime | None:
    """接受 ISO 8601 字符串或 epoch 秒/毫秒。解析不了返回 None。"""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.astimezone()).astimezone(timezone.utc)

    if isinstance(value, (int, float)):
        return _from_epoch(int(value))

    raw = str(value).strip()
    if not raw:
        return None
    try:
        return _from_epoch(int(raw))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.astimezone()).astimezone(timezone.utc)


def _from_epoch(number: int) -> datetime | None:
    if number <= 0:
        return None
    if number > 10_000_000_000:  # 毫秒
        number //= 1000
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _first_str(meta: dict, *keys: str) -> str:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(path)
    except OSError as exc:  # pragma: no cover
        logger.warning("podcast_new: 无法写 state %s: %s", path, exc)


class PodcastNewAdapter:
    name = "podcast_new"

    def __init__(
        self,
        roots: Iterable[Path] | None = None,
        *,
        state_path: Path = DEFAULT_STATE_PATH,
        days: int = DEFAULT_DAYS,
        now_fn: Callable | None = None,
    ) -> None:
        self.roots = tuple(Path(root) for root in (roots if roots is not None else DEFAULT_ROOTS))
        self.state_path = state_path
        self.days = days
        #: 可注入的「现在」，测试里用来固定时间窗
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        #: 10-05 验收 F216/F219: 本轮的失败原因, 供 cli 读（项目里没有"失败原因"约定）
        self.last_errors: list[str] = []

    def _build_record(
        self,
        transcript_path: Path,
        *,
        root: Path,
        cutoff: datetime,
    ) -> ItemRecord | None:
        meta = load_metadata(transcript_path)
        file_title, file_ts = parse_from_filename(transcript_path)

        show = _first_str(meta or {}, "podcast", "show", "show_name", "channel")
        if not show:
            rel = transcript_path.relative_to(root)
            show = rel.parts[0] if len(rel.parts) > 1 else root.name
        show = show.strip() or root.name

        title = _first_str(meta or {}, "title", "episode_title") or file_title
        title = title.strip()
        if not title:
            return None

        meta_ts = coerce_ts(
            (meta or {}).get("pub_date")
            or (meta or {}).get("published")
            or (meta or {}).get("published_at")
        )
        if meta_ts is not None:
            ts, ts_source = meta_ts, "metadata"
        elif file_ts is not None:
            ts, ts_source = file_ts, "filename"
        else:
            try:
                ts = datetime.fromtimestamp(transcript_path.stat().st_mtime, tz=timezone.utc)
                ts_source = "mtime"
            except OSError:
                logger.info("podcast_new: 拿不到 mtime, 跳过 %s", transcript_path)
                return None
        if ts < cutoff:
            return None

        url = _first_str(meta or {}, "url", "link", "episode_url")
        if not url:
            # 没链接就造一个稳定的本地地址：同一集永远同一个 id，去重与收藏链接都靠它
            url = EPISODE_URL_PREFIX + sha1_hex(str(transcript_path))

        # rglob 之后文件可能被删/外置卷部分目录不可读, read_text 抛 OSError 未捕
        # 会让整轮 collect 中断(卷整体没挂有 F219 挡, 单文件级没有) —— 跳过该集不致命。
        try:
            transcript = read_text(transcript_path).strip()
        except OSError as exc:
            logger.warning("podcast_new: 读不了 %s: %s", transcript_path, exc)
            return None
        if not transcript:
            return None

        duration = None
        if meta:
            for key in ("duration", "duration_s", "duration_seconds"):
                value = meta.get(key)
                if value in (None, ""):
                    continue
                try:
                    duration = float(value)
                    break
                except (TypeError, ValueError):
                    continue

        source = f"{self.name}:{show}"
        item = Item(
            id=compute_item_id(source, url=url),
            source=source,
            url=url,
            title=title[:TITLE_MAX],
            body=transcript[:BODY_CHARS],
            author=show[:256],
            ts=ts,
            lang="zh",
            transcript=transcript[:TRANSCRIPT_MAX],
            summary=transcript[:BODY_CHARS] or None,
            tags=["podcast", show[:128]],
        )
        payload = {
            "show": show,
            "title": title,
            "local_path": str(transcript_path),
            "has_metadata": meta is not None,
            "ts_source": ts_source,
            "pub_date": ts.isoformat(),
            "url": url,
            "synthetic_url": not _first_str(meta or {}, "url", "link", "episode_url"),
            "media": {
                "type": "audio",
                "duration_s": duration,
            },
            "transcript_chars": len(transcript),
        }
        return ItemRecord(
            item=item,
            adapter_name=self.name,
            source_payload_json=json.dumps(payload, ensure_ascii=False),
            media_urls=[],
        )

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        now = self.now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        # 与上面 now 的归一对齐 —— naive 的 since 也按 UTC 解释, 不随宿主 TZ 漂移
        # (cli --since 给的是 naive 值, 原来 astimezone 按宿主本地时区算, who_don R09 同款)。
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        cutoff = since.astimezone(timezone.utc) if since else now - timedelta(days=self.days)

        state = _load_state(self.state_path)
        seen: list[str] = [str(value) for value in (state.get("seen") or [])]
        seen_set = set(seen)
        records: dict[str, ItemRecord] = {}
        self.last_errors = []

        for root in self.roots:
            for transcript_path in self._iter_transcripts_for(root):
                record = self._build_record(transcript_path, root=root, cutoff=cutoff)
                if record is None:
                    continue
                if record.item.url in seen_set:
                    continue  # 已收过
                records[record.item.id] = record

        out = sorted(records.values(), key=lambda record: record.item.ts, reverse=True)
        if limit is not None:
            out = out[:limit]

        # 10-05 验收 F216: 先按 --limit 截断, 再只把实际输出的条目记进 seen。
        # 原来先写 seen 存盘最后才截断, 被截掉的集下一轮被判"已收过" → 永久丢集。
        seen.extend(record.item.url for record in out)
        _save_state(
            self.state_path,
            {
                "seen": seen[-SEEN_LIMIT:],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return out

    def _iter_transcripts_for(self, root: Path) -> list[Path]:
        if not root.is_dir():
            # 10-05 验收 F219: 根目录在外置卷上, 卷没挂或 launchd TCC 拒访(exit 78)
            # 都走到这里。原来只记 info(项目没配 handler, 实际从不输出), summary 还是 ok + 0 条。
            reason = f"转录根目录不存在 {root}(外置卷未挂载或无权访问)"
            logger.warning("podcast_new: %s", reason)
            self.last_errors.append(reason)
            return []
        files = [
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in TRANSCRIPT_SUFFIXES
        ]
        files.sort()
        return files