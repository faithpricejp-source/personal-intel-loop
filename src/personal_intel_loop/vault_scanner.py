"""Auto-detect vault promotion events — self-closing feedback loop.

When the user creates or edits a canonical vault object (SRC/EVD/JDG/MON/DEC/CAS/
MEC/DIA/HEU) that cites a pil item's URL, this scanner records it as a
`promotion_events` row. Those events then flow into `recompute_source_trust`.

The real feedback signal is the promotion action itself, not any checkbox.
This scanner turns that action into a trained signal with zero user friction.

Pure local — no LLM. URL canonicalization + string match + sqlite.

Usage (CLI):
    pil detect-promotions [--since 2026-04-15]
    pil detect-promotions --all          # ignore last-scan timestamp, walk everything

launchd trigger suggestion: once a day, off-peak (after nightly edits settle).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from personal_intel_loop import APP_HOME, DATA_DIR, VAULT_DIR
from personal_intel_loop.schemas import (
    FEEDBACK_EVENT_WEIGHTS,
    canonicalize_url,
    normalize_dt_to_utc_z,
    sha1_hex,
    weight_for,
)

logger = logging.getLogger(__name__)

#: 笔记库根目录(环境变量 PIL_VAULT_DIR); 未配置时指向一个不存在的目录, 相关功能返回空结果。
VAULT_ROOT = VAULT_DIR if VAULT_DIR is not None else APP_HOME / "vault"
SCANNED_LAYERS = (
    "01 来源库",
    "03 证据与溯源",
    "04 案例库",
    "05 知识本体",
    "07 判断与决策",
    "08 注意力配置",
)
LAST_SCAN_PATH = DATA_DIR / ".last_vault_scan"

# Filename prefix → event_type (and canonical object_type).
# Map also covers the 05 知识本体 subdivision (MEC/DIA/HEU).
_OBJECT_TYPE_TO_EVENT = {
    "SRC": "detected_src_reference",
    "EVD": "detected_evd_reference",
    "JDG": "detected_jdg_reference",
    "DEC": "detected_dec_link",
    "CAS": "detected_cas_link",
    "MON": "detected_mon_link",
    "MEC": "detected_mec_enrich",
    "DIA": "detected_dia_enrich",
    "HEU": "detected_heu_enrich",
}

_URL_RE = re.compile(
    r"""
    https?://
    [a-zA-Z0-9][a-zA-Z0-9.\-]*[a-zA-Z0-9]
    (?::\d+)?
    (?:/[^\s)<>"']*)?
    """,
    re.VERBOSE,
)

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# Match pil item_ids inline in vault text. The item_id patterns we issue are
# short prefixes (weibo:, xhs:, rss:, nikkei:) followed by either digits or hex.
_ITEM_ID_RE = re.compile(
    r"\b(?:weibo|xhs|rss|nikkei)(?::[A-Za-z0-9]+){1,2}\b"
)

# Frontmatter URL-like fields we inspect directly (in addition to regex-scanning the body).
_FRONTMATTER_URL_FIELDS = (
    "source",
    "sources",
    "url",
    "reference",
    "references",
    "link",
    "links",
    "origin",
)


@dataclass
class VaultCitation:
    """One extracted citation candidate from a vault .md file."""

    vault_relpath: str  # relative to vault root, e.g. "07 判断与决策/Judgments/JDG_xxx.md"
    object_type: str  # "SRC" / "EVD" / ...
    urls: set[str]  # canonicalized URLs
    item_ids: set[str]  # explicit item_id strings found inline


@dataclass
class ScanResult:
    files_scanned: int
    citations_found: int
    events_inserted: int
    events_skipped_dup: int
    scan_started_at_utc: str
    scan_finished_at_utc: str
    last_scan_persisted_to: str | None = None


def _classify_object_type(filename_stem: str) -> str | None:
    """`JDG_2026-04-18_...` → `JDG`. Returns None if filename doesn't match."""
    match = re.match(r"^(SRC|EVD|JDG|DEC|CAS|MON|MEC|DIA|HEU)[_\-]", filename_stem)
    return match.group(1) if match else None


def _iter_scannable_files(
    vault_root: Path,
    layers: Iterable[str] = SCANNED_LAYERS,
    since_utc: datetime | None = None,
) -> Iterable[Path]:
    """Yield .md files under tracked layers whose mtime >= since_utc."""
    since_ts = since_utc.timestamp() if since_utc else None
    for layer in layers:
        layer_dir = vault_root / layer
        if not layer_dir.is_dir():
            continue
        for md in layer_dir.rglob("*.md"):
            # skip ignored / hidden paths
            rel = md.relative_to(vault_root)
            parts = rel.parts
            if any(p.startswith(".") for p in parts):
                continue
            if since_ts is not None:
                try:
                    mtime = md.stat().st_mtime
                except OSError:
                    continue
                if mtime < since_ts:
                    continue
            yield md


def _extract_frontmatter_urls(frontmatter_text: str) -> set[str]:
    """Parse frontmatter YAML and pull URLs from known URL-like fields."""
    try:
        import yaml
    except Exception:
        return set()
    try:
        data = yaml.safe_load(frontmatter_text) or {}
    except Exception:
        return set()
    urls: set[str] = set()
    if not isinstance(data, dict):
        return urls
    for field in _FRONTMATTER_URL_FIELDS:
        value = data.get(field)
        if not value:
            continue
        if isinstance(value, str):
            for match in _URL_RE.finditer(value):
                urls.add(match.group(0))
        elif isinstance(value, list):
            for entry in value:
                if isinstance(entry, str):
                    for match in _URL_RE.finditer(entry):
                        urls.add(match.group(0))
    return urls


def extract_citations(vault_file: Path, vault_root: Path) -> VaultCitation | None:
    """Parse one vault .md, return None if it's not a tracked object type."""
    rel = vault_file.relative_to(vault_root).as_posix()
    object_type = _classify_object_type(vault_file.stem)
    if object_type is None:
        return None
    try:
        text = vault_file.read_text("utf-8")
    except OSError:
        return None

    urls_raw: set[str] = set()
    frontmatter = ""
    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match:
        frontmatter = fm_match.group(1)
        body = text[fm_match.end() :]
        urls_raw |= _extract_frontmatter_urls(frontmatter)
    else:
        body = text

    for match in _URL_RE.finditer(body):
        urls_raw.add(match.group(0))

    # canonicalize (drops tracking params, lowercases host, etc)
    urls: set[str] = set()
    for raw in urls_raw:
        try:
            urls.add(canonicalize_url(raw))
        except Exception:
            continue

    item_ids: set[str] = set()
    for match in _ITEM_ID_RE.finditer(text):
        item_ids.add(match.group(0))

    return VaultCitation(
        vault_relpath=rel,
        object_type=object_type,
        urls=urls,
        item_ids=item_ids,
    )


def _event_id(vault_relpath: str, item_id: str, event_type: str) -> str:
    return sha1_hex(f"vault_detection|{vault_relpath}|{item_id}|{event_type}")


# Platform-specific id extractors. URLs for the same note/post show up in many
# query-param variants (Safari/app/share dialogs each decorate differently), so
# matching by raw URL string misses most citations. We extract the platform-
# canonical id (xhs note_id / weibo post_id / etc) and look up pil items by id
# prefix instead.
_XHS_NOTE_ID_RE = re.compile(
    r"xiaohongshu\.com/(?:discovery/item|explore)/([a-f0-9]{20,32})"
)
_WEIBO_POST_ID_RE = re.compile(
    r"(?:m\.)?weibo\.(?:cn|com)/(?:status|detail|\d+/)(\d{10,20})"
)


def _resolve_url_to_pil_item(conn: sqlite3.Connection, url: str) -> str | None:
    """Map one canonical URL to a pil item_id, trying platform-specific ids first.

    Returns None when no pil item corresponds to this URL.
    """
    if not url:
        return None
    # xhs: the 24-hex note_id survives all URL decorations
    m = _XHS_NOTE_ID_RE.search(url)
    if m:
        row = conn.execute(
            "SELECT item_id FROM items WHERE item_id = ?",
            (f"xhs:{m.group(1)}",),
        ).fetchone()
        if row:
            return row["item_id"]
    # weibo: post_id is the stable identity (uid varies, path varies)
    m = _WEIBO_POST_ID_RE.search(url)
    if m:
        # 10-05 验收 P09: LIKE 是大小写不敏感的, 吃不掉 BINARY  collation 的主键索引,
        # 14.5 万行 items 只能全扫。改 GLOB 走主键范围搜。GLOB 区分大小写且 * ? [ 是通配符
        # —— _WEIBO_POST_ID_RE 的 (\d{10,20}) 已把 post_id 限死为数字, 模式里无通配符字符,
        # 与改前 LIKE 在真实 item_id(compute_item_id 固定小写 weibo:<uid>:<post_id>)上等价。
        row = conn.execute(
            "SELECT item_id FROM items WHERE item_id GLOB ?",
            (f"weibo:*:{m.group(1)}",),
        ).fetchone()
        if row:
            return row["item_id"]
    # Fallback: exact canonical URL match (catches nikkei / rss / substack / etc.)
    row = conn.execute("SELECT item_id FROM items WHERE url = ?", (url,)).fetchone()
    return row["item_id"] if row else None


def _find_item_ids_for_urls(conn: sqlite3.Connection, urls: set[str]) -> dict[str, str]:
    """Match URLs → pil item_id. Uses platform-specific id extraction with URL fallback."""
    out: dict[str, str] = {}
    for url in urls:
        item_id = _resolve_url_to_pil_item(conn, url)
        if item_id:
            out[url] = item_id
    return out


def _validate_item_ids(conn: sqlite3.Connection, ids: set[str]) -> set[str]:
    """Filter item_ids that don't exist in pil items."""
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    sql = f"SELECT item_id FROM items WHERE item_id IN ({placeholders})"
    rows = conn.execute(sql, tuple(ids)).fetchall()
    return {row["item_id"] for row in rows}


def _insert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    item_id: str,
    source: str,
    event_type: str,
    vault_object_type: str,
    vault_object_id: str,
    vault_relpath: str,
    now_utc: str,
) -> bool:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO promotion_events (
          event_id, item_id, source, event_type, event_weight, origin,
          digest_path, vault_object_type, vault_object_id, note,
          event_ts, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            item_id,
            source,
            event_type,
            weight_for(event_type),
            "vault_detection",
            None,
            vault_object_type,   # "EVD" / "JDG" / "MEC" / ...
            vault_object_id,     # stem of the vault file, e.g. "JDG_2026-04-18_xxx"
            vault_relpath,       # full relpath stored in `note` field for traceback
            now_utc,
            now_utc,
        ),
    )
    return cursor.rowcount > 0


