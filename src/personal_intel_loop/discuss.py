"""Deep-discuss context packet generator.

When a digest item's `pil_action=deep_discuss` checkbox is checked, the feedback
scanner calls `emit_discuss_packet` which writes

    <PIL_STAGING_DIR>/discuss_{item_id_safe}_{date}.md

with everything an AI session needs to start the conversation: the full item
body, top-K vault neighbors from the shared Qdrant collection, currently-active
JDG/MON/DEC refs, and a launch-prompt template. The user hands the file path (or
the file contents) to an AI assistant to continue the conversation.

The packet is idempotent per (item_id, date): re-running scan on the same
checked checkbox overwrites the same file, so drift-free.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from personal_intel_loop import STAGING_DIR

logger = logging.getLogger(__name__)

TOP_VAULT_NEIGHBORS = 5
BODY_EXCERPT_LIMIT = 4000
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
_ITEM_BLOCK_RE = re.compile(
    r"<!-- PIL_ITEM_START item_id=(?P<item_id>\S+)\s.*?-->(?P<body>.*?)<!-- PIL_ITEM_END -->",
    re.S,
)
_WHY_RE = re.compile(
    r"^- why_for_you:\s*\[(?P<route>[^\]]+)\](?:\s+(?P<target_id>\S+)\s+@\s+(?P<score>[0-9.]+))?",
    re.M,
)
_NOV_RE = re.compile(r"^- novelty:\s*(?P<v>[0-9.]+)", re.M)
_ACT_RE = re.compile(r"^- active_relevance:\s*(?P<v>[0-9.]+)", re.M)


@dataclass
class DiscussPacketResult:
    item_id: str
    path: Path
    created: bool
    error: str | None = None


def _safe_item_id(item_id: str) -> str:
    return _SAFE_ID_RE.sub("_", item_id).strip("_") or "item"


def _load_item_row(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT i.item_id, i.source, i.url, i.title, i.body, i.author, i.ts, i.summary,
               i.tags_json, i.source_payload_json, i.embedding
        FROM items AS i
        WHERE i.item_id = ?
        """,
        (item_id,),
    ).fetchone()


def _render_header(row: sqlite3.Row, date_iso: str) -> list[str]:
    return [
        f"<!-- PIL_DISCUSS_CONTEXT item_id={row['item_id']} generated_at={date_iso} -->",
        f"# 讨论上下文:{row['title']}",
        "",
        "> 用法:把这个文件路径或全文交给 AI 助手,",
        "> 说 \"讨论这条\"。讨论到位后让 AI 写 SRC/EVD/JDG draft 到 staging/。",
        "",
        "## 原文元数据",
        "",
        f"- item_id: `{row['item_id']}`",
        f"- source: `{row['source']}`",
        f"- author: {row['author'] or '—'}",
        f"- ts_utc: {row['ts']}",
        f"- url: <{row['url']}>",
    ]


def _render_payload_block(row: sqlite3.Row) -> list[str]:
    raw = row["source_payload_json"] or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    if not payload:
        return []
    pretty = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return ["", "## source_payload", "", "```json", pretty, "```"]


def _render_body_block(row: sqlite3.Row) -> list[str]:
    body = row["body"] or ""
    if not body:
        return ["", "## 原文正文", "", "_(正文为空)_"]
    excerpt = body[:BODY_EXCERPT_LIMIT]
    tail = "" if len(body) == len(excerpt) else "\n\n…(正文被截断到 4000 字,完整内容在 sqlite items 表)"
    return ["", "## 原文正文", "", excerpt + tail]


def _vault_neighbor_state(row: sqlite3.Row) -> tuple[list, list[str]]:
    embedding_blob = row["embedding"]
    if not embedding_blob:
        return [], ["", "## vault 最近邻", "", "_(item 还未嵌入 — 请先跑一次 v2 digest 让 embedding 写入 sqlite)_"]
    try:
        import numpy as np

        from personal_intel_loop import vault_corpus

        vec = np.frombuffer(embedding_blob, dtype="float32")
        hits = vault_corpus.query_top_k(vec, k=TOP_VAULT_NEIGHBORS)
    except Exception as exc:
        logger.warning("vault neighbor query failed for %s: %s", row["item_id"], exc)
        return [], ["", "## vault 最近邻", "", f"_(查询失败: {exc})_"]
    if not hits:
        return [], ["", "## vault 最近邻", "", "_(无命中)_"]
    lines = ["", "## vault 最近邻(Qdrant top-5,越接近 1 越相关)", ""]
    for hit in hits:
        title = hit.title or hit.source or "?"
        ot = hit.object_type or "-"
        preview = (hit.text or "").replace("\n", " ").strip()[:200]
        lines.append(f"- **{hit.score:.3f}** `[{ot}]` [[{title}]] — {preview}")
    return hits, lines


def _cell(text: str) -> str:
    s = (text or "").replace("\n", " ").replace("|", "\\|").strip()
    return s if s else "—"


def _fnum(match: re.Match | None, key: str) -> float | None:
    if not match or match.group(key) is None:
        return None
    try:
        return float(match.group(key))
    except ValueError:
        return None


def _parse_digest_item_signals(text: str, item_id: str) -> dict | None:
    for match in _ITEM_BLOCK_RE.finditer(text):
        if match.group("item_id") != item_id:
            continue
        body = match.group("body")
        why = _WHY_RE.search(body)
        out: dict = {}
        if why and why.group("target_id"):
            out["target_id"] = why.group("target_id")
            out["route"] = why.group("route")
            score = _fnum(why, "score")
            if score is not None:
                out["why_score"] = score
        novelty, active = _fnum(_NOV_RE.search(body), "v"), _fnum(_ACT_RE.search(body), "v")
        if novelty is not None:
            out["novelty"] = novelty
        if active is not None:
            out["active_relevance"] = active
        return out
    return None


