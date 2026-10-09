from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from personal_intel_loop import RUNS_DIR, STAGING_DIR
from personal_intel_loop.schemas import FeedbackEvent, compute_feedback_event_id, normalize_dt_to_utc_z
from personal_intel_loop.store import mark_item_status, recompute_source_trust, record_feedback_event

DIGEST_RE = re.compile(r"<!-- PIL_DIGEST date=(?P<date>\d{4}-\d{2}-\d{2}) ranking_version=(?P<ranking>[^ ]+) -->")
ITEM_BLOCK_RE = re.compile(
    r"<!-- PIL_ITEM_START item_id=(?P<item_id>\S+) source=(?P<source>\S+) -->\n(?P<body>.*?)<!-- PIL_ITEM_END -->",
    re.DOTALL,
)
PROCESSED_RE = re.compile(r"<!-- PIL_PROCESSED event_ids=(?P<event_ids>.*?) scanned_at_utc=(?P<scanned_at_utc>.*?) -->")
CHECKBOX_RE = re.compile(
    r"^- \[(?P<checked>[ xX])\].*?<!--\s*pil_action=(?P<action>[a-z_]+)\s*-->$",
    re.MULTILINE,
)


def parse_digest_feedback(md_text: str) -> list[FeedbackEvent]:
    digest_match = DIGEST_RE.search(md_text)
    if not digest_match:
        return []
    digest_date = digest_match.group("date")
    digest_stem = f"intel_loop_digest_{digest_date}"
    now_utc = normalize_dt_to_utc_z(datetime.now(timezone.utc))
    events: list[FeedbackEvent] = []

    for block_match in ITEM_BLOCK_RE.finditer(md_text):
        item_id = block_match.group("item_id")
        body = block_match.group("body")
        processed_match = PROCESSED_RE.search(body)
        processed_ids = set()
        if processed_match:
            raw_ids = processed_match.group("event_ids").strip()
            processed_ids = {part for part in raw_ids.split(",") if part}

        for checkbox_match in CHECKBOX_RE.finditer(body):
            if checkbox_match.group("checked").lower() != "x":
                continue
            action = checkbox_match.group("action")
            event_id = compute_feedback_event_id(
                origin="digest_checkbox",
                item_id=item_id,
                action=action,
                digest_stem=digest_stem,
            )
            if event_id in processed_ids:
                continue
            events.append(
                FeedbackEvent(
                    event_id=event_id,
                    item_id=item_id,
                    action=action,
                    origin="digest_checkbox",
                    event_ts=now_utc,
                )
            )
    return events


def scan_feedback_files(*, staging_dir: Path = STAGING_DIR, digest: Path | None = None) -> list[Path]:
    if digest is not None:
        return [digest]
    return sorted(staging_dir.glob("intel_loop_digest_*.md"))


def _status_for_action(action: str) -> str | None:
    if action in {"promote_to_src", "promote_to_evd"}:
        return "promoted"
    if action in {"keep", "deep_discuss", "useful", "light", "more_like_this"}:
        return "reviewed"
    if action in {"already_known", "unclear", "not_interested", "spam_or_false", "less_like_this"}:
        return "rejected"
    if action == "later":
        return "deferred"
    return None


def append_processed_markers(path: Path, item_event_ids: dict[str, list[str]], *, scanned_at_utc: str) -> None:
    text = path.read_text("utf-8")

    def replace_block(match: re.Match[str]) -> str:
        item_id = match.group("item_id")
        block_text = match.group(0)
        new_ids = item_event_ids.get(item_id)
        if not new_ids:
            return block_text
        processed_match = PROCESSED_RE.search(block_text)
        existing_ids: list[str] = []
        if processed_match:
            existing_ids = [value for value in processed_match.group("event_ids").split(",") if value]
        merged_ids = list(dict.fromkeys(existing_ids + new_ids))
        replacement = f"<!-- PIL_PROCESSED event_ids={','.join(merged_ids)} scanned_at_utc={scanned_at_utc} -->"
        if processed_match:
            return PROCESSED_RE.sub(replacement, block_text, count=1)
        return block_text.replace("<!-- PIL_ITEM_END -->", replacement + "\n<!-- PIL_ITEM_END -->")

    updated = ITEM_BLOCK_RE.sub(replace_block, text)
    if updated != text:
        path.write_text(updated, "utf-8")


def apply_feedback_scan(conn, *, paths: list[Path], dry_run: bool = False) -> dict:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    scanned_at_utc = normalize_dt_to_utc_z(datetime.now(timezone.utc))
    total_candidates = 0
    total_inserted = 0
    item_event_ids_by_path: dict[Path, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    audit_rows: list[dict] = []
    discuss_packets: list[dict] = []

    for path in paths:
        text = path.read_text("utf-8")
        events = parse_digest_feedback(text)
        total_candidates += len(events)
        for event in events:
            event = event.model_copy(update={"digest_path": str(path)})
            inserted = False
            if not dry_run:
                inserted = record_feedback_event(conn, event)
                if inserted:
                    total_inserted += 1
                    status = _status_for_action(event.action)
                    if status:
                        mark_item_status(conn, event.item_id, status)
                    item_event_ids_by_path[path][event.item_id].append(event.event_id)
                    if event.action == "deep_discuss":
                        # Lazy import to keep the feedback module cheap to load
                        from personal_intel_loop.discuss import emit_discuss_packet

                        packet = emit_discuss_packet(conn, item_id=event.item_id, staging_dir=STAGING_DIR)
                        discuss_packets.append(
                            {
                                "item_id": packet.item_id,
                                "path": str(packet.path),
                                "created": packet.created,
                                "error": packet.error,
                            }
                        )
            audit_rows.append(
                {
                    "digest_path": str(path),
                    "event_id": event.event_id,
                    "item_id": event.item_id,
                    "action": event.action,
                    "dry_run": dry_run,
                    "inserted": inserted,
                    "scanned_at_utc": scanned_at_utc,
                }
            )

    if not dry_run:
        for path, event_ids in item_event_ids_by_path.items():
            append_processed_markers(path, event_ids, scanned_at_utc=scanned_at_utc)
        recompute_source_trust(conn, now_utc=scanned_at_utc)

    run_path = RUNS_DIR / f"feedback_scan_{scanned_at_utc.replace(':', '').replace('-', '')}.jsonl"
    run_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in audit_rows) + ("\n" if audit_rows else ""),
        "utf-8",
    )
    return {
        "scanned_files": len(paths),
        "candidate_events": total_candidates,
        "inserted_events": total_inserted,
        "run_path": str(run_path),
        "discuss_packets": discuss_packets,
    }
