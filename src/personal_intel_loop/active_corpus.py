"""Build embedding reference vectors for the currently-active JDG / DEC / MON layer.

Reads two canonical active indices in the vault:
- 07 判断与决策/当前活跃判断与决策索引.md    (active JDG / DEC)
- 08 注意力配置/当前注意力索引.md            (active MON)

Resolves [[wikilinks]] to vault files, pulls frontmatter + first summary-ish section,
and returns embedded reference vectors the ranker can use for `active_relevance`.

## Caching (added 2026-04-20)

`build_active_references()` is called once per `pil digest` but loading + embedding
17 active refs costs ~5-10s. We cache the built list to `data/cache/active_refs.pkl`
keyed by a signature over (index file mtimes + resolved object file mtimes + model).
Warm runs return in <100ms. The cache self-invalidates when any tracked file changes.
"""
from __future__ import annotations

import hashlib
import logging
import os
import pickle
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from personal_intel_loop import APP_HOME, DATA_DIR, VAULT_DIR
from personal_intel_loop.embeddings import MODEL_NAME, embed_documents

logger = logging.getLogger(__name__)

#: 笔记库根目录(环境变量 PIL_VAULT_DIR); 未配置时指向一个不存在的目录, 相关功能返回空结果。
VAULT_ROOT = VAULT_DIR if VAULT_DIR is not None else APP_HOME / "vault"
ACTIVE_JDG_DEC_INDEX = VAULT_ROOT / "07 判断与决策" / "当前活跃判断与决策索引.md"
ACTIVE_MON_INDEX = VAULT_ROOT / "08 注意力配置" / "当前注意力索引.md"

CACHE_DIR = DATA_DIR / "cache"
CACHE_PATH = CACHE_DIR / "active_refs.pkl"
CACHE_DISABLED_ENV = "PIL_DISABLE_ACTIVE_CORPUS_CACHE"

WIKILINK_RE = re.compile(r"\[\[([^\[\]\|#]+?)(?:#[^\[\]\|]*)?(?:\|[^\[\]]*)?\]\]")
FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
SUMMARY_SECTION_RE = re.compile(r"(^##\s*摘要\s*\n[\s\S]*?)(?=^##\s|\Z)", re.MULTILINE)

SUMMARY_BODY_LIMIT = 1200


@dataclass
class ActiveReference:
    object_id: str
    source_file: str
    object_type: str
    text: str
    vector: np.ndarray


def _parse_wikilinks(index_path: Path) -> list[str]:
    if not index_path.exists():
        return []
    text = index_path.read_text("utf-8")
    seen: list[str] = []
    for match in WIKILINK_RE.finditer(text):
        target = match.group(1).strip()
        if target and target not in seen:
            seen.append(target)
    return seen


def _resolve_object_file(object_id: str, vault_root: Path = VAULT_ROOT) -> Path | None:
    direct = [
        vault_root / "07 判断与决策" / "Judgments" / f"{object_id}.md",
        vault_root / "07 判断与决策" / "Decisions" / f"{object_id}.md",
        vault_root / "08 注意力配置" / f"{object_id}.md",
    ]
    for candidate in direct:
        if candidate.exists():
            return candidate
    matches = list(vault_root.rglob(f"{object_id}.md"))
    for match in matches:
        rel = match.relative_to(vault_root).as_posix()
        if rel.startswith((".obsidian/", "staging/", "09 版本与归档/")):
            continue
        return match
    return None


def _extract_object_type(object_id: str, frontmatter_text: str) -> str:
    try:
        import yaml

        meta = yaml.safe_load(frontmatter_text) or {}
        value = meta.get("object_type") or meta.get("type")
        if value:
            return str(value)
    except Exception:
        pass
    prefix = object_id.split("_", 1)[0]
    return prefix or ""


def _extract_summary_text(body: str) -> str:
    match = SUMMARY_SECTION_RE.search(body)
    if match:
        section = match.group(1)
    else:
        section = body
    return section.strip()[:SUMMARY_BODY_LIMIT]


def _load_object_text(path: Path) -> tuple[str, str]:
    raw = path.read_text("utf-8")
    frontmatter_text = ""
    body = raw
    fm_match = FRONTMATTER_RE.match(raw)
    if fm_match:
        frontmatter_text = fm_match.group(1)
        body = raw[fm_match.end() :]
    summary = _extract_summary_text(body)
    return frontmatter_text, summary