def _latest_digest_signals(conn: sqlite3.Connection, item_id: str) -> dict:
    try:
        rows = conn.execute(
            "SELECT digest_path FROM digest_inclusions WHERE item_id=? ORDER BY digest_date DESC, included_at_utc DESC",
            (item_id,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    for row in rows:
        path = Path(row["digest_path"])
        try:
            text = path.read_text("utf-8")
        except OSError:
            continue
        try:
            parsed = _parse_digest_item_signals(text, item_id)
        except Exception:
            continue
        if parsed is not None:
            return parsed
    return {}


def _hit_id(hit) -> str:
    return (getattr(hit, "note_id", None) or getattr(hit, "title", None) or "").strip()


def _render_chunk_draft(row: sqlite3.Row, hits: list, signals: dict) -> list[str]:
    item_id = row["item_id"]
    chunk_id = f"pil:{item_id}:1"
    title, body, url, source = row["title"] or "", row["body"] or "", row["url"] or "", row["source"] or ""
    claim = f"{title}（{body.strip()[:80]}）" if body.strip() else title
    anchor = f"{url} {body.strip()[:30]}".strip() or "—"
    target = (signals.get("target_id") or "").strip()
    host = target or (_hit_id(hits[0]) if hits else "") or "UNMOUNTABLE"
    if target:
        bits = [signals[k] for k in ("why_score", "route") if k in signals]
        deriv_tail = " ".join(str(b) for b in bits) if bits else "—"
    elif hits:
        deriv_tail = f"{hits[0].score} chroma_top1"
    else:
        deriv_tail = "—"
    novelty, active = signals.get("novelty"), signals.get("active_relevance")
    disp = "instance-fire" if novelty is not None and active is not None and novelty < 0.5 and active > 0.35 else "undetermined"
    f1_ids = [hid for hid in (_hit_id(h) for h in hits[:TOP_VAULT_NEIGHBORS]) if hid]
    probe = f"f1: {_cell(','.join(f1_ids) if f1_ids else '—')} \\| f2: （待换 framing 重查）"
    um = f"UNMOUNTABLE: 1；top-3: {chunk_id}" if host == "UNMOUNTABLE" else f"UNMOUNTABLE: 0；已挂 {host}"
    return [
        "",
        "## 候选表征块草稿",
        "",
        "| id | claim | anchor | provenance | source_stance | probe | host | derivability | disposition | edges | conflict | 三件套 | flip | cost_to_verify |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        (
            f"| {_cell(chunk_id)} | {_cell(claim)} | {_cell(anchor)} | reported | "
            f"{_cell(f'derivative（来源 {source}，激励偏向未判）')} | {probe} | {_cell(host)} | "
            f"undetermined：{_cell(deriv_tail)} | {disp} | — | — | — | — | — |"
        ),
        "",
        um,
    ]


def _render_active_layer() -> list[str]:
    try:
        from personal_intel_loop import active_corpus

        refs = active_corpus.build_active_references()
    except Exception as exc:
        logger.warning("active refs load failed: %s", exc)
        return ["", "## 当前 active 层对象", "", f"_(加载失败: {exc})_"]
    if not refs:
        return ["", "## 当前 active 层对象", "", "_(当前 active 索引为空)_"]
    lines = ["", "## 当前 active 层对象(可能受影响)", ""]
    for ref in refs:
        head = (ref.text or "").replace("\n", " ").strip()[:180]
        lines.append(f"- `[{ref.object_type}]` [[{ref.object_id}]] — {head}")
    return lines


def _render_launch_prompt(row: sqlite3.Row) -> list[str]:
    return [
        "",
        "## 启动 prompt(可改)",
        "",
        "```",
        "下面是一条我想深入讨论的候选信息。请按以下步骤:",
        "",
        "1) 判断它是否对上面列出的 active JDG/MON/DEC 产生冲击,说明理由",
        "2) 抽出可作为 EVD 的具体命题(claim + 我需要多高置信度才接受)",
        "3) 补齐上表 f2 / derivability / disposition / conflict / flip",
        "4) 按契约三档处置，起草进 staging",
        "",
        "注意笔记库的约束:",
        "- 不要把机制、诊断、启发式混成一类",
        "- 不要把判断和决策写成一句话",
        "- 不确定时优先保留上游层 / 优先链接对象",
        "- 对象边界需要人定,你只起草,我审定后再移进 canonical",
        "```",
        "",
        f"_item_id: `{row['item_id']}`  |  生成于 {datetime.now(timezone.utc).isoformat(timespec='seconds')}_",
        "",
    ]


def emit_discuss_packet(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    staging_dir: Path = STAGING_DIR,
) -> DiscussPacketResult:
    row = _load_item_row(conn, item_id)
    if row is None:
        return DiscussPacketResult(item_id=item_id, path=staging_dir, created=False, error="item not found")

    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"discuss_{_safe_item_id(item_id)}_{today_iso}.md"
    staging_dir.mkdir(parents=True, exist_ok=True)
    out_path = staging_dir / filename

    hits, neighbor_lines = _vault_neighbor_state(row)
    lines: list[str] = []
    lines.extend(_render_header(row, today_iso))
    lines.extend(_render_body_block(row))
    lines.extend(_render_chunk_draft(row, hits, _latest_digest_signals(conn, item_id)))
    lines.extend(neighbor_lines)
    lines.extend(_render_active_layer())
    lines.extend(_render_payload_block(row))
    lines.extend(_render_launch_prompt(row))

    out_path.write_text("\n".join(lines).rstrip() + "\n", "utf-8")
    return DiscussPacketResult(item_id=item_id, path=out_path, created=True)
