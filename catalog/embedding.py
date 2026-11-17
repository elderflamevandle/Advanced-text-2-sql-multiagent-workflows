"""Embedder interface for the catalog and query memory.

The sentence-transformers model is an optional extra. Without it get_embedder() returns
None and the catalog falls back to lexical search only, so nothing here is required.
"""
from __future__ import annotations

import logging
import threading
from typing import Protocol

logger = logging.getLogger(__name__)


class Embedder(Protocol):
    name: str

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class _SentenceTransformerEmbedder:
    def __init__(self) -> None:
        from vector.embeddings import get_embedder as _shared

        self._inner = _shared()
        self.name = getattr(self._inner, "MODEL_NAME", "sentence-transformers")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return list(self._inner.embed_query_cached(text))


_lock = threading.Lock()
_embedder: Embedder | None = None
_resolved = False


def get_embedder() -> Embedder | None:
    """Process-wide embedder, or None when the embedding model is not installed."""
    global _embedder, _resolved
    if not _resolved:
        with _lock:
            if not _resolved:
                try:
                    import sentence_transformers  # noqa: F401

                    _embedder = _SentenceTransformerEmbedder()
                except Exception as exc:  # noqa: BLE001 - optional dependency
                    logger.info("No embedding model available (%s): lexical search only", exc)
                    _embedder = None
                _resolved = True
    return _embedder


def set_embedder(embedder: Embedder | None) -> None:
    """Inject an embedder (tests, or a hosted embedding API)."""
    global _embedder, _resolved
    with _lock:
        _embedder = embedder
        _resolved = True


def reset_embedder() -> None:
    global _embedder, _resolved
    with _lock:
        _embedder = None
        _resolved = False
