"""Cache invalidation for build_active_references."""
from __future__ import annotations

import os
import pickle
import time
from pathlib import Path

import numpy as np
import pytest

from personal_intel_loop import active_corpus as ac
from personal_intel_loop.active_corpus import (
    ActiveReference,
    _compute_cache_signature,
    _load_cache,
    _save_cache,
    build_active_references,
)


def _write_object(root: Path, object_id: str, body: str) -> Path:
    folder = root / "07 判断与决策" / "Judgments"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{object_id}.md"
    path.write_text(f"---\nobject_type: judgment\n---\n\n## 摘要\n{body}\n", "utf-8")
    return path


def _write_index(path: Path, wikilinks: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# index\n"] + [f"- [[{wl}]]\n" for wl in wikilinks]
    path.write_text("".join(lines), "utf-8")


def _fake_embed(monkeypatch, dim: int = 4):
    """Replace embed_documents so tests don't need the model."""
    calls = {"count": 0}

    def fake(texts):
        calls["count"] += 1
        batch = list(texts)
        return np.full((len(batch), dim), fill_value=0.1, dtype=np.float32)

    monkeypatch.setattr(ac, "embed_documents", fake)
    return calls


def test_cache_hit_avoids_reembed(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    _write_object(vault, "JDG_test1", "一条测试判断。")
    jdg_idx = vault / "07 判断与决策" / "idx.md"
    mon_idx = vault / "08 注意力配置" / "idx.md"
    _write_index(jdg_idx, ["JDG_test1"])
    _write_index(mon_idx, [])

    cache_path = tmp_path / "cache" / "active_refs.pkl"
    monkeypatch.setattr(ac, "CACHE_PATH", cache_path)

    calls = _fake_embed(monkeypatch)

    refs1 = build_active_references(jdg_dec_index=jdg_idx, mon_index=mon_idx, vault_root=vault)
    assert len(refs1) == 1
    assert calls["count"] == 1
    assert cache_path.exists()

    refs2 = build_active_references(jdg_dec_index=jdg_idx, mon_index=mon_idx, vault_root=vault)
    assert len(refs2) == 1
    assert calls["count"] == 1, "second call should hit cache, not re-embed"


def test_cache_invalidates_on_index_change(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    _write_object(vault, "JDG_a", "a body")
    _write_object(vault, "JDG_b", "b body")
    jdg_idx = vault / "07 判断与决策" / "idx.md"
    mon_idx = vault / "08 注意力配置" / "idx.md"
    _write_index(jdg_idx, ["JDG_a"])
    _write_index(mon_idx, [])

    cache_path = tmp_path / "cache" / "active_refs.pkl"
    monkeypatch.setattr(ac, "CACHE_PATH", cache_path)
    calls = _fake_embed(monkeypatch)

    refs1 = build_active_references(jdg_dec_index=jdg_idx, mon_index=mon_idx, vault_root=vault)
    assert {r.object_id for r in refs1} == {"JDG_a"}
    assert calls["count"] == 1

    # Modify index to add JDG_b → signature must change → re-embed
    time.sleep(0.05)  # ensure mtime changes
    _write_index(jdg_idx, ["JDG_a", "JDG_b"])

    refs2 = build_active_references(jdg_dec_index=jdg_idx, mon_index=mon_idx, vault_root=vault)
    assert {r.object_id for r in refs2} == {"JDG_a", "JDG_b"}
    assert calls["count"] == 2


def test_cache_invalidates_on_object_content_change(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    obj_path = _write_object(vault, "JDG_x", "original body")
    jdg_idx = vault / "07 判断与决策" / "idx.md"
    mon_idx = vault / "08 注意力配置" / "idx.md"
    _write_index(jdg_idx, ["JDG_x"])
    _write_index(mon_idx, [])

    cache_path = tmp_path / "cache" / "active_refs.pkl"
    monkeypatch.setattr(ac, "CACHE_PATH", cache_path)
    calls = _fake_embed(monkeypatch)

    build_active_references(jdg_dec_index=jdg_idx, mon_index=mon_idx, vault_root=vault)
    assert calls["count"] == 1

    time.sleep(0.05)
    obj_path.write_text(
        "---\nobject_type: judgment\n---\n\n## 摘要\nCOMPLETELY REWRITTEN body\n",
        "utf-8",
    )

    build_active_references(jdg_dec_index=jdg_idx, mon_index=mon_idx, vault_root=vault)
    assert calls["count"] == 2, "content change must bust cache"


def test_cache_disabled_by_env(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    _write_object(vault, "JDG_env", "body")
    jdg_idx = vault / "07 判断与决策" / "idx.md"
    mon_idx = vault / "08 注意力配置" / "idx.md"
    _write_index(jdg_idx, ["JDG_env"])
    _write_index(mon_idx, [])

    cache_path = tmp_path / "cache" / "active_refs.pkl"
    monkeypatch.setattr(ac, "CACHE_PATH", cache_path)
    monkeypatch.setenv("PIL_DISABLE_ACTIVE_CORPUS_CACHE", "1")

    calls = _fake_embed(monkeypatch)

    build_active_references(jdg_dec_index=jdg_idx, mon_index=mon_idx, vault_root=vault)
    build_active_references(jdg_dec_index=jdg_idx, mon_index=mon_idx, vault_root=vault)
    assert calls["count"] == 2, "env-disabled cache must always re-embed"
    assert not cache_path.exists()


def test_cache_use_cache_false_forces_rebuild(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    _write_object(vault, "JDG_force", "body")
    jdg_idx = vault / "07 判断与决策" / "idx.md"
    mon_idx = vault / "08 注意力配置" / "idx.md"
    _write_index(jdg_idx, ["JDG_force"])
    _write_index(mon_idx, [])

    cache_path = tmp_path / "cache" / "active_refs.pkl"
    monkeypatch.setattr(ac, "CACHE_PATH", cache_path)
    calls = _fake_embed(monkeypatch)

    build_active_references(jdg_dec_index=jdg_idx, mon_index=mon_idx, vault_root=vault)
    build_active_references(
        jdg_dec_index=jdg_idx, mon_index=mon_idx, vault_root=vault, use_cache=False
    )
    assert calls["count"] == 2


def test_corrupt_cache_triggers_rebuild(monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    _write_object(vault, "JDG_c", "body")
    jdg_idx = vault / "07 判断与决策" / "idx.md"
    mon_idx = vault / "08 注意力配置" / "idx.md"
    _write_index(jdg_idx, ["JDG_c"])
    _write_index(mon_idx, [])

    cache_path = tmp_path / "cache" / "active_refs.pkl"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"\x00\x01NOT A VALID PICKLE\xff")
    monkeypatch.setattr(ac, "CACHE_PATH", cache_path)

    calls = _fake_embed(monkeypatch)

    refs = build_active_references(jdg_dec_index=jdg_idx, mon_index=mon_idx, vault_root=vault)
    assert len(refs) == 1
    assert calls["count"] == 1
    # after rebuild, cache should be valid again
    payload = pickle.loads(cache_path.read_bytes())
    assert "signature" in payload and "refs" in payload


def test_signature_is_stable_across_calls(tmp_path):
    vault = tmp_path / "vault"
    p = _write_object(vault, "JDG_s", "body")
    idx = vault / "07 判断与决策" / "idx.md"
    _write_index(idx, ["JDG_s"])
    sig1 = _compute_cache_signature([idx], [p], "model-v1")
    sig2 = _compute_cache_signature([idx], [p], "model-v1")
    assert sig1 == sig2


def test_signature_changes_with_model_name(tmp_path):
    vault = tmp_path / "vault"
    p = _write_object(vault, "JDG_s", "body")
    idx = vault / "07 判断与决策" / "idx.md"
    _write_index(idx, ["JDG_s"])
    sig1 = _compute_cache_signature([idx], [p], "model-v1")
    sig2 = _compute_cache_signature([idx], [p], "model-v2")
    assert sig1 != sig2
