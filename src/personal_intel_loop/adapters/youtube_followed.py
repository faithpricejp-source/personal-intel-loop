"""YouTube followed-channels adapter.

Architecture (per project roadmap 2026-04-20):
    channels.json (manual or auto-bootstrapped)
       ↓ for each channel_id
    https://www.youtube.com/feeds/videos.xml?channel_id=<id>   (no auth, ~15 recent)
       ↓ each entry → (video_id, title, published_at, author)
    youtube_transcript_api.fetch(video_id)                     (no auth, no key)
       ↓ transcript text
    pil Item (source=youtube_followed:<channel_id>)

RSS + transcript are both reader-tier endpoints, so this path avoids the auth +
anti-bot costs of logged-in aggregator routes. If YouTube tightens RSS in the future,
fall back to logged-in feed/subscriptions HTML scraping, then to /feed/trending.

channels.json shape (minimal):
    {
      "channels": [
        {"channel_id": "UCbRP3c757lWg9M-U7TyEkXA", "channel_name": "Andrej Karpathy"},
        {"channel_id": "UCXZCJLdBC09xxGZ6gcdrc6A", "channel_name": "OpenAI"}
      ]
    }

body limit: pil Item.body has max_length=100000. A 2-hour video ≈ 120K chars of
transcript, so we truncate — not a rank signal anyway; pil summarizer/ranker use
the first slice.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from personal_intel_loop import DATA_DIR
from personal_intel_loop.schemas import Item, compute_item_id

logger = logging.getLogger(__name__)

DEFAULT_CHANNELS_FILE = (
    Path(__file__).resolve().parents[3] / "data" / "youtube_channels.json"
)
DEFAULT_LOOKBACK_HOURS = 72  # YouTube uploads are less frequent than news; widen window
VIDEO_URL_TEMPLATE = "https://www.youtube.com/watch?v={video_id}"
# flat 抽取拿不到上传时间, ts 只能合成。合成值按 video_id 记首次见到的时刻并固定下来,
# 否则每轮都被刷成「当前时刻 - idx 分钟」, 老视频永远落在 lookback 窗内反复进日报。
DEFAULT_FIRST_SEEN_PATH = DATA_DIR / "cache" / "youtube_first_seen.json"
BODY_MAX = 100000


@dataclass
class ItemRecord:
    item: Item
    adapter_name: str
    source_payload_json: str
    media_urls: list[str]


# Ordering matters — the FIRST `channelId` field on a @handle page can belong to
# a featured/recommended channel, not the one we're looking at. `externalId` and
# the canonical `/channel/UCxxx` URL are the owner-identity fields.
_CHANNEL_ID_PATTERNS = [
    re.compile(r'"externalId"\s*:\s*"(UC[A-Za-z0-9_\-]{22})"'),
    re.compile(r'<link\s+rel="canonical"[^>]*channel/(UC[A-Za-z0-9_\-]{22})'),
    re.compile(r'"ownerUrls"\s*:\s*\[[^\]]*?/channel/(UC[A-Za-z0-9_\-]{22})'),
    re.compile(r'"browseId"\s*:\s*"(UC[A-Za-z0-9_\-]{22})"'),
    re.compile(r'"channelId"\s*:\s*"(UC[A-Za-z0-9_\-]{22})"'),
]
_HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_\-.]+)$")


def resolve_channel_id(handle_or_url: str, *, timeout: float = 15.0) -> str | None:
    """Resolve a @handle or youtube channel URL to its canonical `UCxxx...` id.

    Accepts:
      @PatrickBoyleOnFinance
      PatrickBoyleOnFinance
      https://www.youtube.com/@PatrickBoyleOnFinance
      https://www.youtube.com/c/SomeChannel
      https://www.youtube.com/channel/UCxxx (passthrough)
    """
    raw = handle_or_url.strip()
    if not raw:
        return None
    # passthrough: already a channel_id or URL containing it
    m = re.search(r"(UC[A-Za-z0-9_\-]{22})", raw)
    if m:
        return m.group(1)

    if raw.startswith("http"):
        url = raw.rstrip("/")
    else:
        handle_match = _HANDLE_RE.match(raw)
        if not handle_match:
            return None
        handle = handle_match.group(1)
        url = f"https://www.youtube.com/@{handle}"

    # YouTube returns 404 to stock `requests` user-agents for @handle pages —
    # needs real browser TLS fingerprint. Use curl_cffi.
    try:
        from curl_cffi.requests import Session as _CurlSession
        with _CurlSession(impersonate="chrome136") as s:
            r = s.get(
                url,
                timeout=timeout,
                headers={
                    "accept": "text/html,application/xhtml+xml,*/*;q=0.9",
                    "accept-language": "en-US,en;q=0.9",
                },
            )
        if r.status_code != 200:
            logger.warning("resolve_channel_id HTTP %d for %s", r.status_code, url)
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolve_channel_id fetch failed %s: %s", url, exc)
        return None

    for pat in _CHANNEL_ID_PATTERNS:
        m = pat.search(r.text)
        if m:
            return m.group(1)
    return None


def load_channels(path: Path = DEFAULT_CHANNELS_FILE) -> list[dict]:
    """Load channels.json. Entries may specify `channel_id` directly OR `handle`
    (which is auto-resolved once and written back into the file).
    """
    if not path.exists():
        logger.warning("youtube channels file missing: %s", path)
        return []
    try:
        payload = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as exc:
        logger.warning("youtube channels file invalid JSON: %s", exc)
        return []
    channels = payload.get("channels") if isinstance(payload, dict) else None
    if not isinstance(channels, list):
        return []

    clean: list[dict] = []
    mutated = False
    for c in channels:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("channel_id") or "").strip()
        if not cid:
            handle = str(c.get("handle") or "").strip()
            if not handle:
                continue
            resolved = resolve_channel_id(handle)
            if not resolved:
                logger.warning("could not resolve handle %r; skipping", handle)
                continue
            cid = resolved
            c["channel_id"] = cid  # persist resolution back
            mutated = True
            logger.info("resolved handle %r → %s", handle, cid)
        clean.append({
            "channel_id": cid,
            "channel_name": str(c.get("channel_name") or c.get("handle") or "").strip(),
            "handle": str(c.get("handle") or "").strip(),
            "category": str(c.get("category") or "").strip(),
        })

    if mutated:
        try:
            path.write_text(
                json.dumps({"channels": channels}, ensure_ascii=False, indent=2),
                "utf-8",
            )
        except OSError as exc:
            logger.warning("failed to persist resolved channel IDs back to %s: %s", path, exc)

    return clean


def fetch_channel_videos(
    channel_id: str, *, limit: int = 15
) -> list[dict] | None:
    """Return recent video metadata for a channel via yt-dlp.

    Returns None when the fetch itself failed (10-05 验收 F206); an empty list
    means the channel genuinely has no videos. Callers must tell them apart.

    YouTube deprecated the public `/feeds/videos.xml?channel_id=...` RSS endpoint
    (404 as of 2026-04). yt-dlp is the stable replacement — extracts from the
    channel /videos tab and handles layout changes via its maintained extractor.

    extract_flat='in_playlist' returns minimal metadata (no upload timestamps —
    those require per-video calls). We trust the natural channel ordering
    (newest first) and synthesize a strictly-decreasing ts in the caller so pil's
    sort-by-ts stays sane.
    """
    from yt_dlp import YoutubeDL

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "playlistend": limit,
        "skip_download": True,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/channel/{channel_id}/videos",
                download=False,
            )
    except Exception as exc:  # noqa: BLE001
        # 10-05 验收 F206: 失败返回 None, 与「频道真的没有视频」([]) 区分开。
        # 原来两者都是 []，全频道失败时 lane 出 0 条而 summary 仍写 ok。
        logger.warning("yt-dlp channel fetch failed %s: %s", channel_id, exc)
        return None

    entries = info.get("entries") if isinstance(info, dict) else None
    if not isinstance(entries, list):
        return []

    out: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        vid = e.get("id")
        title = e.get("title")
        if not vid or not title:
            continue
        out.append(
            {
                "video_id": str(vid),
                "title": str(title).strip(),
                "uploader": str(e.get("uploader") or "").strip(),
                "duration": e.get("duration"),  # seconds, may be None
            }
        )
    return out


def fetch_transcript(
    video_id: str,
    *,
    preferred_langs: tuple[str, ...] = ("zh", "zh-Hans", "zh-CN", "en"),
) -> str:
    """Return concatenated transcript text (via yt-dlp). Empty string if unavailable.

    Why yt-dlp not youtube-transcript-api: youtube-transcript-api hits a
    transcript-specific endpoint that IP-limits aggressively (blocked for us
    after ~5 calls in a session). yt-dlp uses the same cookie-aware HTTP path as
    the rest of our pipeline, handles anti-bot headers, and is the library's
    own recommended fallback.

    yt-dlp writes subtitle files to disk then reads. We use a temp dir and clean
    up. Format: VTT (YouTube's default) — minimal parsing to strip timing lines
    and cue markers.
    """
    import subprocess
    import tempfile

    lang_opt = ",".join(preferred_langs) + ",en"
    with tempfile.TemporaryDirectory() as td:
        out_tpl = f"{td}/%(id)s.%(ext)s"
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs", lang_opt,
            "--sub-format", "vtt",
            "--quiet", "--no-warnings",
            "-o", out_tpl,
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            logger.warning("yt-dlp transcript timed out for %s", video_id)
            return ""
        if r.returncode != 0:
            logger.info("yt-dlp no transcript for %s: %s", video_id, r.stderr.strip()[:200])
            return ""
        # find any .vtt file yt-dlp wrote
        from pathlib import Path as _P
        vtt_files = sorted(_P(td).glob(f"{video_id}*.vtt"))
        if not vtt_files:
            return ""
        # prefer the first language in preferred_langs that exists
        chosen = vtt_files[0]
        for lang in preferred_langs:
            match = [p for p in vtt_files if f".{lang}." in p.name]
            if match:
                chosen = match[0]
                break
        try:
            content = chosen.read_text("utf-8", errors="ignore")
        except OSError:
            return ""
    return _vtt_to_plain(content)


def _vtt_to_plain(vtt: str) -> str:
    """Strip VTT timing cues and return concatenated speech text."""
    lines: list[str] = []
    seen: set[str] = set()  # youtube auto-subs repeat lines with rolling overlap
    for line in vtt.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("WEBVTT") or stripped.startswith("Kind:") or stripped.startswith("Language:"):
            continue
        if "-->" in stripped:
            continue
        # remove inline <c> / <v> tags common in auto-subs
        clean = re.sub(r"<[^>]+>", "", stripped)
        clean = re.sub(r"&nbsp;", " ", clean).strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        lines.append(clean)
    return " ".join(lines)


class YouTubeFollowedAdapter:
    name = "youtube_followed"

    def __init__(
        self,
        channels_file: Path = DEFAULT_CHANNELS_FILE,
        lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
        first_seen_path: Path = DEFAULT_FIRST_SEEN_PATH,
    ):
        self.channels_file = channels_file
        self.lookback_hours = lookback_hours
        self.first_seen_path = first_seen_path
        #: 10-05 验收 F206: 本轮的失败原因, 供 cli 读（项目里没有"失败原因"约定）
        self.last_errors: list[str] = []

    def _load_first_seen(self) -> dict[str, str]:
        try:
            data = json.loads(self.first_seen_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_first_seen(self, first_seen: dict[str, str]) -> None:
        self.first_seen_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.first_seen_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(first_seen, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(self.first_seen_path)

    def collect(
        self,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterable[ItemRecord]:
        fallback_now = datetime.now(timezone.utc)
        since_utc = (
            since.astimezone(timezone.utc)
            if since
            else fallback_now - timedelta(hours=self.lookback_hours)
        )
        records: dict[str, ItemRecord] = {}

        # Stage 1: collect all candidate video metadata (cheap — yt-dlp flat extract).
        # We DO NOT fetch transcripts here because `limit` may cap to a small subset
        # and transcript fetches are expensive (seconds each, × N channels × 15 videos).
        candidates: list[dict] = []
        first_seen = self._load_first_seen()
        first_seen_changed = False
        self.last_errors = []
        # 10-05 验收 F206: yt-dlp 失败(None) 与「频道无视频」([]) 分开统计
        failed_channels: list[str] = []
        total_channels = 0
        for channel in load_channels(self.channels_file):
            channel_id = channel["channel_id"]
            channel_name = channel.get("channel_name") or ""
            category = channel.get("category") or ""
            total_channels += 1
            videos = fetch_channel_videos(channel_id, limit=15)
            if videos is None:
                failed_channels.append(channel_id)
                continue
            if not videos:
                continue
            for idx, video in enumerate(videos):
                seen_iso = first_seen.get(video["video_id"])
                if seen_iso:
                    ts_utc = datetime.fromisoformat(seen_iso)
                else:
                    ts_utc = fallback_now - timedelta(minutes=idx)
                    first_seen[video["video_id"]] = ts_utc.isoformat()
                    first_seen_changed = True
                if ts_utc < since_utc:
                    continue
                candidates.append({
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "category": category,
                    "source": f"{self.name}:{channel_id}",
                    "video_id": video["video_id"],
                    "title": video["title"],
                    "author": video.get("uploader") or channel_name,
                    "duration": video.get("duration"),
                    "ts_utc": ts_utc,
                    "channel_order_idx": idx,
                })

        if first_seen_changed:
            self._save_first_seen(first_seen)

        # 10-05 验收 F206: 过半频道失败时 last_errors 要带失败数, 否则全灭也是 summary ok + 0 条
        if total_channels and len(failed_channels) * 2 > total_channels:
            reason = (
                f"{len(failed_channels)}/{total_channels} 个频道 yt-dlp 取数失败"
                f"（{', '.join(failed_channels[:5])}）"
            )
            logger.warning("youtube_followed: %s", reason)
            self.last_errors.append(reason)

        # Stage 2: sort by ts, cap to `limit`, THEN fetch transcripts. This bounds
        # transcript RPCs to what pil's digest will actually rank.
        candidates.sort(key=lambda c: c["ts_utc"], reverse=True)
        if limit is not None:
            candidates = candidates[:limit]

        for c in candidates:
            video_id = c["video_id"]
            transcript = fetch_transcript(video_id)
            url = VIDEO_URL_TEMPLATE.format(video_id=video_id)
            body = transcript[:BODY_MAX]
            tags = ["youtube"]
            if c["category"]:
                tags.append(c["category"])
            item = Item(
                id=compute_item_id(c["source"], url=url, guid=video_id),
                source=c["source"],
                url=url,
                title=c["title"],
                body=body,
                author=c["author"],
                ts=c["ts_utc"],
                lang="unknown",
                summary=body[:2000] or None,
                tags=tags,
            )
            payload = {
                "channel_id": c["channel_id"],
                "channel_name": c["channel_name"],
                "category": c["category"],
                "video_id": video_id,
                "channel_order_idx": c["channel_order_idx"],
                "duration_seconds": c["duration"],
                "transcript_chars": len(transcript),
                "has_transcript": bool(transcript),
            }
            records[item.id] = ItemRecord(
                item=item,
                adapter_name=self.name,
                source_payload_json=json.dumps(payload, ensure_ascii=False),
                media_urls=[],
            )

        return list(records.values())