def load_last_scan_ts(path: Path | None = None) -> datetime | None:
    p = path or LAST_SCAN_PATH
    if not p.exists():
        return None
    try:
        iso = p.read_text("utf-8").strip()
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def save_last_scan_ts(ts: datetime, path: Path | None = None) -> None:
    p = path or LAST_SCAN_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(ts.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), "utf-8")


def scan_vault(
    conn: sqlite3.Connection,
    *,
    vault_root: Path = VAULT_ROOT,
    since_utc: datetime | None = None,
    layers: Iterable[str] = SCANNED_LAYERS,
    persist_last_scan: bool = True,
    last_scan_path: Path | None = None,
) -> ScanResult:
    """Walk vault, match citations against items, insert promotion_events."""
    scan_started = datetime.now(timezone.utc)
    now_utc_str = normalize_dt_to_utc_z(scan_started)

    files_scanned = 0
    citations_found = 0
    inserted = 0
    skipped_dup = 0

    for vault_file in _iter_scannable_files(vault_root, layers=layers, since_utc=since_utc):
        files_scanned += 1
        citation = extract_citations(vault_file, vault_root)
        if citation is None:
            continue
        if not citation.urls and not citation.item_ids:
            continue
        citations_found += 1

        event_type = _OBJECT_TYPE_TO_EVENT.get(citation.object_type)
        if event_type is None:
            continue

        # URL-based matches
        url_matches = _find_item_ids_for_urls(conn, citation.urls)
        # item_id-based matches (for inline `weibo:...` references in text)
        direct_matches = _validate_item_ids(conn, citation.item_ids)

        matched_item_ids: set[str] = set(url_matches.values()) | direct_matches
        if not matched_item_ids:
            continue

        # Look up the source for each matched item so promotion_events.source is correct
        placeholders = ",".join("?" * len(matched_item_ids))
        rows = conn.execute(
            f"SELECT item_id, source FROM items WHERE item_id IN ({placeholders})",
            tuple(matched_item_ids),
        ).fetchall()
        item_source_map = {row["item_id"]: row["source"] for row in rows}

        vault_object_id = Path(citation.vault_relpath).stem
        for item_id in matched_item_ids:
            source = item_source_map.get(item_id, "")
            event_id = _event_id(citation.vault_relpath, item_id, event_type)
            if _insert_event(
                conn,
                event_id=event_id,
                item_id=item_id,
                source=source,
                event_type=event_type,
                vault_object_type=citation.object_type,
                vault_object_id=vault_object_id,
                vault_relpath=citation.vault_relpath,
                now_utc=now_utc_str,
            ):
                inserted += 1
                logger.info(
                    "vault_detection: %s cited %s (event=%s)",
                    citation.vault_relpath,
                    item_id,
                    event_type,
                )
            else:
                skipped_dup += 1

    scan_finished = datetime.now(timezone.utc)
    persisted_to: str | None = None
    if persist_last_scan:
        save_last_scan_ts(scan_finished, last_scan_path)
        persisted_to = str(last_scan_path or LAST_SCAN_PATH)

    return ScanResult(
        files_scanned=files_scanned,
        citations_found=citations_found,
        events_inserted=inserted,
        events_skipped_dup=skipped_dup,
        scan_started_at_utc=normalize_dt_to_utc_z(scan_started),
        scan_finished_at_utc=normalize_dt_to_utc_z(scan_finished),
        last_scan_persisted_to=persisted_to,
    )
