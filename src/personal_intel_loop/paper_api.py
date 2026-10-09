"""报纸读接口(纯函数, web.py 调它)。输出结构逐字段照 docs/paper_v2_contract.md。

TASK4 增补(契约 6-10 节): 全部来源 archive、登录态 auth、推荐关注 follow_suggestions、
行程 trips; Item 全键输出 same_day_url/kind/media/novelty/verification/opportunity/settle;
Edition 全键输出 auth_alerts/follow_suggestions 与 counter/warmth/risk/opportunity/settle/
leisure 分区、stats.novelty_mix/est_read_min; 合成条目(digest:/risk:/leisure:)读 digests 表。
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from personal_intel_loop import LOCAL_TZ
from personal_intel_loop.paper_common import (
    MEDIA_ADAPTERS,
    MEDIA_TYPES,
    NOVELTY_KINDS,
    SECTION_ORDER,
    SOCIAL_ADAPTERS,
    SYNTHETIC_PREFIXES,
    adapter_of,
    author_key_from_payload,
    author_score,
    first_media_url,
    item_ai_payload,
    source_label_of,
)
from personal_intel_loop.paper_inbox import source_label_for_inbox
from personal_intel_loop.profile import pending_proposals, resolve_profile_dir, resolve_profile_path
from personal_intel_loop.schemas import normalize_dt_to_utc_z
from personal_intel_loop.fulltext import FULLTEXT_MAX_CHARS
from personal_intel_loop.store import fetch_item

ITEM_KEYS_REQUIRED = (
    "item_id", "title", "url", "source", "source_label", "author_key", "author_label",
    "author_is_byline", "published_at", "lang", "image_url", "lede", "one_liner",
    "backstory", "so_what", "claim", "topic", "style_tags", "why_here", "my", "trust",
)
RATING_DIMS = ("overall", "quality", "author", "style", "topic")
REASON_CODES = ("already_known", "unclear", "not_interested", "keep", "deep_discuss")
REASON_EVENT_ORIGINS = ("digest_checkbox", "web", "cli")
DEFAULT_SOURCE_TRUST = 0.35
EDITIONS_MAX_LIMIT = 3
ADJUSTMENTS_DEFAULT_LIMIT = 50

logger = logging.getLogger(__name__)


def _published_at_local(ts_utc: str) -> str:
    """items.ts 是 UTC Z; 契约要求 ISO 8601 本地时区(东京)。"""
    dt = datetime.fromisoformat(str(ts_utc).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ).isoformat()


def _payload_for(conn: sqlite3.Connection, item_id: str) -> dict:
    return item_ai_payload(conn, item_id) or {}


def _fulltext_image(conn: sqlite3.Connection, item_id: str) -> str | None:
    """RSS 没给图时，用抓全文时记下的文章主图（og:image）。"""
    try:
        row = conn.execute("SELECT image_url FROM item_fulltext WHERE item_id=?", (item_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["image_url"] if row and row["image_url"] else None


def _source_label_for(item_row: sqlite3.Row) -> str:
    try:
        payload = json.loads(item_row["source_payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    label = source_label_of(item_row["source"], payload if isinstance(payload, dict) else None)
    # 10-04 头版：微博/知乎来源名是数字 uid（如 1234567890）→ 显示用博主昵称（items.author）；author_key 仍用 uid 不变
    if adapter_of(item_row["source"]) in SOCIAL_ADAPTERS and label.isdigit():
        try:
            author = item_row["author"]
        except (IndexError, KeyError):
            author = None
        if author:
            return str(author)
    return label


def _my_ratings(conn: sqlite3.Connection, item_id: str) -> dict:
    my = {dim: 0 for dim in RATING_DIMS}
    for row in conn.execute("SELECT dim, value FROM item_ratings WHERE item_id=?", (item_id,)):
        if row["dim"] in my:
            my[row["dim"]] = int(row["value"])
    return my


def _reason_code(conn: sqlite3.Connection, item_id: str) -> str | None:
    row = conn.execute(
        "SELECT event_type FROM promotion_events WHERE item_id=? AND event_type IN ('already_known','unclear','not_interested','keep','deep_discuss') AND origin IN ('digest_checkbox','web','cli') ORDER BY event_ts DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    return row["event_type"] if row else None


def _trust_block(conn: sqlite3.Connection, source: str, author_key: str) -> dict:
    source_row = conn.execute("SELECT trust_score FROM source_trust WHERE source=?", (source,)).fetchone()
    author_row = conn.execute(
        "SELECT n_up, n_down FROM author_trust WHERE author_key=?", (author_key,)
    ).fetchone()
    return {
        "source": float(source_row["trust_score"]) if source_row else DEFAULT_SOURCE_TRUST,
        "author": author_score(int(author_row["n_up"]), int(author_row["n_down"])) if author_row else None,
    }


def _local_date_of_ts(ts_utc: str) -> str:
    return _published_at_local(ts_utc)[:10]


def _same_day_url(source: str, ts_utc: str) -> str:
    """契约 6.2: 单篇页「同一来源当天的其它内容」链接。"""
    return f"/paper/#/archive?source={quote(str(source or ''), safe='')}&date={_local_date_of_ts(ts_utc)}"


def _media_block(item_row: sqlite3.Row, payload: dict) -> dict | None:
    """契约 6.4: 媒体条目(podcast_new/youtube_followed/local_transcripts)的 media 字段。"""
    if adapter_of(item_row["source"]) not in MEDIA_ADAPTERS:
        return None
    duration = payload.get("duration_s")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        duration = payload.get("duration")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        duration = None
    transcript = item_row["transcript"]
    return {
        "type": MEDIA_TYPES.get(adapter_of(item_row["source"]), "audio"),
        "duration_s": int(duration) if duration is not None else None,
        "transcript_chars": len(transcript) if isinstance(transcript, str) and transcript else None,
    }


def _clean_byline(payload: dict) -> str | None:
    """byline 脏值(非字符串, 如数字/对象)按缺失处理, 不让 .strip() 炸上层 (10-05 审计 API-3)。"""
    value = payload.get("byline")
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _item_dict(conn: sqlite3.Connection, item_row: sqlite3.Row, *, section: str | None, blind_reason: str | None) -> dict:
    item_id = item_row["item_id"]
    payload = _payload_for(conn, item_id)
    source_label = _source_label_for(item_row)
    byline = _clean_byline(payload)
    author_key = byline or source_label
    author_label = byline or source_label
    if section == "blind":
        why_here = {"profile_hit": None, "lane": "blind", "blind_reason": blind_reason}
    else:
        why_here = {
            "profile_hit": payload.get("profile_hit"),
            "lane": payload.get("lane"),
            "blind_reason": None,
        }
    my = _my_ratings(conn, item_id)
    my["reason_code"] = _reason_code(conn, item_id)
    try:
        note_row = conn.execute("SELECT text FROM item_notes WHERE item_id=?", (item_id,)).fetchone()
    except sqlite3.OperationalError:
        note_row = None
    my["note"] = note_row["text"] if note_row else None
    claim = payload.get("claim")
    novelty = payload.get("novelty")
    verification = payload.get("verification")
    opportunity = payload.get("opportunity")
    return {
        "item_id": item_id,
        "title": item_row["title"],
        "url": item_row["url"],
        "source": item_row["source"],
        "source_label": source_label,
        "author_key": author_key,
        "author_label": author_label,
        "author_is_byline": byline is not None,
        "published_at": _published_at_local(item_row["ts"]),
        "lang": item_row["lang"],
        "image_url": first_media_url(_safe_json(item_row["source_payload_json"])) or _fulltext_image(conn, item_row["item_id"]),
        "lede": payload.get("lede"),
        "one_liner": payload.get("one_liner") or item_row["title"],
        "backstory": payload.get("backstory"),
        "so_what": payload.get("so_what"),
        "claim": dict(claim) if isinstance(claim, dict) and claim.get("text") else None,
        "topic": payload.get("topic"),
        "style_tags": list(payload.get("style_tags") or []),
        "same_day_url": _same_day_url(item_row["source"], item_row["ts"]),
        "kind": "media" if adapter_of(item_row["source"]) in MEDIA_ADAPTERS else "article",
        "media": _media_block(item_row, payload),
        "novelty": dict(novelty) if isinstance(novelty, dict) else None,
        "verification": dict(verification) if isinstance(verification, dict) else None,
        "opportunity": dict(opportunity) if isinstance(opportunity, dict) else None,
        "settle": None,
        "why_here": why_here,
        "my": my,
        "trust": _trust_block(conn, item_row["source"], author_key),
    }


def _safe_json(text: str | None) -> dict:
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _synthetic_payload(conn: sqlite3.Connection, item_id: str) -> dict | None:
    """digest:/risk:/leisure: 条目从 digests 表取完整 Item payload。"""
    row = conn.execute("SELECT payload_json FROM digests WHERE digest_id=?", (item_id,)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _edition_item(conn: sqlite3.Connection, edition_row: sqlite3.Row) -> dict:
    item_id = edition_row["item_id"]
    if item_id.startswith(SYNTHETIC_PREFIXES):
        payload = _synthetic_payload(conn, item_id)
        if payload is None:
            raise KeyError(f"unknown synthetic item: {item_id}")
        out = dict(payload)
        out["item_id"] = item_id
        return out
    item_row = fetch_item(conn, item_id)
    if item_row is None:
        raise KeyError(f"unknown item_id: {item_id}")
    return _item_dict(
        conn,
        item_row,
        section=edition_row["section"],
        blind_reason=edition_row["blind_reason"],
    )


def _settle_block(conn: sqlite3.Connection, item_id: str) -> dict | None:
    from personal_intel_loop.paper_settle import entry_block

    return entry_block(conn, item_id)


def _parse_proposal_line(line: str) -> dict:
    header, sep, rest = line.partition("]")
    parsed_date = None
    header_text = header.lstrip("- [").strip()
    for part in header_text.split("·"):
        part = part.strip()
        try:
            parsed_date = date.fromisoformat(part).isoformat()
            break
        except ValueError:
            continue
    basis = target = suggestion = None
    for segment in rest.split(" → "):
        segment = segment.strip()
        if segment.startswith("依据:"):
            basis = segment[len("依据:"):].strip() or None
        elif segment.startswith("目标:"):
            target = segment[len("目标:"):].strip() or None
        elif segment.startswith("建议:"):
            suggestion = segment[len("建议:"):].strip() or None
    return {"line": line, "date": parsed_date, "basis": basis, "target": target, "suggestion": suggestion}


def _local_day_bounds_utc(day_iso: str) -> tuple[str, str]:
    """东京本地日 [当天 00:00, 次日 00:00) 对应的 UTC Z 区间(inbox.created_at 是 UTC Z)。"""
    day_start = datetime.fromisoformat(day_iso).replace(tzinfo=LOCAL_TZ)
    return (
        normalize_dt_to_utc_z(day_start),
        normalize_dt_to_utc_z(day_start + timedelta(days=1)),
    )


def _inbox_dict(row: sqlite3.Row) -> dict:
    """契约第 5 节 InboxItem; created_at 按本机时区 ISO 输出(与 published_at 同口径)。"""
    return {
        "inbox_id": int(row["inbox_id"]),
        "source": row["source"],
        "source_label": source_label_for_inbox(row["source"]),
        "title": row["title"],
        "body": row["body"],
        "url": row["url"],
        "priority": row["priority"],
        "created_at": _published_at_local(row["created_at"]),
        "read": row["read_at"] is not None,
    }


def _inbox_items_for_date(conn: sqlite3.Connection, edition_date: str) -> list[dict]:
    """该期日期(东京本地日)收到的全部条目: normal/low + urgent 全部(不论已读未读), 按投递时间升序。"""
    start_utc, end_utc = _local_day_bounds_utc(edition_date)
    rows = conn.execute(
        "SELECT * FROM inbox WHERE created_at >= ? AND created_at < ? ORDER BY created_at ASC, inbox_id ASC",
        (start_utc, end_utc),
    ).fetchall()
    return [_inbox_dict(row) for row in rows]


def _follow_suggestions_for_edition(conn: sqlite3.Connection, edition_date: str) -> list[dict]:
    """契约 6.3: 周一(东京)那期带「推荐关注」小栏, 只放 status=new 的。"""
    try:
        if date.fromisoformat(edition_date).weekday() != 0:
            return []
    except ValueError:
        return []
    return get_follow_suggestions(conn, status="new")["suggestions"]


def _novelty_mix(sections: dict) -> dict:
    counts = {kind: 0 for kind in NOVELTY_KINDS}
    for section in ("lead", "top", "counter", "briefs"):
        for item in sections.get(section, []):
            kind = (item.get("novelty") or {}).get("kind")
            if kind in counts:
                counts[kind] += 1
    return counts


def _est_read_min(sections: dict) -> int:
    """每期页尾「预计阅读 N 分钟」: 按各条导语+简讯字数 ÷ 400 字/分钟, 不足 1 分钟记 1。"""
    chars = 0
    for name, entries in sections.items():
        if name == "inbox":
            continue
        for item in entries:
            text = item.get("lede") or item.get("one_liner") or item.get("title") or ""
            chars += len(text)
    if chars <= 0:
        return 0
    return max(1, math.ceil(chars / 400))


def _edition_dict(conn: sqlite3.Connection, edition_date: str, *, with_letters: bool, profile_path) -> dict:
    rows = conn.execute(
        "SELECT item_id, section, rank, blind_reason, built_at FROM editions WHERE edition_date=? ORDER BY rank",
        (edition_date,),
    ).fetchall()
    if not rows:
        raise KeyError(f"unknown edition: {edition_date}")
    sections = {name: [] for name in SECTION_ORDER}
    for row in rows:
        section = row["section"]
        if section not in sections:
            continue
        try:
            item = _edition_item(conn, row)
        except KeyError:
            # 10-05 审计 API-2: 脏引用(如 digests 行已被保留期裁剪)只跳过该条, 不放大成整期拿不到
            logger.warning("edition %s: unknown item %s, skipped", edition_date, row["item_id"])
            continue
        if section == "settle" and item.get("settle") is None:
            item["settle"] = _settle_block(conn, row["item_id"])
        sections[section].append(item)
    sections["inbox"] = _inbox_items_for_date(conn, edition_date)
    # flash = 当天 urgent 且未读(报头上方红色快讯横条)
    flash = [entry for entry in sections["inbox"] if entry["priority"] == "urgent" and not entry["read"]]
    # 契约第 9 节: risk 分区 = 行前风险简报 + 当天 urgent 投递里 source 以 pil.alerts 开头的条目
    sections["risk"] = sections["risk"] + [
        entry for entry in sections["inbox"] if entry["priority"] == "urgent" and str(entry["source"]).startswith("pil.alerts")
    ]
    picked_ids = [row["item_id"] for row in rows]
    # 10-04 首期：原口径是 items 全表（14 万），报头「自 N 条里挑的」失真 → 改为该期日期前 36 小时内入库的条数
    _day_end = datetime.combine(date.fromisoformat(edition_date) + timedelta(days=1), datetime.min.time(), tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    _day_start = _day_end - timedelta(hours=36)
    pool = int(conn.execute(
        "SELECT COUNT(*) FROM items WHERE first_ingested_at >= ? AND first_ingested_at < ?",
        (_day_start.strftime("%Y-%m-%dT%H:%M:%SZ"), _day_end.strftime("%Y-%m-%dT%H:%M:%SZ")),
    ).fetchone()[0])
    ai_done = 0
    for item_id in picked_ids:
        ai_row = conn.execute("SELECT error FROM item_ai WHERE item_id=?", (item_id,)).fetchone()
        if ai_row is not None and ai_row["error"] is None:
            ai_done += 1
    letters = []
    if with_letters:
        letters = [_parse_proposal_line(line) for line in pending_proposals(profile_path)]
    note_row = conn.execute("SELECT reading_note FROM edition_notes WHERE edition_date=?", (edition_date,)).fetchone()

    from personal_intel_loop.paper_auth import auth_alerts

    return {
        "date": edition_date,
        "built_at": rows[0]["built_at"],
        "reading_note": (note_row["reading_note"] if note_row else None) or None,
        "sections": sections,
        "flash": flash,
        "letters": letters,
        "auth_alerts": auth_alerts(conn),
        "follow_suggestions": _follow_suggestions_for_edition(conn, edition_date),
        "stats": {
            "pool": pool,
            "picked": len(picked_ids),
            "ai_done": ai_done,
            "novelty_mix": _novelty_mix(sections),
            "est_read_min": _est_read_min(sections),
        },
    }


def get_editions(conn: sqlite3.Connection, *, before: str | None = None, limit: int = 1, profile_path=None) -> dict:
    """{"editions": [Edition 按日期倒序], "next_before": 本次最旧一期日期或 null}。"""
    if before is None or str(before).strip() == "":
        before = (datetime.now(LOCAL_TZ).date() + timedelta(days=1)).isoformat()
    before = date.fromisoformat(str(before)).isoformat()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError(f"unsupported limit: {limit!r}") from None
    limit = max(1, min(limit, EDITIONS_MAX_LIMIT))
    if profile_path is None:
        profile_path = resolve_profile_path()
    latest = conn.execute("SELECT MAX(edition_date) AS latest FROM editions").fetchone()["latest"]
    date_rows = conn.execute(
        "SELECT DISTINCT edition_date FROM editions WHERE edition_date < ? ORDER BY edition_date DESC LIMIT ?",
        (before, limit),
    ).fetchall()
    editions = [
        _edition_dict(conn, row["edition_date"], with_letters=(row["edition_date"] == latest), profile_path=profile_path)
        for row in date_rows
    ]
    next_before = None
    if editions:
        oldest = editions[-1]["date"]
        has_older = conn.execute(
            "SELECT 1 FROM editions WHERE edition_date < ? LIMIT 1", (oldest,)
        ).fetchone()
        next_before = oldest if has_older else None
    return {"editions": editions, "next_before": next_before}


def get_item(conn: sqlite3.Connection, item_id: str) -> dict:
    """Item 加 fulltext 与 edition_date; digest:/risk:/leisure: 读 digests 表(digest 附 members);
    settle 分区条目带 settle 块。未知 item 抛 KeyError(web 层转 404)。"""
    if item_id.startswith(SYNTHETIC_PREFIXES):
        row = conn.execute("SELECT payload_json, edition_date, member_ids_json FROM digests WHERE digest_id=?", (item_id,)).fetchone()
        payload = _synthetic_payload(conn, item_id)
        if row is None or payload is None:
            raise KeyError(f"unknown item_id: {item_id}")
        out = dict(payload)
        out["item_id"] = item_id
        out["fulltext"] = None
        out["fulltext_source"] = None
        out["edition_date"] = row["edition_date"]
        if item_id.startswith("digest:"):
            try:
                member_ids = json.loads(row["member_ids_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                member_ids = []
            members = []
            for member_id in member_ids if isinstance(member_ids, list) else []:
                member_row = fetch_item(conn, str(member_id))
                if member_row is not None:
                    members.append(_item_dict(conn, member_row, section=None, blind_reason=None))
            out["members"] = members
        return out

    item_row = fetch_item(conn, item_id)
    if item_row is None:
        raise KeyError(f"unknown item_id: {item_id}")
    edition_row = conn.execute(
        "SELECT section, blind_reason, edition_date FROM editions WHERE item_id=? ORDER BY edition_date DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    out = _item_dict(
        conn,
        item_row,
        section=edition_row["section"] if edition_row else None,
        blind_reason=edition_row["blind_reason"] if edition_row else None,
    )
    if edition_row is not None and edition_row["section"] == "settle":
        out["settle"] = _settle_block(conn, item_id)
    fulltext_row = conn.execute("SELECT text FROM item_fulltext WHERE item_id=?", (item_id,)).fetchone()
    out["fulltext"] = (fulltext_row["text"] if fulltext_row else None) or None
    out["fulltext_source"] = "fetched" if out["fulltext"] else None
    # 原文抓不到时回落到采集时带回的正文（公众号/知乎/播客转录本身就是全文；
    # 新闻 RSS 可能只是摘要，前端据 fulltext_source 标注）
    if not out["fulltext"]:
        body = str(item_row["body"] or "").strip()
        if body:
            out["fulltext"] = body[:FULLTEXT_MAX_CHARS]
            out["fulltext_source"] = "feed"
    out["edition_date"] = edition_row["edition_date"] if edition_row else None
    return out


# 10-05 验收 C1：采集健康（把运行记录里的 status/errors/连续 0 条显示到管道页）
RUN_RECORD_GLOB = "ingest_*.jsonl"
RUN_FILES_SCANNED = 500
ZERO_STREAKS_FILE = "adapter_zero_streaks.json"
STALE_AFTER = timedelta(hours=48)
ZERO_STREAK_MIN = 3


def _resolve_runs_dir(runs_dir=None):
    if runs_dir is not None:
        return Path(runs_dir)
    from personal_intel_loop import RUNS_DIR
    return RUNS_DIR


def _parse_run_at(value) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _record_int(record: dict, *keys) -> int:
    for key in keys:
        value = record.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


def _record_errors(record: dict) -> list[str]:
    errors = record.get("errors")
    if isinstance(errors, list):
        return [str(err) for err in errors if str(err or "").strip()]
    single = record.get("error")  # first_ring 整只适配器抛异常时写的是 error: str
    return [str(single)] if single else []


def _iter_run_records(payload: dict):
    """单适配器记录直接给；first_ring 记录里每个适配器条目一条（整轮时间在外层）。"""
    adapter = payload.get("adapter")
    if adapter:
        yield str(adapter), payload
        return
    outer_run_at = payload.get("run_at_utc")
    for nested in payload.get("adapters") or []:
        if isinstance(nested, dict) and nested.get("adapter"):
            if outer_run_at and not nested.get("run_at_utc"):
                nested = dict(nested, run_at_utc=outer_run_at)
            yield str(nested["adapter"]), nested


def _run_file_stamp(name: str) -> str:
    """文件名里的时间段：ingest_20261005T104914.903134Z.jsonl / ingest_first_ring_2026….jsonl。
    直接按文件名排会让 first_ring 前缀恒排在后面，把真正最新的记录挤出 500 个的窗口。
    """
    stem = name[len(RUN_RECORD_GLOB.split("*")[0]):]
    for prefix in ("first_ring_",):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
    return stem


def _latest_run_records(runs_path: Path) -> dict[str, dict]:
    """每个采集器最近一次运行记录，按 run_at_utc 取最新（同刻取文件名靠后的）。"""
    try:
        paths = sorted(runs_path.glob(RUN_RECORD_GLOB), key=lambda p: _run_file_stamp(p.name))[-RUN_FILES_SCANNED:]
    except OSError:
        return {}
    latest: dict[str, tuple[str, str, dict]] = {}
    for path in paths:
        try:
            text = path.read_text("utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("pipeline health: bad run record in %s", path.name)
                continue
            if not isinstance(payload, dict):
                continue
            run_at = str(payload.get("run_at_utc") or "")
            for adapter, record in _iter_run_records(payload):
                key = (run_at, path.name)
                current = latest.get(adapter)
                if current is None or key >= (current[0], current[1]):
                    latest[adapter] = (run_at, path.name, record)
    return {adapter: value[2] for adapter, value in latest.items()}


def _zero_streak_updated(runs_path: Path) -> dict[str, datetime]:
    try:
        payload = json.loads((runs_path / ZERO_STREAKS_FILE).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, datetime] = {}
    for adapter, info in payload.items():
        if isinstance(info, dict):
            dt = _parse_run_at(info.get("updated_at_utc"))
            if dt is not None:
                out[str(adapter)] = dt
    return out


def _zero_streaks(runs_path: Path) -> dict[str, int]:
    try:
        payload = json.loads((runs_path / ZERO_STREAKS_FILE).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}  # 文件不存在当 0
    if not isinstance(payload, dict):
        return {}
    out: dict[str, int] = {}
    for adapter, info in payload.items():
        value = info.get("zero_streak") if isinstance(info, dict) else info
        try:
            out[str(adapter)] = int(value or 0)
        except (TypeError, ValueError):
            out[str(adapter)] = 0
    return out


def collect_pipeline_health(runs_dir=None, *, now=None) -> list[dict]:
    """每个采集器取最近一次运行记录，只留下「需要注意」的：status 非 ok、有 errors、
    连续 0 条 >= 3 轮、或最近一次运行早于 48 小时（stale）。全部正常时返回空列表。
    """
    runs_path = _resolve_runs_dir(runs_dir)
    now_dt = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    from personal_intel_loop.cli import SUPPORTED_ADAPTER_NAMES, ZERO_IS_NORMAL

    streaks = _zero_streaks(runs_path)
    streak_seen = _zero_streak_updated(runs_path)
    health = []
    for adapter, record in sorted(_latest_run_records(runs_path).items()):
        if adapter not in SUPPORTED_ADAPTER_NAMES:
            continue  # 已退役的采集器（旧运行记录还在）不报
        run_at = _parse_run_at(record.get("run_at_utc"))
        # 有的采集器（如灾害预警）走 alerts 链路不写 ingest 记录，但每轮会更新连续 0 条 state
        seen_at = streak_seen.get(adapter)
        if seen_at is not None and (run_at is None or seen_at > run_at):
            run_at = seen_at
        errors = _record_errors(record)
        status = str(record.get("status") or "ok")  # 旧记录没有 status 键 → 视为 ok
        zero_streak = streaks.get(adapter, 0)
        stale = run_at is None or now_dt - run_at > STALE_AFTER
        streak_alarm = zero_streak >= ZERO_STREAK_MIN and adapter not in ZERO_IS_NORMAL
        if status == "ok" and not errors and not streak_alarm and not stale:
            continue
        health.append(
            {
                "adapter": adapter,
                "status": status,
                "count": _record_int(record, "count", "collected"),
                "errors": errors,
                "run_at_utc": str(record.get("run_at_utc") or ""),
                "zero_streak": zero_streak,
                "stale": bool(stale),
            }
        )
    return health


def get_pipeline(conn: sqlite3.Connection, *, profile_dir=None, runs_dir=None, now=None) -> dict:
    """管道页: sources 按 trust 降序, authors 按 n_up+n_down 降序, recent 最近 50 条。

    health(10-05 验收 C1): 各采集器最近一次运行里「需要注意」的那些，见 collect_pipeline_health。
    """
    cutoff_dt = datetime.now(LOCAL_TZ) - timedelta(days=30)
    cutoff_iso = normalize_dt_to_utc_z(cutoff_dt)
    cutoff_date = cutoff_dt.date().isoformat()

    sources = []
    for row in conn.execute("SELECT source, trust_score FROM source_trust ORDER BY trust_score DESC, source ASC"):
        source = row["source"]
        override = conn.execute(
            "SELECT muted, boosted FROM source_overrides WHERE source=?", (source,)
        ).fetchone()
        items_30d = int(
            conn.execute(
                "SELECT COUNT(*) FROM items WHERE source=? AND first_ingested_at >= ?", (source, cutoff_iso)
            ).fetchone()[0]
        )
        on_paper_30d = int(
            conn.execute(
                "SELECT COUNT(DISTINCT e.item_id) FROM editions e JOIN items i ON i.item_id=e.item_id WHERE i.source=? AND e.edition_date >= ?",
                (source, cutoff_date),
            ).fetchone()[0]
        )
        label_row = conn.execute(
            "SELECT source_payload_json FROM items WHERE source=? AND source_payload_json IS NOT NULL ORDER BY first_ingested_at DESC LIMIT 1",
            (source,),
        ).fetchone()
        label = source_label_of(source, _safe_json(label_row["source_payload_json"]) if label_row else None)
        sources.append(
            {
                "source": source,
                "label": label,
                "trust": float(row["trust_score"]),
                "items_30d": items_30d,
                "on_paper_30d": on_paper_30d,
                "muted": bool(override["muted"]) if override else False,
                "boosted": bool(override["boosted"]) if override else False,
            }
        )

    authors = []
    for row in conn.execute(
        "SELECT author_key, label, n_up, n_down, muted FROM author_trust ORDER BY (n_up + n_down) DESC, author_key ASC"
    ):
        authors.append(
            {
                "author_key": row["author_key"],
                "label": row["label"],
                "trust": author_score(int(row["n_up"]), int(row["n_down"])),
                "n_up": int(row["n_up"]),
                "n_down": int(row["n_down"]),
                "muted": bool(row["muted"]),
            }
        )

    recent = []
    for row in conn.execute(
        "SELECT r.ts, r.item_id, r.dim, r.value, i.title FROM item_ratings r LEFT JOIN items i ON i.item_id=r.item_id ORDER BY r.ts DESC LIMIT 50"
    ):
        recent.append(
            {
                "ts": row["ts"],
                "item_id": row["item_id"],
                "title": row["title"] or "",
                "dim": row["dim"],
                "value": int(row["value"]),
            }
        )

    return {
        "sources": sources,
        "authors": authors,
        "recent": recent,
        "profile_dir": str(profile_dir if profile_dir is not None else resolve_profile_dir()),
        "health": collect_pipeline_health(runs_dir, now=now),  # 10-05 验收 C1
    }


def set_pipeline(conn: sqlite3.Connection, payload: dict, *, now_utc: str) -> dict:
    """source: mute/unmute/boost/unboost/reset 写 source_overrides; author: mute/unmute/reset 写 author_trust。

    返回 before/after(source 为 trust_score, 这些动作不改 trust_score, before=after; reset 作者计数会变)。
    """
    kind = str(payload.get("kind") or "")
    key = str(payload.get("key") or "").strip()
    action = str(payload.get("action") or "")
    if kind not in ("source", "author"):
        raise ValueError(f"unsupported pipeline kind: {kind!r}")
    if not key:
        raise ValueError("key is required")
    # 10-05 验收 H204：前端「取消加权」发 unboost，白名单补上；语义只撤销加权、不动屏蔽
    if action not in ("mute", "unmute", "boost", "unboost", "reset"):
        raise ValueError(f"unsupported pipeline action: {action!r}")

    if kind == "source":
        trust_row = conn.execute("SELECT trust_score FROM source_trust WHERE source=?", (key,)).fetchone()
        item_row = conn.execute("SELECT 1 FROM items WHERE source=? LIMIT 1", (key,)).fetchone()
        if trust_row is None and item_row is None:
            raise KeyError(f"unknown source: {key}")
        before = float(trust_row["trust_score"]) if trust_row else DEFAULT_SOURCE_TRUST
        override = conn.execute("SELECT muted, boosted FROM source_overrides WHERE source=?", (key,)).fetchone()
        muted = int(override["muted"]) if override else 0
        boosted = int(override["boosted"]) if override else 0
        if action == "mute":
            muted = 1
        elif action == "unmute":
            muted = 0
        elif action == "boost":
            boosted = 1
        elif action == "unboost":  # 10-05 验收 H204：只撤加权，muted 保持
            boosted = 0
        else:
            muted = 0
            boosted = 0
        conn.execute(
            "INSERT OR REPLACE INTO source_overrides (source, muted, boosted, updated_at) VALUES (?, ?, ?, ?)",
            (key, muted, boosted, normalize_dt_to_utc_z(now_utc)),
        )
        conn.commit()
        return {
            "ok": True,
            "kind": "source",
            "key": key,
            "action": action,
            "before": before,
            "after": before,
            "muted": bool(muted),
        }

    # kind == "author"
    if action in ("boost", "unboost"):  # 10-05 验收 H204：author 与 boost 同样不支持 unboost
        raise ValueError("author does not support boost/unboost")
    author_row = conn.execute("SELECT n_up, n_down, muted FROM author_trust WHERE author_key=?", (key,)).fetchone()
    if author_row is None:
        raise KeyError(f"unknown author: {key}")
    n_up = int(author_row["n_up"])
    n_down = int(author_row["n_down"])
    muted = int(author_row["muted"])
    before = author_score(n_up, n_down)
    if action == "mute":
        muted = 1
    elif action == "unmute":
        muted = 0
    else:
        muted = 0
        n_up = 0
        n_down = 0
    conn.execute(
        "UPDATE author_trust SET n_up=?, n_down=?, muted=?, updated_at=? WHERE author_key=?",
        (n_up, n_down, muted, normalize_dt_to_utc_z(now_utc), key),
    )
    conn.commit()
    return {
        "ok": True,
        "kind": "author",
        "key": key,
        "action": action,
        "before": before,
        "after": author_score(n_up, n_down),
        "muted": bool(muted),
    }


def list_adjustments(conn: sqlite3.Connection, *, limit: int = ADJUSTMENTS_DEFAULT_LIMIT) -> dict:
    """AI 调整记录(管道页), 新的在前。键集合照契约第 4 节。"""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError(f"unsupported limit: {limit!r}") from None
    limit = max(1, limit)
    adjustments = [
        {
            "id": int(row["id"]),
            "ts": row["ts"],
            "kind": row["kind"],
            "key": row["key"],
            "label": row["label"],
            "before": float(row["before"]),
            "after": float(row["after"]),
            "reason": row["reason"],
            "reverted": bool(row["reverted"]),
        }
        for row in conn.execute("SELECT id, ts, for_date, kind, key, label, delta, before, after, reason, reverted, model FROM ai_adjustments ORDER BY id DESC LIMIT ?", (limit,))
    ]
    return {"adjustments": adjustments}


def revert_adjustment(conn: sqlite3.Connection, adjustment_id, *, now_utc: str) -> dict:
    """撤销一条 AI 调整: 把该条 delta 从 knob_offsets 里减回去, 标 reverted=1。

    返回 {"ok", "key", "before", "after"}(before/after 为撤销前后该旋钮的有效值)。
    不存在的 id → KeyError(404); 已撤销的再撤 → ValueError(400)。
    """
    try:
        adjustment_id = int(adjustment_id)
    except (TypeError, ValueError):
        raise ValueError(f"unsupported adjustment id: {adjustment_id!r}") from None
    row = conn.execute(
        "SELECT id, kind, key, delta, reverted FROM ai_adjustments WHERE id=?",
        (adjustment_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown adjustment id: {adjustment_id}")
    if int(row["reverted"]):
        raise ValueError(f"adjustment {adjustment_id} already reverted")
    kind, key, delta = row["kind"], row["key"], float(row["delta"])

    from personal_intel_loop.paper_learn import base_value, knob_offsets

    base = base_value(conn, kind, key)
    current = knob_offsets(conn, kind).get(key, 0.0)
    before_eff = _clamp_effective(base + current)
    after_eff = _clamp_effective(base + current - delta)
    with conn:
        conn.execute("UPDATE ai_adjustments SET reverted=1 WHERE id=?", (adjustment_id,))
        conn.execute(
            "INSERT OR REPLACE INTO knob_offsets (kind, key, offset, updated_at) VALUES (?, ?, ?, ?)",
            (kind, key, current - delta, normalize_dt_to_utc_z(now_utc)),
        )
    conn.commit()
    return {"ok": True, "key": key, "before": before_eff, "after": after_eff}


def _clamp_effective(value: float) -> float:
    """有效值夹在 [0.05, 0.95](与 paper_learn 一致)。"""
    return min(0.95, max(0.05, value))


# --- App 推送(契约第 5 节 GET /api/paper/notifications) ---


def _parse_since(value) -> datetime:
    """since 缺省/空 = 最早(全量返回); 无时区按 UTC; 解析不了抛 ValueError(web 层转 400)。"""
    if value is None or str(value).strip() == "":
        return datetime.min.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _headline(conn: sqlite3.Connection, edition_date: str) -> str:
    row = conn.execute(
        "SELECT i.title FROM editions e JOIN items i ON i.item_id=e.item_id WHERE e.edition_date=? AND e.section='lead' ORDER BY e.rank LIMIT 1",
        (edition_date,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT i.title FROM editions e JOIN items i ON i.item_id=e.item_id WHERE e.edition_date=? ORDER BY e.rank LIMIT 1",
            (edition_date,),
        ).fetchone()
    return str(row["title"]) if row else ""


def _edition_number(conn: sqlite3.Connection, edition_date: str) -> int:
    """第 N 期 = 截至该日(含)的期数。"""
    return int(
        conn.execute("SELECT COUNT(DISTINCT edition_date) FROM editions WHERE edition_date <= ?", (edition_date,)).fetchone()[0]
    )


def _auth_notifications(conn: sqlite3.Connection, since_dt: datetime) -> list[tuple[datetime, str, dict]]:
    """契约 6.1: 登录失效通知。同一失效只推一次(id 含 since_failing, 恢复后再失效换新 id)。"""
    from personal_intel_loop.paper_auth import default_registry

    labels = {str(c.get("key")): str(c.get("label") or c.get("key")) for c in default_registry()}
    hints = {str(c.get("key")): str(c.get("relogin_hint") or "") for c in default_registry()}
    out: list[tuple[datetime, str, dict]] = []
    for row in conn.execute("SELECT key, detail, since_failing FROM auth_status WHERE ok=0 AND since_failing IS NOT NULL"):
        try:
            failing_dt = datetime.fromisoformat(str(row["since_failing"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if failing_dt.tzinfo is None:
            failing_dt = failing_dt.replace(tzinfo=timezone.utc)
        if failing_dt <= since_dt:
            continue
        key = row["key"]
        out.append(
            (
                failing_dt,
                f"auth:{key}:{row['since_failing']}",
                {
                    "id": f"auth:{key}:{row['since_failing']}",
                    "title": f"{labels.get(key, key)} 登录已失效",
                    "body": row["detail"] or hints.get(key, ""),
                    "open_path": "/paper/#/auth",
                },
            )
        )
    return out


def notifications(conn: sqlite3.Connection, *, since=None, now_utc: str | None = None) -> dict:
    """App 每 60 秒拉一次, 用上次的 now 作 since。

    三类: 新一期出版(editions.built_at > since, 每个 edition_date 只出一条, 标题「今日 · 第 N 期」,
    正文 = 头条标题)、urgent 投递(inbox.created_at > since)、登录失效(auth_status.since_failing >
    since, 同一失效只推一次)。按发生时间升序返回。
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    now_z = normalize_dt_to_utc_z(now_utc)
    since_dt = _parse_since(since)

    entries: list[tuple[datetime, str, dict]] = []
    for row in conn.execute(
        "SELECT edition_date, MAX(built_at) AS built_at FROM editions GROUP BY edition_date"
    ).fetchall():
        built_dt = datetime.fromisoformat(str(row["built_at"]).replace("Z", "+00:00"))
        if built_dt.tzinfo is None:
            built_dt = built_dt.replace(tzinfo=timezone.utc)
        if built_dt <= since_dt:
            continue
        edition_date = row["edition_date"]
        entries.append(
            (
                built_dt,
                f"edition:{edition_date}",
                {
                    "id": f"edition:{edition_date}",
                    "title": f"今日 · 第 {_edition_number(conn, edition_date)} 期",
                    "body": _headline(conn, edition_date),
                    "open_path": "/paper/#/",
                },
            )
        )
    since_z = normalize_dt_to_utc_z(since_dt)
    for row in conn.execute(
        "SELECT * FROM inbox WHERE priority='urgent' AND created_at > ? ORDER BY created_at ASC, inbox_id ASC",
        (since_z,),
    ).fetchall():
        created_dt = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        # 与同函数 built_dt/failing_dt 一致, 无时区历史行按 UTC 兜底, 避免 sort 混比抛 TypeError
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        entries.append(
            (
                created_dt,
                f"inbox:{row['inbox_id']}",
                {
                    "id": f"inbox:{row['inbox_id']}",
                    "title": row["title"],
                    "body": row["body"],
                    "open_path": f"/paper/#/inbox/{row['inbox_id']}",
                },
            )
        )
    entries.extend(_auth_notifications(conn, since_dt))
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    return {"now": now_z, "notifications": [payload for _ts, _id, payload in entries]}


