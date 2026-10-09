"""行为汇总(契约第 4 节; 确定性代码, 不调模型)。

把某本地日期的 ui_events 按条目汇总裁决五种标签(deep_read/read/glanced/skipped/unseen),
再按 source / author_key / topic 三个维度聚合各标签条数。AI 学习(paper_learn)在此基础上
做 7.3 护栏过滤后喂模型。

标签裁决顺序(契约只给典型定义, 组合边界按此顺序兜底, 见 DELIVERY2「没把握」):
  1. deep_read: opened_original 或 dwell_ratio ≥ 0.7
  2. read:      0.2 ≤ dwell_ratio < 0.7
  3. glanced:   打开但 dwell_ratio < 0.2
  4. unseen:    impression_ms < 1500
  5. skipped:   其余(曝光 ≥1500 未打开; 有显式反馈也落在这, 显式反馈单独随条目带出)
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from personal_intel_loop import LOCAL_TZ
from personal_intel_loop.paper_common import author_key_from_payload, item_ai_payload, source_label_of

READING_WPM = 400  # 字/分钟
MIN_EXPECTED_READ_MS = 30_000
SKIPPED_IMPRESSION_MS = 1500
LABELS = ("deep_read", "read", "glanced", "skipped", "unseen")

# 全字面量 SQL(不用 format/f-string 拼, IN 列表直接写死)
_REASON_CODES_SQL = "('already_known','unclear','not_interested','keep','deep_discuss')"
_REASON_ORIGINS_SQL = "('digest_checkbox','web','cli')"


def as_local_date(value) -> date:
    """date / ISO 字符串 → date。"""
    if isinstance(value, datetime):
        return value.astimezone(LOCAL_TZ).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def local_date_of_ts(ts: str) -> date | None:
    """事件时间戳 → 东京本地日期; 带时区按其换算, 无时区按本地时区。解析失败返回 None。"""
    try:
        dt = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone(LOCAL_TZ).date()


def _utc_window_for_local_date(target: date) -> tuple[str, str]:
    """本地日期 → ui_events.ts 的 UTC 字符串半开区间 [start, end)。

    10-05 验收 P03: 写入侧(paper_events._clean_event, F18)把 ts 归一为 UTC「Z」原文,
    字符串序即时间序。边界取本地零点对应的 UTC 整秒且不带时区后缀 —— 与「…秒.fffZ」
    的小数秒前缀比较方向不变, 跨午夜边界上带小数秒的事件不会错桶。
    """
    end_date = target + timedelta(days=1)
    start_utc = datetime(target.year, target.month, target.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    end_utc = datetime(end_date.year, end_date.month, end_date.day, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    return start_utc.strftime("%Y-%m-%dT%H:%M:%S"), end_utc.strftime("%Y-%m-%dT%H:%M:%S")


def day_event_rows(conn: sqlite3.Connection, date_local) -> list[dict]:
    """该本地日期的全部 ui_events。

    10-05 验收 P03: 改前全表取回后在 Python 按 local_date_of_ts 逐行丢弃(ui_events 只增不删,
    32 万行全扫); 现把本地日期换算成 UTC 区间下推到 SQL, 走 idx_ui_events_ts。取回的行仍用
    local_date_of_ts 复核, 解析失败或他日的行与改前一样丢弃(等价依赖写入侧 UTC 归一不变式)。
    """
    target = as_local_date(date_local)
    start_ts, end_ts = _utc_window_for_local_date(target)
    out = []
    for row in conn.execute("SELECT id, session_id, ts, kind, item_id, edition_date, ms, meta_json FROM ui_events WHERE ts >= ? AND ts < ? ORDER BY id", (start_ts, end_ts)):
        event_date = local_date_of_ts(row["ts"])
        if event_date == target:
            item = dict(row)
            item["local_date"] = event_date.isoformat()
            out.append(item)
    return out


def _edition_placement(conn: sqlite3.Connection, item_id: str, date_iso: str, event_dates: list[str]) -> tuple[str | None, int | None]:
    """section/rank 取自 editions: 先按当期, 再按事件自带的 edition_date, 最后按该条最近一期。"""
    row = conn.execute("SELECT section, rank FROM editions WHERE edition_date=? AND item_id=?", (date_iso, item_id)).fetchone()
    if row is not None:
        return row["section"], int(row["rank"])
    for edition_date in event_dates:
        if not edition_date:
            continue
        row = conn.execute("SELECT section, rank FROM editions WHERE edition_date=? AND item_id=?", (edition_date, item_id)).fetchone()
        if row is not None:
            return row["section"], int(row["rank"])
    row = conn.execute("SELECT section, rank FROM editions WHERE item_id=? AND edition_date<=? ORDER BY edition_date DESC LIMIT 1", (item_id, date_iso)).fetchone()
    if row is not None:
        return row["section"], int(row["rank"])
    return None, None


def _source_label_for(conn: sqlite3.Connection, item_row: sqlite3.Row) -> str:
    try:
        payload = json.loads(item_row["source_payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    return source_label_of(item_row["source"], payload if isinstance(payload, dict) else None)


def _expected_read_ms(conn: sqlite3.Connection, item_id: str, body: str | None) -> int:
    """正文长度 ÷ 400 字/分钟; 全文优先, 无全文按 body; 最少 30 秒。"""
    fulltext_row = conn.execute("SELECT text FROM item_fulltext WHERE item_id=? AND status='ok'", (item_id,)).fetchone()
    text = (fulltext_row["text"] if fulltext_row and fulltext_row["text"] else body) or ""
    chars = len(text)
    return max(MIN_EXPECTED_READ_MS, chars * 60_000 // READING_WPM)


def _ratings_for(conn: sqlite3.Connection, item_id: str) -> tuple[dict[str, int], str | None]:
    ratings = {
        row["dim"]: int(row["value"])
        for row in conn.execute("SELECT dim, value FROM item_ratings WHERE item_id=?", (item_id,))
        if row["dim"] in ("overall", "quality", "author", "style", "topic")
    }
    row = conn.execute(
        f"SELECT event_type FROM promotion_events WHERE item_id=? AND event_type IN {_REASON_CODES_SQL} AND origin IN {_REASON_ORIGINS_SQL} ORDER BY event_ts DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    return ratings, (row["event_type"] if row else None)


def _decide_label(*, opened: bool, opened_original: bool, impression_ms: int, dwell_ratio: float) -> str:
    if opened_original or dwell_ratio >= 0.7:
        return "deep_read"
    if dwell_ratio >= 0.2:
        return "read"
    if opened:
        return "glanced"
    if impression_ms < SKIPPED_IMPRESSION_MS:
        return "unseen"
    return "skipped"


def _aggregate_by(items: list[dict], key_field: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for item in items:
        key = item.get(key_field)
        if not key:
            continue
        counts = out.setdefault(key, {})
        counts[item["label"]] = counts.get(item["label"], 0) + 1
    return out


def summarize_day(conn: sqlite3.Connection, *, date_local) -> dict:
    """该本地日期的行为汇总: {"date", "session_ms", "items", "by_source", "by_author", "by_topic"}。"""
    date_obj = as_local_date(date_local)
    date_iso = date_obj.isoformat()
    rows = day_event_rows(conn, date_obj)

    session_ms = sum(int(r["ms"] or 0) for r in rows if r["kind"] == "session_end")

    per_item: dict[str, dict[str, Any]] = {}
    for row in rows:
        item_id = row["item_id"]
        if not item_id:
            continue
        state = per_item.setdefault(
            item_id,
            {
                "impressions": 0,
                "impression_ms": 0,
                "opened": False,
                "dwell_ms": 0,
                "max_scroll_pct": None,
                "opened_original": False,
                "edition_dates": [],
            },
        )
        ms = row["ms"]
        if row["kind"] == "impression":
            state["impressions"] += 1
            state["impression_ms"] += int(ms or 0)
        elif row["kind"] == "open_item":
            state["opened"] = True
        elif row["kind"] == "item_dwell":
            state["dwell_ms"] += int(ms or 0)
            try:
                meta = json.loads(row["meta_json"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            scroll = meta.get("max_scroll_pct") if isinstance(meta, dict) else None
            if isinstance(scroll, (int, float)) and not isinstance(scroll, bool):
                if state["max_scroll_pct"] is None or scroll > state["max_scroll_pct"]:
                    state["max_scroll_pct"] = scroll
        elif row["kind"] == "open_original":
            state["opened_original"] = True
        if row["edition_date"]:
            state["edition_dates"].append(row["edition_date"])

    items = []
    for item_id, state in per_item.items():
        item_row = conn.execute(
            "SELECT item_id, source, title, body, source_payload_json FROM items WHERE item_id=?", (item_id,)
        ).fetchone()
        if item_row is None:
            continue  # 库里没有的 item 没法归一来源/作者, 留在 ui_events 里但不进汇总
        payload = item_ai_payload(conn, item_id) or {}
        source_label = _source_label_for(conn, item_row)
        section, rank = _edition_placement(conn, item_id, date_iso, state["edition_dates"])
        expected_read_ms = _expected_read_ms(conn, item_id, item_row["body"])
        dwell_ratio = round(state["dwell_ms"] / expected_read_ms, 4) if expected_read_ms else 0.0
        ratings, reason_code = _ratings_for(conn, item_id)
        items.append(
            {
                "item_id": item_id,
                "title": item_row["title"],
                "source": item_row["source"],
                "source_label": source_label,
                "author_key": author_key_from_payload(payload, source_label),
                "topic": payload.get("topic"),
                "style_tags": list(payload.get("style_tags") or []),
                "section": section,
                "rank": rank,
                "impressions": state["impressions"],
                "impression_ms": state["impression_ms"],
                "opened": state["opened"],
                "dwell_ms": state["dwell_ms"],
                "max_scroll_pct": state["max_scroll_pct"],
                "opened_original": state["opened_original"],
                "expected_read_ms": expected_read_ms,
                "dwell_ratio": dwell_ratio,
                "ratings": ratings,
                "reason_code": reason_code,
                "label": _decide_label(
                    opened=state["opened"],
                    opened_original=state["opened_original"],
                    impression_ms=state["impression_ms"],
                    dwell_ratio=dwell_ratio,
                ),
            }
        )

    return {
        "date": date_iso,
        "session_ms": session_ms,
        "items": items,
        "by_source": _aggregate_by(items, "source"),
        "by_author": _aggregate_by(items, "author_key"),
        "by_topic": _aggregate_by(items, "topic"),
    }


def next_day(date_local) -> date:
    return as_local_date(date_local) + timedelta(days=1)
