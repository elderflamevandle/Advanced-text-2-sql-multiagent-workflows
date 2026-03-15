"""Unit tests for vector/embeddings.py (VECTOR-002)."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from vector.embeddings import EmbeddingGenerator, build_column_text, build_table_text


# ---------------------------------------------------------------------------
# build_table_text tests
# ---------------------------------------------------------------------------

def test_build_table_text_format(sample_schema):
    table = sample_schema["Invoice"]
    text = build_table_text("Invoice", table)
    assert "Table: Invoice." in text
    assert "Columns:" in text
    assert "InvoiceId" in text
    assert "INTEGER" in text
    assert "Foreign keys:" in text
    assert "Customer" in text


def test_build_table_text_no_fks(sample_schema):
    table = sample_schema["Customer"]  # Customer has no outgoing FKs
    text = build_table_text("Customer", table)
    assert "Foreign keys:" not in text


def test_build_table_text_no_samples(sample_schema):
    from database.schema_utils import SchemaTable, ColumnInfo
    table = SchemaTable(
        columns=[ColumnInfo(name="Id", type="INTEGER", nullable=False)],
        primary_keys=["Id"],
        foreign_keys=[],
        sample_rows=[],
    )
    text = build_table_text("Empty", table)
    assert "Sample:" not in text


# ---------------------------------------------------------------------------
# build_column_text tests
# ---------------------------------------------------------------------------

def test_build_column_text_format():
    text = build_column_text("Total", "NUMERIC", "Invoice", [100.0, 200.5])
    assert "Column: Total (NUMERIC) in table Invoice." in text
    assert "100.0" in text
    assert "200.5" in text


def test_build_column_text_max_3_samples():
    text = build_column_text("Total", "NUMERIC", "Invoice", [1.0, 2.0, 3.0, 4.0, 5.0])
    assert "4.0" not in text
    assert "5.0" not in text
    assert "1.0" in text
    assert "3.0" in text


# ---------------------------------------------------------------------------
# EmbeddingGenerator tests
# ---------------------------------------------------------------------------

def test_embedding_generator_lazy_load():
    gen = EmbeddingGenerator()
    assert gen._model is None


def test_embed_query_cached_returns_tuple():
    gen = EmbeddingGenerator()
    mock_model = MagicMock()
    mock_model.encode.return_value = np.array([0.1, 0.2, 0.3])
    # Patch _get_model to avoid downloading real model
    with patch.object(gen, "_get_model", return_value=mock_model):
        result = gen.embed_query_cached("test query")
    assert isinstance(result, tuple)


def test_embed_query_cached_cache_hit():
    gen = EmbeddingGenerator()
    mock_model = MagicMock()
    mock_model.encode.return_value = np.array([0.1, 0.2, 0.3])
    with patch.object(gen, "_get_model", return_value=mock_model):
        # Patch embed_query on the instance to count calls
        original_embed_query = gen.embed_query
        call_count = {"n": 0}

        def counting_embed_query(query):
            call_count["n"] += 1
            return original_embed_query(query)

        gen.embed_query = counting_embed_query
        gen.embed_query_cached.cache_clear()
        gen.embed_query_cached("same query")
        gen.embed_query_cached("same query")
    assert call_count["n"] == 1


def test_embed_documents_batching():
    gen = EmbeddingGenerator()
    mock_model = MagicMock()

    def mock_encode(chunk, **kwargs):
        return np.zeros((len(chunk), 1024))

    mock_model.encode.side_effect = mock_encode
    with patch.object(gen, "_get_model", return_value=mock_model):
        texts = ["text"] * 120
        result = gen.embed_documents(texts)
    assert mock_model.encode.call_count == 3  # 50+50+20
    assert len(result) == 120


def test_embed_documents_progress_logging(caplog):
    gen = EmbeddingGenerator()
    mock_model = MagicMock()

    def mock_encode(chunk, **kwargs):
        return np.zeros((len(chunk), 1024))

    mock_model.encode.side_effect = mock_encode
    with patch.object(gen, "_get_model", return_value=mock_model):
        with caplog.at_level(logging.INFO):
            gen.embed_documents(["text"] * 60)
    assert any("Embedding" in r.message for r in caplog.records)
