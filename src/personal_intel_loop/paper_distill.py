"""「编辑部来信」蒸馏: 攒够一批 style/topic 评分后, 让模型归纳成 ≤3 条 profile 修订提案。

提案行格式与现有提案一致, 经 profile.append_proposal 追加到「待接受的修订」,
用户在 /profile 页(或契约的 /api/proposal)接受/拒绝。蒸馏过的评分打上 distilled_at。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from personal_intel_loop.paper_common import item_ai_payload
from personal_intel_loop.schemas import normalize_dt_to_utc_z

logger = logging.getLogger(__name__)

DISTILL_MIN_PROPOSALS = 3
DISTILL_MAX_PROPOSALS = 3
CLOUD_TIMEOUT_SECONDS = 120

_ARRAY_RE = re.compile(r"\[[\s\S]*\]")

DISTILL_PROMPT = """你在维护一个人的「阅读偏好 profile」——一页自然语言文本, 是推荐系统对他的全部模型。
下面是他最近对若干条内容的 style / topic 评分(1 = 这类多来点, -1 = 这类少来点),
以及每条内容的标题、风格标签与主题。请归纳出**至多 {max_proposals} 条**对 profile 的最小修订提案。

## 当前 profile(正文, 不含未接受提案)
{profile}

## 评分记录(每行: 维度 方向 | 标题 | style_tags | topic | item_id)
{records}

## 规则
- 只提 profile 里缺失或写错的一句的最小修改; 改一句或加一行, 不重写段落。
- basis 要引用具体评分与内容特征(如「三次 style -1 都是数据密集的论战体」), 不写空话。
- target 逐字引用 profile 里要改的那句(≤60 字); 若是新增, 写 "新增于: <段落标题>"。
- item_id 填这条提案所依据的那条评分的 item_id; dim 填 style 或 topic。
- 没有值得提的就返回空数组 []。

## 输出
只返回一个 JSON 数组(不要 markdown 代码块), 每个元素:
{{"item_id": "...", "dim": "style 或 topic", "basis": "...", "target": "...", "suggestion": "..."}}
"""


def _call_cloud(prompt: str) -> str | None:
    from personal_intel_loop.summarizer import _try_cloud

    attempts: list[dict[str, str]] = []
    response = _try_cloud(prompt, CLOUD_TIMEOUT_SECONDS, attempts)
    return response.text if response else None


def _extract_array(text: str | None) -> list | None:
    """解析 JSON 数组; 返回 None 表示完全解析失败(与合法的空数组 [] 区分开)。"""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = _ARRAY_RE.search(cleaned)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, list) else None


def _pending_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT item_id, dim, value FROM item_ratings WHERE dim IN ('style','topic') AND distilled_at IS NULL ORDER BY ts ASC"
    ).fetchall()
    out = []
    for row in rows:
        title_row = conn.execute("SELECT title FROM items WHERE item_id=?", (row["item_id"],)).fetchone()
        payload = item_ai_payload(conn, row["item_id"]) or {}
        out.append(
            {
                "item_id": row["item_id"],
                "dim": row["dim"],
                "value": int(row["value"]),
                "title": (title_row["title"] if title_row else "") or "(无标题)",
                "style_tags": payload.get("style_tags") or [],
                "topic": payload.get("topic") or "(未标)",
            }
        )
    return out


def _record_line(entry: dict) -> str:
    direction = "喜欢(+1)" if entry["value"] == 1 else "不喜欢(-1)"
    tags = "、".join(entry["style_tags"]) if entry["style_tags"] else "无"
    return f"{entry['dim']} {direction} | {entry['title']} | style_tags: {tags} | topic: {entry['topic']} | {entry['item_id']}"


def _valid_proposal(proposal, known_ids: set[str]) -> dict | None:
    if not isinstance(proposal, dict):
        return None
    item_id = str(proposal.get("item_id") or "").strip()
    dim = str(proposal.get("dim") or "").strip()
    if dim not in ("style", "topic"):
        return None
    if item_id not in known_ids:
        return None
    return {
        "item_id": item_id,
        "dim": dim,
        "basis": str(proposal.get("basis") or "").strip(),
        "target": str(proposal.get("target") or "").strip(),
        "suggestion": str(proposal.get("suggestion") or "").strip(),
    }


def distill(conn: sqlite3.Connection, *, llm_call: Callable[[str], str | None] | None = None, profile_path: Path | None = None, min_new: int = 3) -> int:
    """把未蒸馏的 style/topic 评分交给模型, 追加 ≤3 条提案; 完成后打上 distilled_at。返回追加条数。"""
    from personal_intel_loop.profile import append_proposal, load_profile, profile_body_for_prompt, resolve_profile_path

    rows = _pending_rows(conn)
    if len(rows) < min_new:
        return 0
    if profile_path is None:
        profile_path = resolve_profile_path()

    prompt = DISTILL_PROMPT.format(
        max_proposals=DISTILL_MAX_PROPOSALS,
        profile=profile_body_for_prompt(profile_path).strip() or "(空)",
        records="\n".join(_record_line(entry) for entry in rows),
    )
    try:
        raw = llm_call(prompt) if llm_call is not None else _call_cloud(prompt)
    except Exception as exc:
        # 10-05 审计 F16: 失败留痕——持续失败时 pending 评分无界累积, 零日志只能靠 distilled_at 全空倒推
        logger.warning("distill: llm failed, will retry next run: %s", exc)
        return 0  # 模型失败不打 distilled_at, 下次重试

    parsed = _extract_array(raw)
    if parsed is None:
        logger.warning("distill: unparseable llm output, will retry next run: %.200r", raw)
        return 0  # 输出完全解析不了: 不追加也不回填, 下次重试

    known_ids = {entry["item_id"] for entry in rows}
    proposals = []
    for proposal in parsed:
        cleaned = _valid_proposal(proposal, known_ids)
        if cleaned:
            proposals.append(cleaned)
    proposals = proposals[:DISTILL_MAX_PROPOSALS]

    today = datetime.now(timezone.utc).astimezone().date().isoformat()
    appended = 0
    for proposal in proposals:
        line = (
            f"[{today} · {proposal['dim']} · {proposal['item_id']}] "
            f"依据: {proposal['basis'][:160]} → 目标: {proposal['target'][:120]} → 建议: {proposal['suggestion'][:240]}"
        )
        # fix-1007-N-9: 与 profile_proposals 相同的子串键。中途崩溃重跑时已追加的提案不再追加一遍。
        key = f"· {proposal['dim']} · {proposal['item_id']}]"
        if key in load_profile(profile_path):
            continue
        append_proposal(line, profile_path)
        appended += 1

    # 模型有可解析输出(无论提了几条)都算蒸馏完成, 防止同一批评分反复进提示词
    # 10-05 审计 F14: 只给本次送评的 (item_id, dim) 打标——模型调用窗口内新增的评分
    # 不在快照里, 原先不限 item_id 的 UPDATE 会把它们标成已蒸馏但从未蒸馏
    now_z = normalize_dt_to_utc_z(datetime.now(timezone.utc))
    for entry in rows:
        conn.execute(
            "UPDATE item_ratings SET distilled_at=? WHERE item_id=? AND dim=? AND distilled_at IS NULL",
            (now_z, entry["item_id"], entry["dim"]),
        )
    conn.commit()
    return appended
