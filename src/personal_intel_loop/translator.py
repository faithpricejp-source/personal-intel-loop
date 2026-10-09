"""Translate non-Chinese article bodies to Chinese.

Uses the primary / fallback tiers of `llm_backend` (configured via environment variables).
A failed or unconfigured translation just leaves the item untranslated.
"""
from __future__ import annotations

from typing import Any

DEFAULT_TRANSLATE_MODEL = "primary"
TRANSLATE_BODY_LIMIT = 6000
_CJK_THRESHOLD = 0.15


def needs_translation(text: str) -> bool:
    """Return True if the text is predominantly non-Chinese (CJK ratio < threshold)."""
    if not text or not text.strip():
        return False
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff")
    return cjk / len(text) < _CJK_THRESHOLD


def translate_body(
    body: str,
    title: str = "",
    *,
    model: str = DEFAULT_TRANSLATE_MODEL,
    timeout_seconds: float = 60.0,
) -> str | None:
    """Translate body to Chinese. Returns translated string or None on failure."""
    text = (body or "").strip()
    if not text:
        return None

    excerpt = text[:TRANSLATE_BODY_LIMIT]
    has_truncation = len(text) > TRANSLATE_BODY_LIMIT

    prompt_parts = ["请把以下文章翻译成中文。直接输出译文，保留段落结构，不要加解释或前言。"]
    if title:
        prompt_parts.append(f"标题：{title}")
    prompt_parts.append("")
    prompt_parts.append(excerpt)
    if has_truncation:
        prompt_parts.append(f"\n[原文超过 {TRANSLATE_BODY_LIMIT} 字符，以上为节选]")
    prompt = "\n".join(prompt_parts)

    from personal_intel_loop import llm_backend

    try:
        r = llm_backend.chat_first_available(prompt, max_tokens=4000, timeout=max(timeout_seconds * 4, 120))
    except Exception:
        return None
    if not r:
        return None
    return (r.get("text") or "").strip() or None