def collect_active_object_ids(
    *,
    jdg_dec_index: Path = ACTIVE_JDG_DEC_INDEX,
    mon_index: Path = ACTIVE_MON_INDEX,
) -> list[str]:
    ids: list[str] = []
    for source in (jdg_dec_index, mon_index):
        for object_id in _parse_wikilinks(source):
            if object_id not in ids:
                ids.append(object_id)
    return ids


def _file_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _compute_cache_signature(
    index_paths: list[Path],
    object_paths: list[Path],
    model_name: str,
) -> str:
    """Signature = hash of (tracked file paths + mtimes + model name).

    Any change in index files, referenced object files, or embedding model →
    signature changes → cache invalidated on next load.
    """
    h = hashlib.sha256()
    h.update(model_name.encode("utf-8"))
    for path in sorted(index_paths + object_paths, key=lambda p: str(p)):
        h.update(str(path).encode("utf-8"))
        h.update(b"\0")
        h.update(f"{_file_mtime(path):.6f}".encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _resolve_cache_path(cache_path: Path | None) -> Path:
    """Resolve module-level CACHE_PATH at call time so monkeypatch works in tests."""
    if cache_path is not None:
        return cache_path
    # Re-read the module attribute each call instead of capturing at def-time.
    import personal_intel_loop.active_corpus as _self

    return _self.CACHE_PATH


def _load_cache(signature: str, cache_path: Path | None = None):
    if os.environ.get(CACHE_DISABLED_ENV):
        return None
    path = _resolve_cache_path(cache_path)
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            payload = pickle.load(f)
    except Exception as exc:
        logger.warning("active_refs cache unreadable (%s); rebuilding", exc)
        return None
    if not isinstance(payload, dict) or payload.get("signature") != signature:
        return None
    refs = payload.get("refs")
    if not isinstance(refs, list):
        return None
    return refs


def _save_cache(signature: str, refs: list["ActiveReference"], cache_path: Path | None = None) -> None:
    if os.environ.get(CACHE_DISABLED_ENV):
        return
    path = _resolve_cache_path(cache_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            pickle.dump({"signature": signature, "refs": refs}, f)
    except Exception as exc:
        logger.warning("active_refs cache write failed: %s", exc)


def build_active_references(
    *,
    jdg_dec_index: Path = ACTIVE_JDG_DEC_INDEX,
    mon_index: Path = ACTIVE_MON_INDEX,
    vault_root: Path = VAULT_ROOT,
    use_cache: bool = True,
) -> list[ActiveReference]:
    object_ids = collect_active_object_ids(jdg_dec_index=jdg_dec_index, mon_index=mon_index)

    # Resolve first (cheap — file existence check); we need paths to compute cache signature
    resolved: list[tuple[str, Path]] = []
    for object_id in object_ids:
        path = _resolve_object_file(object_id, vault_root=vault_root)
        if path is None:
            continue
        resolved.append((object_id, path))

    if not resolved:
        return []

    object_paths = [p for _, p in resolved]
    signature = _compute_cache_signature(
        index_paths=[jdg_dec_index, mon_index],
        object_paths=object_paths,
        model_name=MODEL_NAME,
    )

    if use_cache:
        cached = _load_cache(signature)
        if cached is not None:
            logger.debug("active_refs cache hit: %d refs", len(cached))
            return cached

    # Cold path — reread summaries and embed.
    texts: list[str] = []
    staged: list[tuple[str, Path, str, str]] = []
    for object_id, path in resolved:
        frontmatter, summary = _load_object_text(path)
        object_type = _extract_object_type(object_id, frontmatter)
        text = summary if summary else object_id
        staged.append((object_id, path, object_type, text))
        texts.append(text)

    vectors = embed_documents(texts)
    refs: list[ActiveReference] = [
        ActiveReference(
            object_id=object_id,
            source_file=str(path),
            object_type=object_type,
            text=text,
            vector=vector,
        )
        for (object_id, path, object_type, text), vector in zip(staged, vectors)
    ]

    if use_cache:
        _save_cache(signature, refs)
    return refs


def max_similarity_to_active(
    query_vector: np.ndarray,
    references: Iterable[ActiveReference],
) -> tuple[float, ActiveReference | None]:
    best_score = 0.0
    best_ref: ActiveReference | None = None
    for ref in references:
        score = float(np.dot(query_vector, ref.vector))
        if score > best_score:
            best_score = score
            best_ref = ref
    return best_score, best_ref
