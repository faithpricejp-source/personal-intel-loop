from __future__ import annotations

import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

from personal_intel_loop import APP_HOME, DATA_DIR
from personal_intel_loop.schemas import Item, compute_item_id, sha1_hex

logger = logging.getLogger(__name__)

def _default_transcript_roots() -> dict[str, Path]:
    """本机转录/文章 markdown 的根目录, 键是平台名(会成为 source 后缀 local_transcripts:<键>)。

    环境变量 PIL_TRANSCRIPT_ROOTS 覆盖, 写成 JSON 对象: {"podcast": "/path/to/podcasts", ...}。
    缺省只有一个 <PIL_HOME>/transcripts/podcast。键 caixin 会启用财新周刊结构页过滤。
    """
    raw = os.environ.get("PIL_TRANSCRIPT_ROOTS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): Path(str(v)).expanduser() for k, v in parsed.items()}
        except ValueError:
            logger.warning("PIL_TRANSCRIPT_ROOTS is not valid JSON; using default")
    return {"podcast": APP_HOME / "transcripts" / "podcast"}


DEFAULT_TRANSCRIPT_ROOTS: dict[str, Path] = _default_transcript_roots()

# 财新周刊的结构性非文章页: 导播/目录/读者来信/答疑/"读周刊 看视频"。
# 实测约 15% 的条目是这类, 且它们标题贴合当期热点, 在 enrich/active 上打分不低, 会挤占日报席位。
STRUCTURAL_TITLE_RE = re.compile(r"(读周刊\s*看视频|周刊导播|^回声（|^答疑（|封面目录|编辑寄语)")

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n+", re.DOTALL)
LEADING_H1_RE = re.compile(r"\A\s*#\s+(.+?)\s*(?:\n+|\Z)")
LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
INLINE_CODE_RE = re.compile(r"`([^`]+)`")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
LIST_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
QUOTE_RE = re.compile(r"^\s*>\s?", re.MULTILINE)
TABLE_RULE_RE = re.compile(r"^\s*\|?[-: ]+\|[-|: ]*\s*$", re.MULTILINE)
WHITESPACE_RE = re.compile(r"[ \t]+")
EPISODE_INDEX_RE = re.compile(r"^(\d{1,4})")

# ts 优先取文件里真实的发布日期，取不到才用 mtime。
# mtime 往往只是下载批次时间（上万条只落在十几个日期），用它发布时间就整体丢失。
DEFAULT_STATE_PATH = DATA_DIR / "cache" / "local_transcripts_state.json"
#: 文件名里的日期: 2024-03-11 / 2024.03.11 / 2024_03_11 / 20240311
FILENAME_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)")
#: 正文首行里的日期（财新导语常有「2024年3月11日」）
CJK_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
#: frontmatter 里常见的日期键
DATE_META_KEYS = ("date", "published", "published_at", "publishedAt", "pubDate", "created", "created_at")
MAX_WATERMARK_PATHS = 200_000

BODY_PREVIEW_LIMIT = 12000
SUMMARY_LIMIT = 2000
TRANSCRIPT_MAX = 500000


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


def _read_text(path: Path) -> str:
    try:
        return path.read_text("utf-8")
    except UnicodeDecodeError:
        return path.read_text("utf-8", errors="replace")


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except Exception:
        meta = {}
    return meta if isinstance(meta, dict) else {}, raw[match.end() :]


