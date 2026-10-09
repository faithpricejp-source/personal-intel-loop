"""反馈 → profile 修改提案。回路的"写"侧。

用户勾了原因码之后, 模型读三样——那条内容、原因码、当前 profile——写一条提案追加到
profile 文末「待接受的修订」。提案是文本 diff 不是权重: 用户能读、能拒、能手改。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from personal_intel_loop import RUNS_DIR
from personal_intel_loop.profile import append_proposal, load_profile, resolve_profile_path
from personal_intel_loop.schemas import normalize_dt_to_utc_z

REASON_ACTIONS = ("already_known", "unclear", "not_interested", "keep", "deep_discuss")
STATE_PATH = RUNS_DIR / "profile_proposals_state.json"
# 同一反馈事件模型连续失败这么多次就记 warning 跳过, 水位线越过它。
# 否则一条持续返回坏 JSON 的事件会卡住后续全部反馈(924898b1 改成失败即停之后的副作用)。
# 计数存 state JSON 的 fail_counts{event_id: n}(跨进程: scan-feedback --propose 每 30 分钟一轮),
# 成功/no_change/已提案/跳过后清掉; 跳过记录留在 skipped_events(有界), 供追溯与防重试。
MAX_EVENT_FAILURES = 3
SKIPPED_EVENTS_KEEP = 200

logger = logging.getLogger(__name__)

ACTION_ZH = {
    "already_known": "早知道(我脑子里已有, 不需要再推)",
    "unclear": "没看懂(文章没写清, 不是新颖性问题)",
    "not_interested": "不感兴趣(域外/与我无关——不是早知道也不是没写清; 提案应落在域表: 这个域该不该进、什么例外)",
    "keep": "留(值得读原文)",
    "deep_discuss": "深挖(值得进外脑讨论)",
}

PROPOSAL_PROMPT = """你在维护一个人的「阅读偏好 profile」——一页自然语言文本, 是推荐系统对他的全部模型。
他刚对一条推送打了反馈。你的任务: 判断这条反馈是否揭示了 profile 里缺失或写错的一句, 若是, 提出**一条**最小修改。

## 当前 profile
{profile}

## 这条内容
- 标题: {title}
- 来源: {source}
- 正文节选: {body}
- 可检验断言(若有): {claim}

## 他的反馈
{action_zh}

## 规则
- 只在 profile 现有规则**解释不了**这条反馈时才提案。若现有规则已能推出这个反馈, 返回 no_change。
- 提案必须小: 改一句、或在某张表加一行。不重写段落, 不加新章节。
- target 必须逐字引用 profile 里要改的那句(≤60 字); 若是新增, target 填 "新增于: <段落标题>"。
- rationale 一句话, 引用这条内容的具体特征, 不写空话。

