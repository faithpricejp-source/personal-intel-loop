"""Query wrapper over the vault Qdrant collection (built by an external indexer, see embeddings.py)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from personal_intel_loop.embeddings import (
    VAULT_QDRANT_COLLECTION_MARKER,
    VAULT_QDRANT_URL,
)

VAULT_STALE_THRESHOLD = 400

_CLIENT = None
_COLLECTION_NAME: str | None = None


class VaultCorpusStale(RuntimeError):
    """Raised when the vault collection is missing or obviously under-indexed."""


@dataclass
class VaultHit:
    score: float
    source: str
    title: str
    layer: str
    note_id: str
    object_type: str
    domain: str
    status: str
    text: str


def _load_collection(
    url: str = VAULT_QDRANT_URL,
    marker: Path = VAULT_QDRANT_COLLECTION_MARKER,
):
    global _CLIENT, _COLLECTION_NAME
    if _CLIENT is not None and _COLLECTION_NAME is not None:
        return _CLIENT, _COLLECTION_NAME
    from qdrant_client import QdrantClient

    try:
        name = marker.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VaultCorpusStale(f"collection marker unreadable: {marker}") from exc
    if not name:
        raise VaultCorpusStale(f"collection marker empty: {marker}")
    client = QdrantClient(url=url, timeout=30)
    try:
        client.get_collection(name)
    except Exception as exc:
        raise VaultCorpusStale(
            f"collection {name!r} unavailable at {url}; check your vault indexer"
        ) from exc
    _CLIENT, _COLLECTION_NAME = client, name
    return client, name


def assert_fresh(min_count: int = VAULT_STALE_THRESHOLD) -> int:
    client, name = _load_collection()
    count = client.count(name, exact=True).count
    if count < min_count:
        raise VaultCorpusStale(
            f"vault collection {name!r} has only {count} chunks (< {min_count}); rebuild the vault index before using"
        )
    return count


def _build_filter(object_types: Iterable[str] | None, layers: Iterable[str] | None):
    from qdrant_client import models

    must = []
    for key, values in (("object_type", object_types), ("layer", layers)):
        picked = [v for v in (values or []) if v]
        if picked:
            must.append(models.FieldCondition(key=key, match=models.MatchAny(any=picked)))
    return models.Filter(must=must) if must else None


def query_top_k(
    query_vector: np.ndarray,
    *,
    k: int = 5,
    object_types: Iterable[str] | None = None,
    layers: Iterable[str] | None = None,
) -> list[VaultHit]:
    client, name = _load_collection()
    points = client.query_points(
        collection_name=name,
        query=query_vector.astype("float32").tolist(),
        query_filter=_build_filter(object_types, layers),
        limit=max(k, 1),
        with_payload=True,
        with_vectors=False,
    ).points
    hits: list[VaultHit] = []
    for point in points:
        meta = point.payload or {}
        doc = str(meta.get("text", ""))
        preview = doc[:400] + ("…" if len(doc) > 400 else "")
        hits.append(
            VaultHit(
                # Cosine collection: Qdrant returns similarity directly (Chroma gave 1 - distance).
                score=round(float(point.score), 4),
                source=str(meta.get("source", "")),
                title=str(meta.get("title", "")),
                layer=str(meta.get("layer", "")),
                note_id=str(meta.get("id", "")),
                object_type=str(meta.get("object_type", "")),
                domain=str(meta.get("domain", "")),
                status=str(meta.get("status", "")),
                text=preview,
            )
        )
    return hits


def max_similarity(
    query_vector: np.ndarray,
    *,
    object_types: Iterable[str] | None = None,
    layers: Iterable[str] | None = None,
    probe_k: int = 5,
) -> float:
    """Returns 0.0 when no hits; otherwise top score. Used for novelty."""
    hits = query_top_k(
        query_vector,
        k=probe_k,
        object_types=object_types,
        layers=layers,
    )
    if not hits:
        return 0.0
    return max(hit.score for hit in hits)