def _strip_markdown(text: str) -> str:
    cleaned = str(text or "")
    cleaned = CODE_FENCE_RE.sub(" ", cleaned)
    cleaned = IMAGE_RE.sub(" ", cleaned)
    cleaned = LINK_RE.sub(r"\1", cleaned)
    cleaned = INLINE_CODE_RE.sub(r"\1", cleaned)
    cleaned = HEADING_RE.sub("", cleaned)
    cleaned = LIST_RE.sub("", cleaned)
    cleaned = QUOTE_RE.sub("", cleaned)
    cleaned = TABLE_RULE_RE.sub("", cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[str] = []
    for block in cleaned.split("\n\n"):
        line_text = "\n".join(WHITESPACE_RE.sub(" ", line).strip() for line in block.splitlines())
        normalized = html.unescape(line_text).strip()
        if normalized:
            blocks.append(normalized)
    return "\n\n".join(blocks)


def _extract_title(meta: dict, body_md: str, path: Path) -> tuple[str, str]:
    title = str(meta.get("title") or "").strip()
    remaining = body_md
    if not title:
        match = LEADING_H1_RE.match(body_md)
        if match:
            title = match.group(1).strip()
            remaining = body_md[match.end() :]
    if not title:
        title = path.stem.strip()
    return title, remaining


def _episode_index(path: Path) -> int | None:
    match = EPISODE_INDEX_RE.match(path.stem)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _date_from_match(year: str, month: str, day: str) -> datetime | None:
    # 年份限定在 2000-2099：`_iter` 里的集数编号（如「1203-08-11」这种）不该被当成日期。
    if not (2000 <= int(year) <= 2099):
        return None
    try:
        return datetime(int(year), int(month), int(day), tzinfo=timezone.utc)
    except ValueError:
        return None


def _date_from_text(text: str) -> datetime | None:
    match = FILENAME_DATE_RE.search(str(text or ""))
    if match:
        got = _date_from_match(match.group(1), match.group(2), match.group(3))
        if got is not None:
            return got
    match = CJK_DATE_RE.search(str(text or ""))
    if match:
        return _date_from_match(match.group(1), f"{int(match.group(2)):02d}", f"{int(match.group(3)):02d}")
    return None


def _content_date(meta: dict, path: Path, body_md: str) -> datetime | None:
    """按 frontmatter → 文件名 → 正文首行的顺序找真实发布日期。"""
    for key in DATE_META_KEYS:
        got = _date_from_text(str(meta.get(key) or ""))
        if got is not None:
            return got
    got = _date_from_text(path.stem)
    if got is not None:
        return got
    # 正文只认「开头就是日期」的电头行：课程类正文里随手提到的历史日期（「iPhone 十年」→2007-01-09）
    # 不是发布日期（否则课程类条目会被抓成十几年前的日期）
    for line in body_md.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        head = line[:20]
        got = _date_from_text(head)
        if got is not None and (FILENAME_DATE_RE.match(head) or CJK_DATE_RE.match(head)):
            return got
        return None
    return None


def _load_state(path: Path) -> dict:
    """旧 state（没有 file_state 键）读进来不报错，按空水位处理。"""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
        tmp.replace(path)
    except OSError as exc:  # pragma: no cover
        logger.warning("local_transcripts: 无法写 state %s: %s", path, exc)


def _file_state(stat_result: os.stat_result) -> list[float]:
    return [round(stat_result.st_mtime, 6), stat_result.st_size]


def _iter_transcript_files(
    roots: dict[str, Path],
    *,
    since: datetime | None = None,
) -> list[tuple[float, str, Path, os.stat_result]]:
    since_utc = since.astimezone(timezone.utc) if since else None
    files: list[tuple[float, str, Path, os.stat_result]] = []
    for platform, root in roots.items():
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            mtime_utc = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if since_utc and mtime_utc < since_utc:
                continue
            files.append((stat.st_mtime, platform, path, stat))
    files.sort(key=lambda item: item[0], reverse=True)
    return files


class LocalTranscriptsAdapter:
    name = "local_transcripts"

    def __init__(self, roots: dict[str, Path] | None = None, *, state_path: Path = DEFAULT_STATE_PATH):
        self.roots = roots or DEFAULT_TRANSCRIPT_ROOTS
        self.state_path = state_path

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        # 10-05 验收 G151：state 里按路径记 (mtime,size) 水位，没变的文件不再重读重写。
        # 原来每轮把 4.45 万个文件全部重读并 upsert（今日 run collected=44500, new=3），
        # 覆盖还会把已算好的向量清空（见 store 侧同批修复）。
        state = _load_state(self.state_path)
        raw_watermark = state.get("file_state")
        watermark: dict[str, list[float]] = {
            str(k): list(v)
            for k, v in raw_watermark.items()
            if isinstance(v, (list, tuple)) and len(v) == 2
        } if isinstance(raw_watermark, dict) else {}

        files = _iter_transcript_files(self.roots, since=since)
        if limit is not None:
            files = files[:limit]
        emitted = 0
        seen_this_run: set[str] = set()
        for mtime, platform, path, stat in files:
            local_path = str(path)
            mark = _file_state(stat)
            if watermark.get(local_path) == mark:
                continue  # 没变过，跳过
            seen_this_run.add(local_path)
            root = self.roots[platform]
            rel_parts = path.relative_to(root).parts
            if len(rel_parts) < 2:
                continue
            series_name = rel_parts[0].strip() or root.name
            # rglob→read 之间文件可能被删/被锁(清理脚本、外置卷卸载、TCC 拒读),
            # read_text 抛 OSError 未捕会穿透 generator 挂掉整轮 ingest —— 与上面 stat 的
            # OSError 防护对齐, 单个坏文件只跳过不致命。
            try:
                raw = _read_text(path)
            except OSError as exc:
                logger.warning("local_transcripts: 读不了 %s: %s", local_path, exc)
                continue
            meta, body_md = _split_frontmatter(raw)
            title, body_md = _extract_title(meta, body_md, path)
            plain_text = _strip_markdown(body_md)
            if not plain_text:
                continue
            if platform == "caixin" and STRUCTURAL_TITLE_RE.search(title):
                continue

            source = f"{self.name}:{platform}"
            synthetic_url = f"https://local.intel-loop/transcripts/{platform}/{sha1_hex(local_path)}"
            tags = ["local_transcript", platform]
            if series_name and len(series_name) <= 128:
                tags.append(series_name)

            # 10-05 验收 G151：ts 优先取文件里的真实日期，mtime 只作兜底并标 ts_source。
            content_date = _content_date(meta, path, body_md)
            if content_date is None:
                item_ts = datetime.fromtimestamp(mtime, tz=timezone.utc)
                ts_source = "mtime"
            else:
                item_ts = content_date
                ts_source = "content"

            # 10-05 验收 G158：全文写进 transcript（schemas 允许 50 万字），body 保持 12k 预览；
            # truncated 必须维持 False —— 置 True 会让 digest.py:273 把长播客全部踢出候选池。
            item = Item(
                id=compute_item_id(source, url=synthetic_url),
                source=source,
                url=synthetic_url,
                title=title[:512],
                body=plain_text[:BODY_PREVIEW_LIMIT],
                author=series_name[:256],
                ts=item_ts,
                lang="zh",
                transcript=plain_text[:TRANSCRIPT_MAX] or None,
                summary=plain_text[:SUMMARY_LIMIT] or None,
                tags=tags,
            )
            payload = {
                "platform": platform,
                "series_name": series_name,
                "local_path": local_path,
                "episode_index": _episode_index(path),
                "content_char_len": len(plain_text),
                "frontmatter_title": meta.get("title"),
                "title_source": "frontmatter" if meta.get("title") else "heading_or_filename",
                "truncated": False,
                "media_urls": [],
                "ts_source": ts_source,
            }
            emitted += 1
            yield ItemRecord(
                item=item,
                adapter_name=self.name,
                source_payload_json=json.dumps(payload, ensure_ascii=False),
                media_urls=[],
            )

        # 水位只在整轮跑完后落盘：中途异常不会把没读成的文件误标成已处理。
        watermark.update({local: _file_state(_safe_stat(Path(local))) for local in seen_this_run})
        if len(watermark) > MAX_WATERMARK_PATHS:
            keep = sorted(seen_this_run, key=lambda p: _safe_stat(Path(p)).st_mtime, reverse=True)
            watermark = {p: watermark[p] for p in keep[:MAX_WATERMARK_PATHS]}
        _save_state(
            self.state_path,
            {
                "file_state": watermark,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "emitted": emitted,
            },
        )


def _safe_stat(path: Path) -> os.stat_result:
    try:
        return path.stat()
    except OSError:  # pragma: no cover - 文件刚被删
        return os.stat_result((0, 0, 0, 0, 0, 0, 0, 0, 0, 0))