# --- 全部来源(契约 6.2 GET /api/paper/archive) ---

ARCHIVE_DEFAULT_LIMIT = 50
ARCHIVE_MAX_LIMIT = 200


def _archive_item(conn: sqlite3.Connection, item_row: sqlite3.Row) -> dict:
    payload = _payload_for(conn, item_row["item_id"])
    source_label = _source_label_for(item_row)
    byline = _clean_byline(payload)
    on_paper = conn.execute(
        "SELECT MAX(edition_date) AS latest FROM editions WHERE item_id=?", (item_row["item_id"],)
    ).fetchone()["latest"]
    return {
        "item_id": item_row["item_id"],
        "title": item_row["title"],
        "url": item_row["url"],
        "source": item_row["source"],
        "source_label": source_label,
        "author_label": byline or source_label,
        "published_at": _published_at_local(item_row["ts"]),
        "one_liner": payload.get("one_liner") or item_row["title"],
        "on_paper": on_paper,
    }


def get_archive(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
    date_local: str | None = None,
    q: str | None = None,
    before: str | None = None,
    limit: int = ARCHIVE_DEFAULT_LIMIT,
) -> dict:
    """全部来源(契约 6.2)。q 走 items_fts(标题+正文); before 为翻页游标(get_archive
    返回的 next_before 原样回传, 形如 "<UTC Z ts>|<item_id>")。"""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError(f"unsupported limit: {limit!r}") from None
    limit = max(1, min(limit, ARCHIVE_MAX_LIMIT))

    where_parts: list[str] = []
    params: list = []
    from_items_only = True
    if q is not None and str(q).strip():
        from personal_intel_loop.store import fts_match_query

        match_expr = fts_match_query(str(q).strip())
        if not match_expr:
            raise ValueError(f"bad archive query: {q!r}")
        where_parts.append("items_fts MATCH ?")
        params.append(match_expr)
        from_items_only = False
    if source:
        where_parts.append("i.source = ?")
        params.append(str(source))
    if date_local:
        date.fromisoformat(str(date_local))  # 格式不对 → ValueError(400)
        start_utc, end_utc = _local_day_bounds_utc(str(date_local))
        where_parts.append("i.ts >= ?")
        where_parts.append("i.ts < ?")
        params.extend([start_utc, end_utc])
    if before:
        text = str(before).strip()
        if "|" in text:
            ts_part, id_part = text.split("|", 1)
            try:
                datetime.fromisoformat(ts_part.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"bad archive cursor: {before!r}") from None
            # ts_part 须归一到 Z 格式再与库内恒为 Z 格式的 items.ts 做字符串比较
            ts_part = normalize_dt_to_utc_z(ts_part)
            where_parts.append("(i.ts < ? OR (i.ts = ? AND i.item_id < ?))")
            params.extend([ts_part, ts_part, id_part])
        else:
            try:
                datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"bad archive cursor: {before!r}") from None
            where_parts.append("i.ts < ?")
            params.append(normalize_dt_to_utc_z(text))
    where = " AND ".join(where_parts) if where_parts else "1=1"
    table = "items i" if from_items_only else "items i JOIN items_fts f ON f.item_id = i.item_id"
    sql = f"SELECT i.* FROM {table} WHERE {where} ORDER BY i.ts DESC, i.item_id DESC LIMIT ?"
    try:
        rows = conn.execute(sql, [*params, limit]).fetchall()
    except sqlite3.OperationalError as exc:
        raise ValueError(f"bad archive query: {exc}") from None
    items = [_archive_item(conn, row) for row in rows]
    next_before = None
    if len(rows) == limit:
        last = rows[-1]
        next_before = f"{last['ts']}|{last['item_id']}"
    return {"items": items, "next_before": next_before}


