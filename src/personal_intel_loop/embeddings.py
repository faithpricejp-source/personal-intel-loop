"""Thin sentence-transformers wrapper aligned with the vault vector index.

The vault index is built by a separate indexer (not part of this repo) that chunks your
notes vault, embeds the chunks with the same MODEL_NAME and writes them to a Qdrant
collection. Keep MODEL_NAME in sync with that indexer so pil and the index share one space.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import numpy as np

from personal_intel_loop import APP_HOME

MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"
MAX_SEQ_LENGTH = 512
DEFAULT_DEVICE = "mps"
# The live collection name is whatever the indexer last wrote into the marker file.
VAULT_QDRANT_URL = os.environ.get("PIL_QDRANT_URL") or "http://localhost:6333"
VAULT_QDRANT_COLLECTION_MARKER = Path(
    os.environ.get("PIL_QDRANT_COLLECTION_MARKER")
    or APP_HOME / "qdrant_collection.txt"
).expanduser()

_MODEL = None



def load_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(MODEL_NAME, device=DEFAULT_DEVICE)
    model.max_seq_length = MAX_SEQ_LENGTH
    _MODEL = model
    return model


def embed_documents(texts: Iterable[str]) -> np.ndarray:
    """Encode documents for storage-side use (item bodies, vault chunks)."""
    model = load_model()
    batch = list(texts)
    if not batch:
        return np.zeros((0, model.get_sentence_embedding_dimension()), dtype=np.float32)
    return np.asarray(
        model.encode(batch, normalize_embeddings=True, show_progress_bar=False),
        dtype=np.float32,
    )


def embed_queries(texts: Iterable[str]) -> np.ndarray:
    """Encode queries. Qwen3-Embedding requires prompt_name='query' on the query side."""
    model = load_model()
    batch = list(texts)
    if not batch:
        return np.zeros((0, model.get_sentence_embedding_dimension()), dtype=np.float32)
    return np.asarray(
        model.encode(
            batch,
            prompt_name="query",
            normalize_embeddings=True,
            show_progress_bar=False,
        ),
        dtype=np.float32,
    )
