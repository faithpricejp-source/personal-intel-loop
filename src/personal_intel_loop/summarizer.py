"""LLM summarizer for digest candidates.

Routing (each tier is configured in `llm_backend` via environment variables):
1. primary tier (`PIL_LLM_*`), then the fallback tier (`PIL_LLM_FALLBACK_*`)
2. local OpenAI-compatible server (`PIL_LOCAL_LLM_*`) — off by default (`use_local=True` to enable)
3. macOS Foundation Models via `fm respond` — off by default

Output per candidate:
    {
      one_liner: str,          # 30-60 Chinese chars summary
      why_for_you: str,         # why this surfaces (novelty | active | enrich)
      is_noise: bool,          # True only for user's 3 noise categories
      noise_category: str|None # manipulation | venting | already_known
    }
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_LOCAL_MODEL = "local"
DEFAULT_FM_MODEL = "apple-foundation-models:on-device"

SUMMARIZE_BODY_LIMIT = 1500

NOISE_CATEGORIES = ("manipulation", "venting", "already_known")
TOPIC_CATEGORIES = (
    "AI与模型",
    "宏观与市场",
    "地缘政治",
    "日本",
    "商业与科技",
    "认知与方法",
    "其他",
)


LANES = ("material", "surprise", "track", "warmth", "none")


@dataclass
class ItemSummary:
    one_liner: str
    why_for_you: str
    is_noise: bool = False
    noise_category: str | None = None
    topic: str = "其他"
    profile_hit: str | None = None
    lane: str = "none"
    saturated: bool = False
    checkable_claim: str | None = None
    claim_check_after: str | None = None
    backstory: str | None = None
    so_what: str | None = None
    raw_response: str = ""
    backend: str = ""
    model: str = ""
    error: str | None = None
    attempts: list[dict[str, str]] = field(default_factory=list)


PROMPT_TEMPLATE = """你在帮一个用户做个人信息推荐。他的笔记库(vault)用 JDG(判断)/MON(监控)/DEC(决策)管理 active layer, 用 MEC(机制)/DIA(诊断)/HEU(启发式)沉淀稳定知识。

## 你的任务
对这条候选信息输出一个严格的 JSON, 字段如下。
- one_liner: 120-300 个中文字符的实质摘要。用户能读长文,不想要抖音式一句话总结。
    要保留:关键事实、具体数字、涉及的人名/机构/时间、各方立场或分歧点
    要避免:营销词、感叹号、口水式铺垫、"值得关注" 这种空话、原文照抄
    如果原文本身不足 120 字,就按原意紧凑重写即可,不要硬凑字数
- why_for_you: 30-80 个中文字符, 说明这条为什么值得他看; 允许的路径有三条:
    1) 对齐某条 active JDG/MON/DEC (提供了 active_target 字段时首选)
    2) 可能丰富知识本体中的 MEC/DIA/HEU
    3) pure novelty — vault 里还没有对应对象, 本身就是新维度的事实/观察
- is_noise: 仅当这条属于以下三类之一时返回 true, 否则必须 false:
    - manipulation: 广告、软文、engagement bait、诈骗/钓鱼、propaganda、astroturfing
    - venting: 只有情绪宣泄, 没有事实/观察/观点 (带情绪的有实质评论不算)
    - already_known: 明显是用户已经知道的常识或 vault 已覆盖的重复内容
- noise_category: is_noise=true 时填上述三选一, 否则填 null
- topic: 这条内容的主题门类, 从以下七选一(用于把日报分组): "AI与模型" / "宏观与市场" / "地缘政治" / "日本" / "商业与科技" / "认知与方法" / "其他"。
    判断不了或都不贴切时填 "其他"; 同时贴合多个时选最主要的那个。
- checkable_claim: 这条内容里最值得日后验证的一句可证伪断言或预测, 保留原文里的量与时间窗, 30-200 个中文字符;
    没有可证伪断言(纯叙述/纯观点/纯情绪)则填 null。这是本条对用户最硬的价值——时间会替他判对错。
