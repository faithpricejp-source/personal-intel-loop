"""Covers codex review finding: `ranking=auto` fallback must be narrow."""
from __future__ import annotations

from datetime import date

import pytest

from personal_intel_loop import digest as digest_mod
# 这些用例测的是**排序回退**逻辑, 用 conn=None + monkeypatch 选择器。
# 预警前置在公开包装 select_candidates_with_fallback 里(要真 conn),
# 与回退无关, 故这里直接打内层。包装本身由 test_digest_alert_section.py 钉住。
from personal_intel_loop.digest import _select_ranked_with_fallback as select_candidates_with_fallback
from personal_intel_loop.vault_corpus import VaultCorpusStale


NOW = "2026-04-20T00:00:00Z"


def test_auto_falls_back_only_on_vault_corpus_stale(monkeypatch):
    captured = {}

    def fake_v2(
        conn,
        *,
        date_local,
        top_k,
        now_utc,
        candidate_pool,
        summarize,
        translate=False,
        tier_top_k=None,
        live_cooldown_days=1,
    ):
        raise VaultCorpusStale("collection has only 42 chunks")

    def fake_v1(conn, *, date_local, top_k, now_utc, live_cooldown_days=1):
        captured["v1_called"] = True
        return []

    monkeypatch.setattr(digest_mod, "select_digest_candidates_v2", fake_v2)
    monkeypatch.setattr(digest_mod, "select_digest_candidates", fake_v1)

    candidates, version = select_candidates_with_fallback(
        conn=None,
        date_local=date(2026, 4, 20),
        top_k=5,
        now_utc=NOW,
        ranking="auto",
    )
    assert captured.get("v1_called") is True
    assert version == digest_mod.RANKING_V1


def test_auto_does_not_mask_unrelated_v2_errors(monkeypatch):
    """Non-stale v2 failures (a real regression) must propagate, not silently fall back."""

    def fake_v2(
        conn,
        *,
        date_local,
        top_k,
        now_utc,
        candidate_pool,
        summarize,
        translate=False,
        tier_top_k=None,
        live_cooldown_days=1,
    ):
        raise ValueError("unexpected NaN in novelty")

    def fake_v1(conn, *, date_local, top_k, now_utc, live_cooldown_days=1):
        pytest.fail("v1 must not be called when v2 raises non-stale error under ranking=auto")

    monkeypatch.setattr(digest_mod, "select_digest_candidates_v2", fake_v2)
    monkeypatch.setattr(digest_mod, "select_digest_candidates", fake_v1)

    with pytest.raises(ValueError, match="NaN in novelty"):
        select_candidates_with_fallback(
            conn=None,
            date_local=date(2026, 4, 20),
            top_k=5,
            now_utc=NOW,
            ranking="auto",
        )


def test_explicit_v2_always_propagates_stale(monkeypatch):
    def fake_v2(
        conn,
        *,
        date_local,
        top_k,
        now_utc,
        candidate_pool,
        summarize,
        translate=False,
        tier_top_k=None,
        live_cooldown_days=1,
    ):
        raise VaultCorpusStale("bad")

    monkeypatch.setattr(digest_mod, "select_digest_candidates_v2", fake_v2)

    with pytest.raises(VaultCorpusStale):
        select_candidates_with_fallback(
            conn=None,
            date_local=date(2026, 4, 20),
            top_k=5,
            now_utc=NOW,
            ranking="v2",
        )


def test_explicit_v1_skips_v2(monkeypatch):
    def fake_v2(*args, **kwargs):
        pytest.fail("v2 must not run when ranking=v1")

    def fake_v1(conn, *, date_local, top_k, now_utc, live_cooldown_days=1):
        return [{"item_id": "x"}]

    monkeypatch.setattr(digest_mod, "select_digest_candidates_v2", fake_v2)
    monkeypatch.setattr(digest_mod, "select_digest_candidates", fake_v1)

    candidates, version = select_candidates_with_fallback(
        conn=None,
        date_local=date(2026, 4, 20),
        top_k=5,
        now_utc=NOW,
        ranking="v1",
    )
    assert version == digest_mod.RANKING_V1
    assert candidates == [{"item_id": "x"}]