def get_archive_sources(conn: sqlite3.Connection, *, days: int = 7, now_utc: str | None = None) -> dict:
    """每个来源近 N 天(缺省 7)条数与最近一条时间, 用于来源列表。"""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    cutoff = normalize_dt_to_utc_z(_parse_since(now_utc) - timedelta(days=max(0, int(days))))
    label_cache: dict[str, str] = {}

    def _label(source: str) -> str:
        if source not in label_cache:
            row = conn.execute(
                "SELECT source_payload_json FROM items WHERE source=? AND source_payload_json IS NOT NULL ORDER BY first_ingested_at DESC LIMIT 1",
                (source,),
            ).fetchone()
            label_cache[source] = source_label_of(source, _safe_json(row["source_payload_json"]) if row else None)
        return label_cache[source]

    sources = []
    for row in conn.execute(
        "SELECT source, SUM(CASE WHEN first_ingested_at >= ? THEN 1 ELSE 0 END) AS items_7d, MAX(ts) AS latest_at FROM items GROUP BY source ORDER BY items_7d DESC, source ASC",
        (cutoff,),
    ):
        sources.append(
            {
                "source": row["source"],
                "source_label": _label(row["source"]),
                "items_7d": int(row["items_7d"]),
                "latest_at": _published_at_local(row["latest_at"]) if row["latest_at"] else None,
            }
        )
    return {"sources": sources}


