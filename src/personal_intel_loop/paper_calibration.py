"""每周校准(契约第 7.3 节 + 第 10.4 节周栏统计)。

- 分歧样例: 用户打了 already_known 而 AI 判为新知(new_fact/new_mechanism/counter)的条目,
  预处理提示词从这里取最近 10 条。
- 印证占比: 每周统计「用户点开的条目里 known+confirming 占比」, 连续两周上升则在编辑部
  来信里自动写一封「你最近在读越来越多印证自己的东西」并附数字(确定性模板, 不调模型)。
- 周栏统计: 各分区近 7 天 impression/打开率/阅读时长写一封来信; 连续 4 周点开率 <5% 的
  分区写「建议砍掉/合并」提案。

全部走 profile.append_proposal(= 最新一期的 letters), 前缀含周一日期天然幂等。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from personal_intel_loop import LOCAL_TZ
from personal_intel_loop.paper_behavior import local_date_of_ts
from personal_intel_loop.paper_common import NOVELTY_NEW_KINDS, NOVELTY_OLD_KINDS
from personal_intel_loop.schemas import normalize_dt_to_utc_z

# 统计/写信的最小样本量, 避免拿几条行为数据就下结论
MIN_OPENED_FOR_SHARE = 5
MIN_SECTION_IMPRESSIONS = 5
SUNSET_OPEN_RATE = 0.05
SUNSET_WEEKS = 4
TRACKED_SECTIONS = ("lead", "top", "counter", "briefs", "blind", "warmth", "opportunity", "settle", "leisure")
READING_WPM = 400

_REASON_SQL = "('already_known','unclear','not_interested','keep','deep_discuss')"
_ORIGINS_SQL = "('digest_checkbox','web','cli')"


def disagreement_samples(conn: sqlite3.Connection, *, limit: int = 10) -> list[dict]:
    """「早知道」原因码 vs AI 判新 的分歧样例(最近在前), 喂预处理提示词。"""
    samples = []
    for row in conn.execute(
        f"SELECT event_ts, item_id FROM promotion_events WHERE event_type='already_known' AND origin IN {_ORIGINS_SQL} ORDER BY event_ts DESC LIMIT ?",
        (limit * 3,),
    ):
        ai_row = conn.execute("SELECT payload_json FROM item_ai WHERE item_id=?", (row["item_id"],)).fetchone()
        if ai_row is None or not ai_row["payload_json"]:
            continue
        try:
            payload = json.loads(ai_row["payload_json"])
        except json.JSONDecodeError:
            continue
        novelty = payload.get("novelty") if isinstance(payload, dict) else None
        if not isinstance(novelty, dict) or novelty.get("kind") not in NOVELTY_NEW_KINDS:
            continue
        title_row = conn.execute("SELECT title FROM items WHERE item_id=?", (row["item_id"],)).fetchone()
        event_date = local_date_of_ts(row["event_ts"])
        samples.append(
            {
                "item_id": row["item_id"],
                "title": (title_row["title"] if title_row else "") or "",
                "ai_kind": novelty["kind"],
                "date": event_date.isoformat() if event_date else None,
                "line": f"{(title_row['title'] if title_row else '') or '(无标题)'} — AI 判 {novelty['kind']}({novelty.get('why') or '无说明'}), 你说早知道",
            }
        )
        if len(samples) >= limit:
            break
    return samples


def _week_bounds(week_start: date) -> tuple[str, str]:
    start_dt = datetime.combine(week_start, datetime.min.time()).replace(tzinfo=LOCAL_TZ)
    return (
        normalize_dt_to_utc_z(start_dt),
        normalize_dt_to_utc_z(start_dt + timedelta(days=7)),
    )


def opened_novelty_mix(conn: sqlite3.Connection, *, week_start: date) -> dict:
    """某一周(周一起 7 天)用户点开的条目里各 novelty.kind 的条数(不含盲区/温暖)。

    点开 = 当周有 open_item 事件; 条目须上过版面且有 item_ai novelty。blind/warmth
    不计入(新颖性对它们无意义/不受行为学习影响)。
    """
    start_z, end_z = _week_bounds(week_start)
    counts = {kind: 0 for kind in ("new_fact", "new_mechanism", "counter", "known", "confirming")}
    opened_rows = conn.execute(
        "SELECT DISTINCT item_id FROM ui_events WHERE kind='open_item' AND ts >= ? AND ts < ? AND item_id IS NOT NULL",
        (start_z, end_z),
    ).fetchall()
    for row in opened_rows:
        placement = conn.execute(
            "SELECT section FROM editions WHERE item_id=? ORDER BY edition_date DESC LIMIT 1",
            (row["item_id"],),
        ).fetchone()
        if placement is None or placement["section"] in ("blind", "warmth"):
            continue
        ai_row = conn.execute("SELECT payload_json FROM item_ai WHERE item_id=?", (row["item_id"],)).fetchone()
        if ai_row is None or not ai_row["payload_json"]:
            continue
        try:
            payload = json.loads(ai_row["payload_json"])
        except json.JSONDecodeError:
            continue
        novelty = payload.get("novelty") if isinstance(payload, dict) else None
        if isinstance(novelty, dict) and novelty.get("kind") in counts:
            counts[novelty["kind"]] += 1
    return counts


def _confirming_share(counts: dict) -> tuple[float, int]:
    total = sum(counts.values())
    old = sum(counts[kind] for kind in NOVELTY_OLD_KINDS)
    return (old / total if total else 0.0), total


def calibration_letter_line(monday: date, share_new: float, share_prev: float, counts_new: dict, counts_prev: dict) -> str:
    def _pct(value: float) -> str:
        return f"{value * 100:.0f}%"

    basis = (
        f"上周你点开的条目里印证性的(known+confirming)占 {_pct(share_new)}"
        f"({counts_new['known']}+{counts_new['confirming']}/{sum(counts_new.values())}),"
        f" 前一周是 {_pct(share_prev)}({counts_prev['known']}+{counts_prev['confirming']}/{sum(counts_prev.values())}), 连续两周上升"
    )
    target = "保持「告诉我不知道的」是一等目标"
    suggestion = "本周起反方/新知栏优先读; 若连续三周仍上升, 编辑部会把主线里 known/confirming 的配额再往下压"
    return f"[{monday.isoformat()} · calibration · 印证占比] 依据: {basis} → 目标: {target} → 建议: {suggestion}"


def section_stats(conn: sqlite3.Connection, *, week_start: date, days: int = 7) -> dict[str, dict]:
    """各分区某周(impression 条数 / 打开数 / 点开率 / 阅读时长 ms)。"""
    start_z, end_z = _week_bounds(week_start)
    end_date = (week_start + timedelta(days=days)).isoformat()
    stats: dict[str, dict] = {}
    for section in TRACKED_SECTIONS:
        placed = {
            row["item_id"]
            for row in conn.execute(
                "SELECT item_id FROM editions WHERE section=? AND edition_date >= ? AND edition_date < ?",
                (section, week_start.isoformat(), end_date),
            )
        }
        if not placed:
            stats[section] = {"shown": 0, "impressions": 0, "opened": 0, "open_rate": None, "read_ms": 0}
            continue
        impressions = opened = 0
        read_ms = 0
        for item_id in placed:
            rows = conn.execute(
                "SELECT kind, ms FROM ui_events WHERE item_id=? AND ts >= ? AND ts < ?",
                (item_id, start_z, end_z),
            ).fetchall()
            if any(row["kind"] == "impression" for row in rows):
                impressions += 1
            if any(row["kind"] == "open_item" for row in rows):
                opened += 1
            read_ms += sum(int(row["ms"] or 0) for row in rows if row["kind"] == "item_dwell")
        stats[section] = {
            "shown": len(placed),
            "impressions": impressions,
            "opened": opened,
            "open_rate": (opened / impressions) if impressions else None,
            "read_ms": read_ms,
        }
    return stats


def _append_once(line: str, profile_path) -> bool:
    """同前缀(同周一)的来信只写一次。"""
    from personal_intel_loop.profile import append_proposal, pending_proposals

    prefix = line.split("] ", 1)[0] + "]"
    for existing in pending_proposals(profile_path):
        if existing.startswith(prefix):
            return False
    append_proposal(line, profile_path)
    return True


def weekly_letters(
    conn: sqlite3.Connection,
    *,
    monday: date,
    now_utc=None,
    profile_path=None,
) -> dict:
    """周一出版时跑: 印证占比来信(连续两周上升才写) + 各栏周统计来信 + 砍栏提案。

    返回 {"calibration": bool, "sections": bool, "sunsets": [section...]}。
    """
    from personal_intel_loop.profile import resolve_profile_path

    path = profile_path if profile_path is not None else resolve_profile_path()
    written = {"calibration": False, "sections": False, "sunsets": []}

    # 1) 印证占比: 上一个完整周 vs 再前一周
    this_week = monday - timedelta(days=7)
    prev_week = monday - timedelta(days=14)
    counts_new = opened_novelty_mix(conn, week_start=this_week)
    counts_prev = opened_novelty_mix(conn, week_start=prev_week)
    share_new, total_new = _confirming_share(counts_new)
    share_prev, total_prev = _confirming_share(counts_prev)
    if total_new >= MIN_OPENED_FOR_SHARE and total_prev >= MIN_OPENED_FOR_SHARE and share_new > share_prev:
        written["calibration"] = _append_once(calibration_letter_line(monday, share_new, share_prev, counts_new, counts_prev), path)

    # 2) 周栏统计来信(近 7 天有行为才写)
    stats = section_stats(conn, week_start=this_week)
    lines = []
    for section, entry in stats.items():
        if entry["shown"] == 0:
            continue
        rate = f"{entry['open_rate'] * 100:.0f}%" if entry["open_rate"] is not None else "—"
        lines.append(f"{section}: 曝光 {entry['impressions']} 条, 打开 {entry['opened']} ({rate}), 读 {entry['read_ms'] // 60000} 分 {entry['read_ms'] % 60000 // 1000} 秒")
    if lines:
        basis = "; ".join(lines)
        letter = (
            f"[{monday.isoformat()} · sections · 各栏阅读统计(近7天)] 依据: {basis}"
            f" → 目标: 让每栏都值得占版面 → 建议: 打开率低的栏优先看反方/新知栏的替代来源"
        )
        written["sections"] = _append_once(letter, path)

    # 3) 砍栏提案: 连续 4 周点开率 <5%(每周曝光 ≥ 最小样本)
    for section in TRACKED_SECTIONS:
        qualifying = True
        details = []
        for offset in range(SUNSET_WEEKS):
            week = monday - timedelta(days=7 * (offset + 1))
            entry = section_stats(conn, week_start=week).get(section)
            if entry is None or entry["impressions"] < MIN_SECTION_IMPRESSIONS or entry["open_rate"] is None or entry["open_rate"] >= SUNSET_OPEN_RATE:
                qualifying = False
                break
            details.append(f"{week.isoformat()} 周 {entry['open_rate'] * 100:.1f}%")
        if qualifying:
            line = (
                f"[{monday.isoformat()} · sunset · {section}] 依据: 连续 {SUNSET_WEEKS} 周点开率 <5%({'; '.join(details)})"
                f" → 目标: {section} 栏 → 建议: 建议砍掉/合并 {section} 栏, 条目并入全部来源"
            )
            if _append_once(line, path):
                written["sunsets"].append(section)
    return written
