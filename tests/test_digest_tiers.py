"""Length-based tier classification + tier-aware digest selection."""
from __future__ import annotations

from datetime import date

import pytest

from personal_intel_loop import digest as digest_mod
from personal_intel_loop.digest import select_digest_candidates_v2
from personal_intel_loop.ranking import (
    TIER_LENGTH_THRESHOLD,
    TIER_LONGFORM,
    TIER_PULSE,
    classify_tier,
)


class TestClassifyTier:
    def test_empty_body_is_pulse(self):
        assert classify_tier("") == TIER_PULSE
        assert classify_tier(None) == TIER_PULSE

    def test_short_body_is_pulse(self):
        assert classify_tier("a" * 100) == TIER_PULSE
        assert classify_tier("a" * (TIER_LENGTH_THRESHOLD - 1)) == TIER_PULSE

    def test_boundary_is_longform(self):
        assert classify_tier("a" * TIER_LENGTH_THRESHOLD) == TIER_LONGFORM

    def test_long_body_is_longform(self):
        assert classify_tier("a" * 5000) == TIER_LONGFORM

    def test_custom_threshold(self):
        assert classify_tier("a" * 200, threshold=100) == TIER_LONGFORM
        assert classify_tier("a" * 50, threshold=100) == TIER_PULSE


class _FakeHit:
    def __init__(self, score=0.3, obj="mechanism"):
        self.score = score
        self.note_id = ""
        self.title = "x"
        self.source = "05 知识本体/foo.md"
        self.object_type = obj
        self.layer = "05 知识本体"


def _install_corpus_stubs(monkeypatch):
    import personal_intel_loop.vault_corpus as vc
    import personal_intel_loop.active_corpus as ac

    monkeypatch.setattr(vc, "assert_fresh", lambda min_count=400: 4225)
    monkeypatch.setattr(vc, "max_similarity", lambda *a, **kw: 0.3)
    monkeypatch.setattr(vc, "query_top_k", lambda *a, **kw: [_FakeHit()])
    monkeypatch.setattr(ac, "build_active_references", lambda *a, **kw: [])
    monkeypatch.setattr(ac, "max_similarity_to_active", lambda vec, refs: (0.0, None))


def _stub_embeddings(monkeypatch):
    import numpy as np
    import personal_intel_loop.embeddings as emb

    def _q(texts):
        return np.zeros((len(list(texts)), 4), dtype=np.float32)

    monkeypatch.setattr(emb, "embed_queries", _q)
    monkeypatch.setattr(emb, "embed_documents", _q)


def _seed(conn, uid: str, post_id: str, body: str, ts: str = "2026-04-20T00:00:00Z"):
    from personal_intel_loop.schemas import Item
    from personal_intel_loop.store import upsert_item

    item = Item(
        id=f"weibo:{uid}:{post_id}",
        source=f"weibo_timeline:{uid}",
        url=f"https://m.weibo.cn/status/{post_id}",
        title=body[:40] or "empty",
        body=body,
        author="a",
        ts=ts,
        lang="zh",
        tags=[],
    )
    with conn:
        upsert_item(conn, item, adapter_name="weibo_timeline", source_payload_json="{}")


def test_digest_buckets_by_actual_length_not_source(db_conn, monkeypatch):
    _install_corpus_stubs(monkeypatch)
    _stub_embeddings(monkeypatch)

    # same weibo source but wildly different lengths → tier should split them
    _seed(db_conn, "111", "short1", "短微博一句话。")
    _seed(db_conn, "111", "long1", "长微博" * 300)  # ~900 chars
    _seed(db_conn, "111", "short2", "又一条短的")
    _seed(db_conn, "111", "long2", "另一条长微博" * 200)

    result = select_digest_candidates_v2(
        db_conn,
        date_local=date(2026, 4, 20),
        top_k=100,
        now_utc="2026-04-20T12:00:00Z",
        candidate_pool=10,
        summarize=False,
    )
    tiers = {c["item_id"]: c["tier"] for c in result}
    assert tiers["weibo:111:short1"] == TIER_PULSE
    assert tiers["weibo:111:short2"] == TIER_PULSE
    assert tiers["weibo:111:long1"] == TIER_LONGFORM
    assert tiers["weibo:111:long2"] == TIER_LONGFORM


def test_digest_default_split_is_longform_70_pulse_30(db_conn, monkeypatch):
    _install_corpus_stubs(monkeypatch)
    _stub_embeddings(monkeypatch)

    # seed enough of each tier to exceed the cap
    for i in range(20):
        _seed(db_conn, "111", f"s{i}", "短的 post", ts=f"2026-04-{19 if i < 10 else 20}T{i%24:02d}:00:00Z")
    for i in range(20):
        _seed(db_conn, "111", f"l{i}", "长的 " * 300, ts=f"2026-04-{19 if i < 10 else 20}T{(i+1)%24:02d}:00:00Z")

    result = select_digest_candidates_v2(
        db_conn,
        date_local=date(2026, 4, 20),
        top_k=10,
        now_utc="2026-04-20T23:00:00Z",
        candidate_pool=50,
        summarize=False,
    )
    pulse = [c for c in result if c["tier"] == TIER_PULSE]
    longform = [c for c in result if c["tier"] == TIER_LONGFORM]
    # top_k=10 → pulse_cap = 3, longform_cap = 7
    assert len(pulse) == 3
    assert len(longform) == 7


def test_digest_explicit_tier_top_k_override(db_conn, monkeypatch):
    _install_corpus_stubs(monkeypatch)
    _stub_embeddings(monkeypatch)

    for i in range(10):
        _seed(db_conn, "111", f"s{i}", "短内容", ts=f"2026-04-20T{i:02d}:00:00Z")
    for i in range(10):
        _seed(db_conn, "111", f"l{i}", "长 " * 300, ts=f"2026-04-20T{(i+5)%24:02d}:00:00Z")

    result = select_digest_candidates_v2(
        db_conn,
        date_local=date(2026, 4, 20),
        top_k=999,
        now_utc="2026-04-20T23:00:00Z",
        candidate_pool=50,
        summarize=False,
        tier_top_k=(5, 2),  # pulse=5, longform=2
    )
    pulse = [c for c in result if c["tier"] == TIER_PULSE]
    longform = [c for c in result if c["tier"] == TIER_LONGFORM]
    assert len(pulse) == 5
    assert len(longform) == 2


def test_digest_longform_appears_before_pulse_in_output_order(db_conn, monkeypatch):
    """render_digest relies on tier transition; longform must come first."""
    _install_corpus_stubs(monkeypatch)
    _stub_embeddings(monkeypatch)

    _seed(db_conn, "111", "a_pulse", "短的")
    _seed(db_conn, "111", "a_long", "长的 " * 300)

    result = select_digest_candidates_v2(
        db_conn,
        date_local=date(2026, 4, 20),
        top_k=10,
        now_utc="2026-04-20T12:00:00Z",
        candidate_pool=5,
        summarize=False,
    )
    tiers_in_order = [c["tier"] for c in result]
    # longform must come before any pulse
    if TIER_PULSE in tiers_in_order and TIER_LONGFORM in tiers_in_order:
        first_pulse = tiers_in_order.index(TIER_PULSE)
        last_longform = len(tiers_in_order) - 1 - list(reversed(tiers_in_order)).index(TIER_LONGFORM)
        assert last_longform < first_pulse