## 输出
只返回一个 JSON 对象, 不要 markdown 代码块:
{{"no_change": false, "target": "...", "change": "...", "rationale": "..."}}
或
{{"no_change": true, "rationale": "现有规则 X 已覆盖"}}
"""


def _load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


def fetch_reason_events(conn, *, since_ts: str | None, limit: int) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in REASON_ACTIONS)
    sql = f"""
        SELECT pe.event_id, pe.event_ts, pe.event_type AS action, pe.item_id, i.source, i.title, i.body,
               (SELECT claim FROM claims c WHERE c.item_id = pe.item_id ORDER BY extracted_at DESC LIMIT 1) AS claim
        FROM promotion_events pe
        JOIN items i ON i.item_id = pe.item_id
        WHERE pe.event_type IN ({placeholders}) AND pe.origin IN ('digest_checkbox', 'cli', 'web')
          AND (? IS NULL OR pe.event_ts > ?)
        ORDER BY pe.event_ts ASC
        LIMIT ?
    """
    rows = conn.execute(sql, (*REASON_ACTIONS, since_ts, since_ts, limit)).fetchall()
    return [dict(r) for r in rows]


def build_proposal_prompt(*, profile: str, event: dict[str, Any]) -> str:
    return PROPOSAL_PROMPT.format(
        profile=profile.strip() or "(空)",
        title=(event.get("title") or "").strip() or "(无标题)",
        source=event.get("source") or "unknown",
        body=((event.get("body") or "").strip()[:800]) or "(空)",
        claim=event.get("claim") or "无",
        action_zh=ACTION_ZH.get(event["action"], event["action"]),
    )


def default_llm(prompt: str) -> str | None:
    """复用摘要器的本地路由 (本机模型服务 → fm 兜底)。返回原文或 None。"""
    from personal_intel_loop.summarizer import _try_foundation_models, _try_local

    attempts: list[dict[str, str]] = []
    r = _try_local(prompt, 180, attempts) or _try_foundation_models(prompt, 120, attempts)
    return r.text if r else None


def format_proposal_line(*, event: dict[str, Any], parsed: dict[str, Any], today: str) -> str:
    return (
        f"[{today} · {event['action']} · {event['item_id']}] "
        f"依据: {str(parsed.get('rationale') or '').strip()[:160]} "
        f"→ 目标: {str(parsed.get('target') or '').strip()[:120]} "
        f"→ 建议: {str(parsed.get('change') or '').strip()[:240]}"
    )


def propose_from_feedback(
    conn,
    *,
    since_ts: str | None = None,
    limit: int = 20,
    dry_run: bool = False,
    llm: Callable[[str], str | None] = default_llm,
    profile_path: Path | None = None,
    state_path: Path = STATE_PATH,
) -> dict[str, Any]:
    from personal_intel_loop.summarizer import _extract_json

    if profile_path is None:
        profile_path = resolve_profile_path()  # fix-1007-N-7
    state = _load_state(state_path)
    since = since_ts if since_ts is not None else state.get("last_event_ts")
    events = fetch_reason_events(conn, since_ts=since, limit=limit)
    today = datetime.now(timezone.utc).astimezone().date().isoformat()
    proposals: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    fail_counts: dict[str, int] = dict(state.get("fail_counts") or {})
    skipped_events: list[dict[str, Any]] = list(state.get("skipped_events") or [])
    skipped_ids = {str(entry.get("event_id")) for entry in skipped_events}
    counts_before = dict(fail_counts)
    skipped_before = len(skipped_events)
    # fix-1007-N-1: 水位线只推进到最后一条处理成功的事件(含 no_change / already_proposed)。
    # 遇到第一条模型失败即停止, 否则失败反馈被越过后再也不会被 fetch_reason_events 捞回。
    last_ok_ts = since
    safe_ts = since  # 严格早于当前事件 ts 的最后成功位置(同 ts 事件可能一成一败)
    for event in events:
        if last_ok_ts != event["event_ts"]:
            safe_ts = last_ok_ts
        # 已因连续失败跳过的事件: 同 ts 兄弟失败使水位线退回时会被重新捞回, 按已处理越过, 不再调模型。
        if event["event_id"] in skipped_ids:
            skipped.append({"event_id": event["event_id"], "reason": "previously_skipped_after_failures"})
            last_ok_ts = event["event_ts"]
            continue
        # 去重: 同一 action·item_id 已有提案(待接受或已接受)就不再提——手动跑与 30 分钟 launchd 扫描
        # 曾并发处理同一事件各提一次(2026-08-26 实例)
        key = f"· {event['action']} · {event['item_id']}]"
        if key in load_profile(profile_path):
            skipped.append({"event_id": event["event_id"], "reason": "already_proposed"})
            last_ok_ts = event["event_ts"]  # fix-1007-N-1
            fail_counts.pop(event["event_id"], None)
            continue
        # 提案 prompt 带上完整 profile(含未接受提案), 避免重复提同一条
        prompt = build_proposal_prompt(profile=load_profile(profile_path), event=event)
        raw = llm(prompt)
        parsed = _extract_json(raw or "") if raw else None
        if not parsed:
            failures = int(fail_counts.get(event["event_id"], 0)) + 1
            if failures >= MAX_EVENT_FAILURES:
                logger.warning(
                    "profile proposal: event %s (%s · %s · ts=%s) model failed %d times in a row, skipped",
                    event["event_id"], event["action"], event["item_id"], event["event_ts"], failures,
                )
                skipped.append({"event_id": event["event_id"], "reason": f"skipped_after_{failures}_failures"})
                fail_counts.pop(event["event_id"], None)
                skipped_events.append({
                    "event_id": event["event_id"],
                    "item_id": event["item_id"],
                    "action": event["action"],
                    "event_ts": event["event_ts"],
                    "failures": failures,
                    "skipped_at": normalize_dt_to_utc_z(datetime.now(timezone.utc)),
                    "last_raw": (raw or "")[:200],
                })
                skipped_ids.add(event["event_id"])
                last_ok_ts = event["event_ts"]
                continue
            fail_counts[event["event_id"]] = failures
            skipped.append({"event_id": event["event_id"], "reason": "llm_failed_or_invalid_json", "failures": failures})
            # 抓取条件是 event_ts > 水位线: 同 ts 的前序事件已成功也不能把水位线停在这个 ts 上,
            # 否则失败事件照样被越过(真库有 4 组同 ts 原因码事件)。退回严格更早的位置。
            last_ok_ts = safe_ts
            break  # fix-1007-N-1
        if parsed.get("no_change"):
            skipped.append({"event_id": event["event_id"], "reason": f"no_change: {parsed.get('rationale', '')}"[:200]})
            last_ok_ts = event["event_ts"]  # fix-1007-N-1
            fail_counts.pop(event["event_id"], None)
            continue
        line = format_proposal_line(event=event, parsed=parsed, today=today)
        proposals.append({"event_id": event["event_id"], "action": event["action"], "line": line})
        if not dry_run:
            append_proposal(line, profile_path)
        last_ok_ts = event["event_ts"]  # fix-1007-N-1
        fail_counts.pop(event["event_id"], None)
    watermark_moved = bool(last_ok_ts) and last_ok_ts != since
    # 前 1、2 次失败水位线不动, 计数变化也要落盘, 否则下轮从 0 数起永远到不了 3。
    bookkeeping_changed = fail_counts != counts_before or len(skipped_events) != skipped_before
    if not dry_run and (watermark_moved or bookkeeping_changed):
        if watermark_moved:
            state["last_event_ts"] = last_ok_ts  # fix-1007-N-1
        state["fail_counts"] = fail_counts
        state["skipped_events"] = skipped_events[-SKIPPED_EVENTS_KEEP:]
        state["last_run_utc"] = normalize_dt_to_utc_z(datetime.now(timezone.utc))
        _save_state(state, state_path)
    return {"events": len(events), "proposals": proposals, "skipped": skipped, "dry_run": dry_run}