- claim_check_after: 若断言带时间窗, 给出可以开始核验的日期 (YYYY-MM-DD); 否则 null。
- backstory (来龙): 这件事**之前**是什么状态、它从哪来——把这条放回它自己的时间线里, 30-120 个中文字符。
    要具体到可核的前情(此前的价格/政策/版本/上一轮同类事件), 不要"近年来备受关注"这类填充。
    正文里找不到、也无法从正文可靠推出的, 填 null——**不要编**。
- so_what (去脉): 这件事**往下**改变什么、谁受影响、接下来看什么, 30-120 个中文字符。
    要具体到承受后果的对象与传导路径, 不要"值得持续关注"这类空话。推不出来就填 null。
    backstory 与 so_what 两者都为 null 的条目, 是只报"发生了 A"的孤立新闻, 用户明确不要看。
- profile_hit: 逐字引用下面「阅读偏好 profile」里与这条最相关的一句(≤60 字), 是它决定了这条该怎么处理; 没有相关句则 null。
- lane: 按 profile 的 lane 判这条属于哪条, 五选一: "material"(外脑原料: 贴着某条机制但推不出来) /
    "surprise"(意外与跨域连接, 含 profile 列的细分: 意外机制/远距连接/预测兑现/失败复盘/反直觉的数) /
    "track"(主要价值是说话人的 track record: 表态、预测、被顶住时的反应) /
    "warmth"(人间温暖: 普通人善举、见义勇为、跨年回报; 判为 warmth 时 saturated 必须 false; 这类被编造最多,
    checkable_claim 必须填可核的具名事实——人物/地点/时间/金额——而不是 null) / "none"(都不是)。
- saturated: true 当且仅当这条落在 profile「域饱和表」里标为"饱和"的域, **且**不满足该行写的例外条件(如"含可检验预测或反直觉反例")。
    这是"早知道了"的机器判定: 参照系是用户脑子里已有的, 不是 vault 里有没有。

## 用户的阅读偏好 profile(判 profile_hit / lane / saturated 的唯一依据)
{profile}

**重要**: 不要因为某条内容"当前对不上任何 active JDG/MON"就判为 noise — novelty 本身有价值。不要做 tunnel vision 过滤。

## 输入
- item_title: {title}
- item_source: {source}
- item_body_excerpt: {body}
- signals:
    novelty_vs_vault: {novelty}
    active_relevance: {active}
    enrich_mec_dia_heu: {enrich}
    source_trust: {trust}
- active_target_hint: {active_target}
- enrich_target_hint: {enrich_target}

