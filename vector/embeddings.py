"""Embedding generation layer for semantic schema retrieval (VECTOR-002)."""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

from database.schema_utils import ColumnInfo, SchemaTable

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


def build_table_text(name: str, table: SchemaTable) -> str:
    """Format a SchemaTable into an embedding-ready text string."""
    col_parts = ", ".join(
        f"{col['name']} ({col['type']})" for col in table["columns"]
    )
    parts = [f"Table: {name}. Columns: {col_parts}."]

    fks = table.get("foreign_keys", [])
    if fks:
        fk_parts = ", ".join(
            f"{fk['column']} references {fk['references_table']}.{fk['references_column']}"
            for fk in fks
        )
        parts.append(f"Foreign keys: {fk_parts}.")

    sample_rows = table.get("sample_rows", [])
    if sample_rows:
        parts.append(f"Sample: {sample_rows[0]}")

    return " ".join(parts)


def build_column_text(
    col_name: str, col_type: str, table_name: str, sample_values: list
) -> str:
    """Format column info into an embedding-ready text string."""
    base = f"Column: {col_name} ({col_type}) in table {table_name}."
    limited = sample_values[:3]
    if limited:
        vals = ", ".join(str(v) for v in limited)
        return f"{base} Sample values: {vals}"
    return base


class EmbeddingGenerator:
    """Lazily loads the BGE model and exposes query/document embedding methods."""

    MODEL_NAME = "BAAI/bge-large-en-v1.5"

    def __init__(self) -> None:
        self._model = None

    # Make instance hashable so lru_cache works on instance methods
    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other

    def _get_model(self):
        """Lazily load the SentenceTransformer model on first call."""
        if self._model is None:
            logger.info("Loading BGE model (first run may download ~600MB)...")
            from sentence_transformers import SentenceTransformer  # lazy import
            self._model = SentenceTransformer(self.MODEL_NAME)
        return self._model

    def embed_query(self, query: str) -> list[float]:
        """Embed a single query string using BGE instruction prefix."""
        model = self._get_model()
        try:
            result = model.encode_query(query, normalize_embeddings=True)
        except AttributeError:
            result = model.encode(
                QUERY_INSTRUCTION + query, normalize_embeddings=True
            )
        return result.tolist()

    @lru_cache(maxsize=10_000)
    def embed_query_cached(self, query: str) -> tuple[float, ...]:
        """Embed a query with LRU caching. Returns tuple for hashability."""
        return tuple(self.embed_query(query))

    def embed_documents(
        self, texts: list[str], batch_size: int = 50
    ) -> list[list[float]]:
        """Embed a list of documents in batches of batch_size."""
        model = self._get_model()
        results: list[list[float]] = []
        total = len(texts)
        for i in range(0, total, batch_size):
            chunk = texts[i: i + batch_size]
            logger.info("Embedding %d/%d...", i + len(chunk), total)
            encoded = model.encode(
                chunk,
                batch_size=batch_size,
                show_progress_bar=False,
                normalize_embeddings=True,
                convert_to_numpy=True,
            )
            results.extend(vec.tolist() for vec in encoded)
        return results
