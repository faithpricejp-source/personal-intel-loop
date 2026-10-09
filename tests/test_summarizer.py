from __future__ import annotations

import sys
import types

from personal_intel_loop import summarizer
from personal_intel_loop.summarizer import (
    NOISE_CATEGORIES,
    ItemSummary,
    _coerce_summary,
    _extract_json,
    build_prompt,
    summarize_candidate,
)


class _FakeResponse:
    def __init__(self, text: str, model: str = summarizer.DEFAULT_LOCAL_MODEL,
                 backend: str = "local"):
        self.text = text
        self.model = model
        self.backend = backend


def _fake_local(monkeypatch, *, text=None, error=None):
    """装一个假的本地档。text 给内容 = 命中；error 给字符串 = 该档失败。"""

    def _impl(prompt, timeout_seconds, attempts):
        attempts.append({"model": summarizer.DEFAULT_LOCAL_MODEL, "backend": "local",
                         "error": error or ""})
        if error:
            return None
        return summarizer._LocalModelResponse(text=text, model=summarizer.DEFAULT_LOCAL_MODEL,
                                              backend="local")

    monkeypatch.setattr(summarizer, "_try_local", _impl)


def _fake_fm(monkeypatch, *, text=None, error=None):
    def _impl(prompt, timeout_seconds, attempts):
        attempts.append({"model": summarizer.DEFAULT_FM_MODEL, "backend": "apple_fm",
                         "error": error or ""})
        if error:
            return None
        return summarizer._LocalModelResponse(text=text, model=summarizer.DEFAULT_FM_MODEL,
                                              backend="apple_fm")

    monkeypatch.setattr(summarizer, "_try_foundation_models", _impl)


def test_build_prompt_contains_all_signal_numbers_and_targets():
    prompt = build_prompt(
        title="DeepSeek V4 全面使用华为算力",
        source="weibo_timeline:1000000001",
        body="长文正文",
        novelty=0.27,
        active=0.64,
        enrich=0.54,
        trust=0.35,
        active_target="JDG_2026-04-18_DeepSeek_V4全面使用华为算力的含义",
        enrich_target=None,
    )
    assert "novelty_vs_vault: 0.270" in prompt
    assert "active_relevance: 0.640" in prompt
    assert "enrich_mec_dia_heu: 0.540" in prompt
    assert "source_trust: 0.350" in prompt
    assert "JDG_2026-04-18_DeepSeek_V4全面使用华为算力的含义" in prompt
    assert "enrich_target_hint: (none)" in prompt


def test_build_prompt_noise_categories_listed():
    prompt = build_prompt(
        title="t",
        source="s",
        body="b",
        novelty=0.5,
        active=0.5,
        enrich=0.5,
        trust=0.5,
        active_target=None,
        enrich_target=None,
    )
    for cat in ("manipulation", "venting", "already_known"):
        assert cat in prompt


def test_extract_json_handles_fenced_code_block():
    text = "```json\n{\"one_liner\": \"x\", \"is_noise\": false}\n```"
    assert _extract_json(text) == {"one_liner": "x", "is_noise": False}


def test_extract_json_handles_plain_object():
    text = '{"one_liner":"y","is_noise":false,"noise_category":null,"why_for_you":"z"}'
    assert _extract_json(text)["why_for_you"] == "z"


def test_extract_json_handles_leading_prose():
    text = 'Sure! Here it is:\n{"one_liner": "a", "is_noise": true, "noise_category": "venting"}'
    payload = _extract_json(text)
    assert payload["is_noise"] is True
    assert payload["noise_category"] == "venting"


def test_extract_json_returns_none_on_garbage():
    assert _extract_json("not json at all") is None
    assert _extract_json("") is None


def test_coerce_summary_clears_noise_category_when_not_noise():
    s = _coerce_summary({"one_liner": "x", "why_for_you": "y", "is_noise": False, "noise_category": "venting"})
    assert s.is_noise is False
    assert s.noise_category is None


def test_coerce_summary_rejects_unknown_noise_category():
    s = _coerce_summary({"one_liner": "x", "why_for_you": "y", "is_noise": True, "noise_category": "fake"})
    assert s.is_noise is True
    assert s.noise_category is None


def test_coerce_summary_accepts_all_valid_categories():
    for cat in NOISE_CATEGORIES:
        s = _coerce_summary({"one_liner": "x", "why_for_you": "y", "is_noise": True, "noise_category": cat})
        assert s.noise_category == cat


CAND = {"title": "t", "source": "s", "body": "b"}
GOOD = '{"one_liner": "一句话", "why_for_you": "理由", "topic": "地缘政治"}'


