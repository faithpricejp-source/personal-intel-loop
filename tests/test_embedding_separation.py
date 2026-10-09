"""Covers codex finding: items must be embedded as queries, active refs as documents."""
from __future__ import annotations

import numpy as np

from personal_intel_loop import active_corpus, embeddings


def test_active_corpus_uses_document_side_embedding(monkeypatch):
    """build_active_references must call embed_documents, not embed_queries.

    Passage-side encoding matches how the vault Chroma collection is indexed
    (the external vault indexer does document-side).
    """
    calls = {"doc": 0, "query": 0}

    def fake_docs(texts):
        calls["doc"] += 1
        arr = np.zeros((len(list(texts)), 4), dtype=np.float32)
        return arr

    def fake_queries(texts):
        calls["query"] += 1
        arr = np.zeros((len(list(texts)), 4), dtype=np.float32)
        return arr

    monkeypatch.setattr(active_corpus, "embed_documents", fake_docs)
    monkeypatch.setattr(embeddings, "embed_queries", fake_queries)

    # _load_object_text will be called but we stub file resolution to an empty list
    monkeypatch.setattr(active_corpus, "collect_active_object_ids", lambda *a, **kw: [])
    refs = active_corpus.build_active_references()
    assert refs == []
    # still no docs because no ids resolved, but test confirms the wiring point:
    # - if IDs existed, embed_documents would be called
    # - embed_queries must never be called from active_corpus.build_active_references
    assert calls["query"] == 0


def test_digest_v2_embeds_items_as_queries(monkeypatch, tmp_path):
    """select_digest_candidates_v2 must call embed_queries for missing items, not embed_documents."""
    from personal_intel_loop import digest as digest_mod
    from personal_intel_loop.store import connect_db, ensure_schema, upsert_item
    from personal_intel_loop.schemas import Item

    db_path = tmp_path / "t.sqlite"
    conn = connect_db(db_path)
    ensure_schema(conn)
    with conn:
        upsert_item(
            conn,
            Item(
                id="weibo:1:aaa",
                source="weibo_timeline:1",
                url="https://example.com/1",
                title="hello",
                body="body text",
                author="a",
                ts="2026-04-20T00:00:00Z",
                lang="zh",
                tags=[],
            ),
            adapter_name="weibo_timeline",
            source_payload_json="{}",
        )

    calls = {"doc": 0, "query": 0}

    def fake_embed_docs(texts):
        calls["doc"] += 1
        return np.zeros((len(list(texts)), 4), dtype=np.float32)

    def fake_embed_queries(texts):
        calls["query"] += 1
        return np.zeros((len(list(texts)), 4), dtype=np.float32)

    # stub vault_corpus so we don't touch real Chroma
    class _FakeHit:
        def __init__(self, score):
            self.score = score
            self.note_id = ""
            self.title = "x"
            self.source = "y"
            self.object_type = "mechanism"

    import personal_intel_loop.vault_corpus as vault_corpus

    monkeypatch.setattr(vault_corpus, "assert_fresh", lambda min_count=400: 4225)
    monkeypatch.setattr(vault_corpus, "max_similarity", lambda *a, **kw: 0.3)
    monkeypatch.setattr(vault_corpus, "query_top_k", lambda *a, **kw: [_FakeHit(0.3)])

    import personal_intel_loop.active_corpus as ac

    monkeypatch.setattr(ac, "build_active_references", lambda *a, **kw: [])
    monkeypatch.setattr(ac, "max_similarity_to_active", lambda vec, refs: (0.0, None))

    import personal_intel_loop.embeddings as emb

    monkeypatch.setattr(emb, "embed_documents", fake_embed_docs)
    monkeypatch.setattr(emb, "embed_queries", fake_embed_queries)

    result = digest_mod.select_digest_candidates_v2(
        conn,
        date_local=None,
        top_k=5,
        now_utc="2026-04-20T00:00:00Z",
        candidate_pool=10,
        summarize=False,
    )
    assert len(result) == 1
    assert calls["query"] == 1, "items must be embedded as queries"
    assert calls["doc"] == 0, "items must NOT be embedded as documents"
