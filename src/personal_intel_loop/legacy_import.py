from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from personal_intel_loop.schemas import (
    FEEDBACK_EVENT_WEIGHTS,
    FeedbackEvent,
    normalize_dt_to_utc_z,
    sha1_hex,
)
from personal_intel_loop.store import record_feedback_event

LEGACY_ORIGIN = "weibo_legacy_import"


@dataclass
class LegacyImportResult:
    scanned_rows: int
    inserted_rows: int
    skipped_missing_item: int
    skipped_unsupported_action: int
    skipped_bad_rows: int


def _event_id(post_id: str, action: str, logged_at_utc: str) -> str:
    return sha1_hex(f"weibo_legacy|{post_id}|{action}|{logged_at_utc}")


def _coerce_logged_at_utc(raw: object) -> str | None:
    if not raw:
        return None
    try:
        return normalize_dt_to_utc_z(str(raw))
    except Exception:
        return None


def import_weibo_feedback_log(
    conn: sqlite3.Connection,
    *,
    feedback_log_path: Path,
) -> LegacyImportResult:
    scanned = inserted = missing_item = unsupported = bad = 0

    with feedback_log_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            scanned += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue

            action = str(row.get("action") or "").strip()
            if action not in FEEDBACK_EVENT_WEIGHTS:
                unsupported += 1
                continue

            uid = str(row.get("uid") or "").strip()
            post_id = str(row.get("post_id") or "").strip()
            if not uid or not post_id:
                bad += 1
                continue

            item_id = f"weibo:{uid}:{post_id}"
            exists = conn.execute(
                "SELECT 1 FROM items WHERE item_id=?",
                (item_id,),
            ).fetchone()
            if exists is None:
                missing_item += 1
                continue

            logged_at_utc = _coerce_logged_at_utc(row.get("logged_at"))
            if logged_at_utc is None:
                logged_at_utc = normalize_dt_to_utc_z(datetime.now(timezone.utc))

            event = FeedbackEvent(
                event_id=_event_id(post_id, action, logged_at_utc),
                item_id=item_id,
                action=action,
                origin=LEGACY_ORIGIN,
                note=str(row.get("note") or "") or None,
                event_ts=logged_at_utc,
            )
            if record_feedback_event(conn, event):
                inserted += 1

    return LegacyImportResult(
        scanned_rows=scanned,
        inserted_rows=inserted,
        skipped_missing_item=missing_item,
        skipped_unsupported_action=unsupported,
        skipped_bad_rows=bad,
    )