def _fake_cloud(monkeypatch, *, text=None, error=None):

    def _impl(prompt, timeout_seconds, attempts):
        attempts.append({"model": "model-x", "backend": "primary", "error": error or ""})
        if error:
            return None
        return summarizer._LocalModelResponse(text=text, model="model-x", backend="primary")

    monkeypatch.setattr(summarizer, "_try_cloud", _impl)


def test_summarize_candidate_uses_cloud_first_and_skips_local_by_default(monkeypatch):
    _fake_cloud(monkeypatch, text=GOOD)
    _fake_local(monkeypatch, error="不该被调用")
    _fake_fm(monkeypatch, error="不该被调用")
    summary = summarize_candidate(candidate=CAND)
    assert summary.backend == "primary"
    assert summary.one_liner == "一句话"
    assert [a["backend"] for a in summary.attempts] == ["primary"]


def test_summarize_candidate_cloud_failure_does_not_touch_local_by_default(monkeypatch):
    """本机档默认关：主力档全挂时宁可这条没摘要，也不自动起本机模型。"""
    _fake_cloud(monkeypatch, error="all free models failed")
    _fake_local(monkeypatch, text=GOOD)
    _fake_fm(monkeypatch, text=GOOD)
    summary = summarize_candidate(candidate=CAND)
    assert summary.one_liner == ""
    assert "all free models failed" in summary.error
    assert [a["backend"] for a in summary.attempts] == ["primary"]


def test_summarize_candidate_opt_in_tiers_run_in_order_and_keep_every_attempt(monkeypatch):
    _fake_cloud(monkeypatch, error="云端挂了")
    _fake_local(monkeypatch, error="server 起不来")
    _fake_fm(monkeypatch, text=GOOD)
    summary = summarize_candidate(candidate=CAND, use_local=True, use_foundation_models=True)
    assert summary.backend == "apple_fm"
    assert [a["backend"] for a in summary.attempts] == ["primary", "local", "apple_fm"]


def test_summarize_candidate_handles_invalid_json(monkeypatch):
    _fake_cloud(monkeypatch, text="模型今天不想输出 JSON")
    summary = summarize_candidate(candidate=CAND)
    assert summary.error == "invalid json"
    assert summary.raw_response == "模型今天不想输出 JSON"
    assert summary.backend == "primary"


def test_summarize_candidate_handles_empty_local_response(monkeypatch):
    """本地档返回空串时必须记成一次失败并降级，而不是当成成功的空摘要。"""
    _fake_cloud(monkeypatch, error="云端挂了")

    def empty_local(prompt, timeout_seconds, attempts):
        attempts.append({"model": summarizer.DEFAULT_LOCAL_MODEL, "backend": "local",
                         "error": "empty response"})
        return None

    monkeypatch.setattr(summarizer, "_try_local", empty_local)
    _fake_fm(monkeypatch, text=GOOD)
    summary = summarize_candidate(candidate=CAND, use_local=True, use_foundation_models=True)
    assert summary.backend == "apple_fm"
    assert summary.attempts[1]["error"] == "empty response"


def test_try_cloud_uses_primary_then_fallback_via_llm_backend(monkeypatch):
    """_try_cloud 走 llm_backend: 主力档失败时再试兜底档, 每档都留 attempt。"""
    from personal_intel_loop import llm_backend

    monkeypatch.setenv("PIL_LLM_BASE_URL", "http://primary.invalid/v1")
    monkeypatch.setenv("PIL_LLM_MODEL", "p-model")
    monkeypatch.setenv("PIL_LLM_FALLBACK_BASE_URL", "http://fallback.invalid/v1")
    monkeypatch.setenv("PIL_LLM_FALLBACK_MODEL", "f-model")
    calls = []

    def fake_chat(prompt, *, tier, **kw):
        calls.append(tier)
        if tier == "primary":
            raise RuntimeError("primary down")
        return {"text": GOOD, "model": "f-model", "backend": tier, "finish_reason": "stop"}

    monkeypatch.setattr(llm_backend, "chat", fake_chat)
    attempts = []
    resp = summarizer._try_cloud("p", 10, attempts)
    assert calls == ["primary", "fallback"]
    assert resp.backend == "fallback" and resp.model == "f-model"
    assert [a["backend"] for a in attempts] == ["primary", "fallback"]


def test_try_cloud_unconfigured_returns_none(monkeypatch):
    for name in ("PIL_LLM_BASE_URL", "PIL_LLM_MODEL", "PIL_LLM_FALLBACK_BASE_URL", "PIL_LLM_FALLBACK_MODEL"):
        monkeypatch.delenv(name, raising=False)
    attempts = []
    assert summarizer._try_cloud("p", 10, attempts) is None
    assert all(a["error"] == "not configured" for a in attempts)