# --- 推荐关注(契约 6.3 GET /api/paper/follow_suggestions) ---


def get_follow_suggestions(conn: sqlite3.Connection, *, status: str | None = None) -> dict:
    sql = "SELECT * FROM follow_suggestions"
    params: list = []
    if status:
        if status not in ("new", "followed", "dismissed"):
            raise ValueError(f"unsupported status: {status!r}")
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id ASC"
    suggestions = []
    for row in conn.execute(sql, params):
        try:
            evidence = json.loads(row["evidence_json"] or "[]")
        except json.JSONDecodeError:
            evidence = []
        suggestions.append(
            {
                "id": row["id"],
                "platform": row["platform"],
                "account_id": row["account_id"],
                "label": row["label"],
                "url": row["url"],
                "reason": row["reason"],
                "evidence": [entry for entry in evidence if isinstance(entry, dict)],
                "status": row["status"],
            }
        )
    return {"suggestions": suggestions}


def decide_follow_suggestion(conn: sqlite3.Connection, payload: dict, *, now_utc: str) -> dict:
    """POST /api/paper/follow_suggestions/decide {"id","decision":"dismiss"}。"""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    suggestion_id = str(payload.get("id") or "").strip()
    decision = str(payload.get("decision") or "")
    if not suggestion_id:
        raise ValueError("id is required")
    if decision != "dismiss":
        raise ValueError(f"unsupported decision: {decision!r}(followed 由系统自动判定)")
    cursor = conn.execute(
        "UPDATE follow_suggestions SET status='dismissed', updated_at=? WHERE id=?",
        (normalize_dt_to_utc_z(now_utc), suggestion_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        raise KeyError(f"unknown follow suggestion: {suggestion_id}")
    return {"ok": True, "id": suggestion_id, "status": "dismissed"}


# --- 行程(契约 9 GET/POST /api/paper/trips) ---


def _trip_dict(row: sqlite3.Row) -> dict:
    return {
        "trip_id": row["trip_id"],
        "place": row["place"],
        "country": row["country"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "note": row["note"],
        "created_at": row["created_at"],
    }


def get_trips(conn: sqlite3.Connection) -> dict:
    return {"trips": [_trip_dict(row) for row in conn.execute("SELECT * FROM trips ORDER BY start_date ASC, trip_id ASC")]}


def create_trip(conn: sqlite3.Connection, payload: dict, *, now_utc: str) -> dict:
    import uuid

    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    place = payload.get("place")
    if not isinstance(place, str) or not place.strip():
        raise ValueError("place is required")
    start_date = payload.get("start_date")
    if not isinstance(start_date, str):
        raise ValueError("start_date is required")
    start = date.fromisoformat(start_date)  # 格式不对 → ValueError(400)
    end_date = payload.get("end_date")
    end = None
    if end_date is not None:
        end = date.fromisoformat(str(end_date))
        if end < start:
            raise ValueError("end_date must be on or after start_date")
    trip_id = f"trip:{uuid.uuid4().hex[:12]}"
    created_at = normalize_dt_to_utc_z(now_utc)
    conn.execute(
        "INSERT INTO trips (trip_id, place, country, start_date, end_date, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            trip_id,
            place.strip()[:120],
            (str(payload.get("country")).strip()[:80] if isinstance(payload.get("country"), str) and payload.get("country").strip() else None) or None,
            start.isoformat(),
            end.isoformat() if end else None,
            (str(payload.get("note")).strip()[:500] if isinstance(payload.get("note"), str) and payload.get("note").strip() else None) or None,
            created_at,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM trips WHERE trip_id=?", (trip_id,)).fetchone()
    return {"ok": True, "trip": _trip_dict(row)}


def delete_trip(conn: sqlite3.Connection, payload: dict, *, now_utc: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    trip_id = str(payload.get("trip_id") or "").strip()
    if not trip_id:
        raise ValueError("trip_id is required")
    cursor = conn.execute("DELETE FROM trips WHERE trip_id=?", (trip_id,))
    conn.commit()
    if cursor.rowcount == 0:
        raise KeyError(f"unknown trip: {trip_id}")
    return {"ok": True}
