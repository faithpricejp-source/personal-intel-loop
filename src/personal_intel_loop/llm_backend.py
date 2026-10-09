"""LLM 调用层：OpenAI 兼容的 /chat/completions 客户端，所有端点、模型、密钥都来自环境变量。

三个可选档位（都没配 = 没有 LLM，调用方按「模型不可用」降级）：

- primary  —— 主力档。`PIL_LLM_BASE_URL` / `PIL_LLM_MODEL` / `PIL_LLM_API_KEY`（或 `PIL_LLM_API_KEY_FILE`）
- fallback —— 主力档失败时的兜底。`PIL_LLM_FALLBACK_BASE_URL` / `PIL_LLM_FALLBACK_MODEL` /
              `PIL_LLM_FALLBACK_API_KEY`（或 `_FILE`）
- local    —— 本机模型服务（llama.cpp server / LM Studio / Ollama 的 OpenAI 兼容端点等）。
              `PIL_LOCAL_LLM_BASE_URL`（例如 `http://127.0.0.1:8080/v1`）/ `PIL_LOCAL_LLM_MODEL`

BASE_URL 写到 `/v1` 这一级（请求发往 `<BASE_URL>/chat/completions`）。

隐私提示：primary/fallback 指向云服务时，发给模型的内容（条目正文、阅读画像、批注、行为摘要等，
见 README「数据流向」）会离开本机。只想本机运行就只配 local，或把 primary 指向本机端点。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TIERS = ("primary", "fallback", "local")
_PREFIX = {"primary": "PIL_LLM", "fallback": "PIL_LLM_FALLBACK", "local": "PIL_LOCAL_LLM"}


class LLMUnavailable(RuntimeError):
    """该档位没有配置。"""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def tier_config(tier: str) -> dict[str, str] | None:
    """读某一档的配置；BASE_URL 或 MODEL 缺一即视为未配置，返回 None。"""
    prefix = _PREFIX[tier]
    base = _env(f"{prefix}_BASE_URL")
    model = _env(f"{prefix}_MODEL")
    if not base or not model:
        return None
    key = _env(f"{prefix}_API_KEY")
    key_file = _env(f"{prefix}_API_KEY_FILE")
    if not key and key_file:
        try:
            key = Path(key_file).expanduser().read_text("utf-8").strip()
        except OSError as exc:
            logger.warning("%s_API_KEY_FILE unreadable: %s", prefix, exc)
    return {"base_url": base.rstrip("/"), "model": model, "api_key": key}


def is_configured(tier: str) -> bool:
    return tier_config(tier) is not None


def chat(
    prompt: str,
    *,
    tier: str = "primary",
    max_tokens: int = 1500,
    timeout: float = 120.0,
    reasoning_effort: str | None = None,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """发一次单轮对话。返回 {"text", "model", "backend", "finish_reason"}。

    未配置抛 LLMUnavailable；网络/HTTP 错误原样抛出，由调用方决定是否回落。
    reasoning_effort 非空时按 OpenAI 约定放进请求体（不支持的服务端一般会忽略）。
    """
    import requests

    cfg = tier_config(tier)
    if cfg is None:
        raise LLMUnavailable(f"LLM tier '{tier}' is not configured")
    body: dict[str, Any] = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": int(max_tokens),
    }
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    if extra_body:
        body.update(extra_body)
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    response = requests.post(f"{cfg['base_url']}/chat/completions", headers=headers, json=body, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    choice = (payload.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    return {
        "text": text,
        "model": payload.get("model") or cfg["model"],
        "backend": tier,
        "finish_reason": choice.get("finish_reason"),
    }


def chat_first_available(prompt: str, *, tiers: tuple[str, ...] = ("primary", "fallback"), **kwargs: Any) -> dict[str, Any] | None:
    """按顺序试已配置的档位，返回第一个非空结果；全部未配置/失败/空返回 None。"""
    for tier in tiers:
        if not is_configured(tier):
            continue
        try:
            r = chat(prompt, tier=tier, **kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm tier %s failed: %s", tier, exc)
            continue
        if (r.get("text") or "").strip():
            return r
    return None