## 输出
只返回一个 JSON 对象,不要 markdown 代码块,不要解释。示例:
{{"one_liner": "...", "why_for_you": "...", "is_noise": false, "noise_category": null, "topic": "AI与模型", "checkable_claim": "开源模型会在 6 个月内追平前沿实验室的 coding 能力", "claim_check_after": "2026-10-15", "backstory": "此前开源模型在 SWE-bench 上落后前沿实验室约 12 个月, 上一次差距收窄发生在 2025 年底的蒸馏浪潮", "so_what": "若成立则前沿实验室的 coding 溢价消失, 直接压缩按 token 计价的编码 API 定价, 先看下一轮开源权重发布的 benchmark", "profile_hit": "AI 产业经济: 饱和, 只要含可检验预测或反直觉反例的", "lane": "material", "saturated": false}}
"""


def build_prompt(
    *,
    title: str,
    source: str,
    body: str,
    novelty: float,
    active: float,
    enrich: float,
    trust: float,
    active_target: str | None,
    enrich_target: str | None,
    profile: str = "",
) -> str:
    body_excerpt = (body or "")[:SUMMARIZE_BODY_LIMIT]
    return PROMPT_TEMPLATE.format(
        profile=(profile or "").strip() or "(无 profile)",
        title=(title or "").strip() or "(no title)",
        source=source or "unknown",
        body=body_excerpt or "(empty)",
        novelty=f"{novelty:.3f}",
        active=f"{active:.3f}",
        enrich=f"{enrich:.3f}",
        trust=f"{trust:.3f}",
        active_target=active_target or "(none)",
        enrich_target=enrich_target or "(none)",
    )


JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = JSON_OBJECT_RE.search(cleaned)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _coerce_summary(payload: dict[str, Any]) -> ItemSummary:
    is_noise = bool(payload.get("is_noise", False))
    category = payload.get("noise_category")
    if is_noise:
        category = str(category or "").strip() or None
        if category not in NOISE_CATEGORIES:
            category = None
    else:
        category = None
    topic = str(payload.get("topic") or "").strip()
    if topic not in TOPIC_CATEGORIES:
        topic = "其他"
    claim = str(payload.get("checkable_claim") or "").strip()[:300] or None
    check_after = str(payload.get("claim_check_after") or "").strip() or None
    if check_after and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", check_after):
        check_after = None
    backstory = str(payload.get("backstory") or "").strip()[:300] or None
    so_what = str(payload.get("so_what") or "").strip()[:300] or None
    profile_hit = str(payload.get("profile_hit") or "").strip()[:120] or None
    lane = str(payload.get("lane") or "").strip()
    if lane not in LANES:
        lane = "none"
    saturated = payload.get("saturated") is True
    return ItemSummary(
        one_liner=str(payload.get("one_liner") or "").strip()[:400],
        why_for_you=str(payload.get("why_for_you") or "").strip()[:200],
        is_noise=is_noise,
        noise_category=category,
        topic=topic,
        checkable_claim=claim,
        backstory=backstory,
        so_what=so_what,
        claim_check_after=check_after,
        profile_hit=profile_hit,
        lane=lane,
        saturated=saturated,
    )


def _profile_for_prompt() -> str:
    """喂模型的 profile 正文(不含未接受提案)。缺文件时为空串, 模型按无 profile 处理。"""
    from personal_intel_loop.profile import profile_body_for_prompt

    return profile_body_for_prompt()


class _LocalModelResponse:
    """本地后端的响应壳，字段与下游解析逻辑对齐。"""

    def __init__(self, text: str, model: str, backend: str):
        self.text = text
        self.model = model
        self.backend = backend


def _try_tier(tier: str, prompt: str, timeout_seconds: float, attempts: list[dict[str, str]]):
    from personal_intel_loop import llm_backend

    if not llm_backend.is_configured(tier):
        attempts.append({"model": "-", "backend": tier, "error": "not configured"})
        return None
    try:
        r = llm_backend.chat(prompt, tier=tier, max_tokens=800, timeout=timeout_seconds)
    except Exception as exc:
        attempts.append({"model": "-", "backend": tier, "error": str(exc)[:320]})
        return None
    text = (r.get("text") or "").strip()
    model = r.get("model") or tier
    if not text:
        attempts.append({"model": model, "backend": tier, "error": "empty response"})
        return None
    attempts.append({"model": model, "backend": tier, "error": ""})
    return _LocalModelResponse(text=text, model=model, backend=tier)


def _try_cloud(prompt: str, timeout_seconds: float, attempts: list[dict[str, str]]):
    """主力档，失败再试兜底档。输入是信息流条目 + 阅读偏好 profile。"""
    return _try_tier("primary", prompt, timeout_seconds, attempts) or _try_tier(
        "fallback", prompt, timeout_seconds, attempts
    )


def _try_local(prompt: str, timeout_seconds: float, attempts: list[dict[str, str]]):
    """本机 OpenAI 兼容模型服务（PIL_LOCAL_LLM_*）。不依赖网络。"""
    return _try_tier("local", prompt, timeout_seconds, attempts)


def _try_foundation_models(
    prompt: str,
    timeout_seconds: float,
    attempts: list[dict[str, str]],
):
    """Call macOS Foundation Models through the macOS 27 `fm` command."""
    fm_bin = shutil.which("fm")
    if not fm_bin:
        attempts.append(
            {
                "model": DEFAULT_FM_MODEL,
                "backend": "apple_fm",
                "error": "fm command not found; requires macOS 27+",
            }
        )
        return None

    try:
        completed = subprocess.run(
            [fm_bin, "respond", prompt],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except Exception as exc:
        attempts.append({"model": DEFAULT_FM_MODEL, "backend": "apple_fm", "error": str(exc)})
        return None
    if completed.returncode != 0:
        error = (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
        attempts.append({"model": DEFAULT_FM_MODEL, "backend": "apple_fm", "error": error[:320]})
        return None
    text = (completed.stdout or "").strip()
    if not text:
        attempts.append({"model": DEFAULT_FM_MODEL, "backend": "apple_fm", "error": "empty response"})
        return None
    attempts.append({"model": DEFAULT_FM_MODEL, "backend": "apple_fm", "error": ""})
    return _LocalModelResponse(text=text, model=DEFAULT_FM_MODEL, backend="apple_fm")


def summarize_candidate(
    *,
    candidate: dict[str, Any],
    use_cloud: bool = True,
    use_foundation_models: bool = False,
    use_local: bool = False,
    timeout_seconds: float = 30.0,
    profile_text: str | None = None,
) -> ItemSummary:
    """Summarize a single v2-ranked candidate.

    Route:
      1. 主力档 → 兜底档（llm_backend 配置）
      2. 本机 OpenAI 兼容服务 —— 默认关（use_local=True 开启）
      3. macOS Foundation Models via `fm respond` —— 默认关（实测在本任务上质量明显不够）

    On full failure, `ItemSummary.attempts` preserves per-model error detail so failure
    diagnosis is possible.
    """

    why = candidate.get("why_for_you") or {}
    active_target = None
    enrich_target = None
    if isinstance(why, dict):
        if why.get("route") == "active_layer":
            active_target = why.get("target_id")
        elif why.get("route") == "enrich_mec_dia_heu":
            enrich_target = why.get("target_id")

    prompt = build_prompt(
        title=candidate.get("title") or "",
        source=candidate.get("source") or "",
        body=candidate.get("body") or candidate.get("summary") or "",
        novelty=float(candidate.get("novelty", 0.0)),
        active=float(candidate.get("active_relevance", 0.0)),
        enrich=float(candidate.get("enrich_score", 0.0)),
        trust=float(candidate.get("trust_score", 0.0)),
        active_target=active_target,
        enrich_target=enrich_target,
        profile=profile_text if profile_text is not None else _profile_for_prompt(),
    )

    attempts: list[dict[str, str]] = []
    response = _try_cloud(prompt, max(timeout_seconds * 4, 120), attempts) if use_cloud else None
    if response is None and use_local:
        response = _try_local(prompt, max(timeout_seconds * 4, 120), attempts)
    if response is None and use_foundation_models:
        response = _try_foundation_models(prompt, max(timeout_seconds * 2, 120), attempts)
    if response is None:
        detail = "; ".join(f"{a['backend']}/{a['model']}: {a['error']}" for a in attempts) or "all attempts empty"
        return ItemSummary(
            one_liner="",
            why_for_you="",
            error=f"all summarizer models failed [{detail}]",
            attempts=attempts,
        )

    raw_text = response.text.strip()
    parsed = _extract_json(raw_text)
    if parsed is None:
        return ItemSummary(
            one_liner="",
            why_for_you="",
            raw_response=raw_text,
            backend=getattr(response, "backend", ""),
            model=getattr(response, "model", ""),
            error="invalid json",
            attempts=attempts,
        )
    summary = _coerce_summary(parsed)
    summary.raw_response = raw_text
    summary.backend = getattr(response, "backend", "")
    summary.model = getattr(response, "model", "")
    summary.attempts = attempts
    return summary


def summarize_candidates(
    candidates: list[dict[str, Any]],
    *,
    timeout_seconds: float = 30.0,
) -> list[ItemSummary]:
    return [
        summarize_candidate(candidate=cand, timeout_seconds=timeout_seconds)
        for cand in candidates
    ]
