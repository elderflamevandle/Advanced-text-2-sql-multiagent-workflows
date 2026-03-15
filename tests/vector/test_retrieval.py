"""Unit tests for BaseRetriever, PineconeRetriever, ChromaRetriever, get_retriever (VECTOR-001, VECTOR-003)."""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from vector.retriever import BaseRetriever, ChromaRetriever, PineconeRetriever, get_retriever


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_pinecone_module(has_index_return: bool = False) -> tuple[ModuleType, MagicMock]:
    """Build a fake `pinecone` module and return (module, mock_index)."""
    mock_index = MagicMock()
    mock_index.describe_index_stats.return_value = {"namespaces": {}}
    mock_index.query.return_value = {"matches": []}
    mock_index.upsert.return_value = None

    mock_pc_instance = MagicMock()
    mock_pc_instance.has_index.return_value = has_index_return
    mock_pc_instance.Index.return_value = mock_index

    MockPinecone = MagicMock(return_value=mock_pc_instance)
    MockServerlessSpec = MagicMock()

    fake_module = ModuleType("pinecone")
    fake_module.Pinecone = MockPinecone  # type: ignore[attr-defined]
    fake_module.ServerlessSpec = MockServerlessSpec  # type: ignore[attr-defined]

    return fake_module, mock_index, mock_pc_instance, MockPinecone, MockServerlessSpec


def _make_pinecone_retriever(has_index: bool = True) -> tuple[PineconeRetriever, Any, Any]:
    """Construct a PineconeRetriever with fully mocked SDK."""
    fake_mod, mock_index, mock_pc, MockPinecone, MockSpec = _fake_pinecone_module(has_index)
    with patch.dict(sys.modules, {"pinecone": fake_mod}), \
         patch.dict("os.environ", {"PINECONE_API_KEY": "test-key"}):
        with patch("vector.embeddings.EmbeddingGenerator.__init__", return_value=None):
            retriever = PineconeRetriever()
    retriever._index = mock_index
    retriever._pc = mock_pc
    retriever._embedder = MagicMock()
    retriever._embedder.embed_query_cached.return_value = tuple([0.1] * 1024)
    retriever._embedder.embed_documents.return_value = [[0.1] * 1024]
    return retriever, mock_pc, MockPinecone


def _fake_chromadb_module(mock_client: MagicMock) -> ModuleType:
    """Build a fake `chromadb` module backed by mock_client."""
    fake_mod = ModuleType("chromadb")
    fake_mod.PersistentClient = MagicMock(return_value=mock_client)  # type: ignore[attr-defined]
    return fake_mod


def _make_chroma_retriever() -> tuple[ChromaRetriever, MagicMock]:
    """Construct a ChromaRetriever with a mocked chromadb client."""
    mock_client = MagicMock()
    mock_client.list_collections.return_value = []
    fake_mod = _fake_chromadb_module(mock_client)
    with patch.dict(sys.modules, {"chromadb": fake_mod}), \
         patch("vector.embeddings.EmbeddingGenerator.__init__", return_value=None):
        retriever = ChromaRetriever()
    retriever._embedder = MagicMock()
    retriever._embedder.embed_query_cached.return_value = tuple([0.1] * 1024)
    retriever._embedder.embed_documents.return_value = [[0.1] * 1024]
    return retriever, mock_client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBaseRetrieverIsAbstract:
    def test_base_retriever_is_abstract(self):
        with pytest.raises(TypeError):
            BaseRetriever()  # type: ignore[abstract]


class TestFactory:
    def test_factory_no_api_key(self, monkeypatch):
        monkeypatch.delenv("PINECONE_API_KEY", raising=False)
        mock_client = MagicMock()
        mock_client.list_collections.return_value = []
        fake_chroma = _fake_chromadb_module(mock_client)
        with patch.dict(sys.modules, {"chromadb": fake_chroma}), \
             patch("vector.embeddings.EmbeddingGenerator.__init__", return_value=None):
            result = get_retriever()
        assert isinstance(result, ChromaRetriever)

    def test_factory_with_api_key(self, monkeypatch):
        monkeypatch.setenv("PINECONE_API_KEY", "test-key")
        fake_mod, mock_index, mock_pc, MockPinecone, MockSpec = _fake_pinecone_module(has_index_return=True)
        with patch.dict(sys.modules, {"pinecone": fake_mod}), \
             patch("vector.embeddings.EmbeddingGenerator.__init__", return_value=None):
            result = get_retriever()
        assert isinstance(result, PineconeRetriever)


class TestPineconeIndexCreation:
    def test_pinecone_index_creation(self, monkeypatch):
        """PineconeRetriever.__init__ calls create_index when has_index() returns False."""
        monkeypatch.setenv("PINECONE_API_KEY", "test-key")
        fake_mod, mock_index, mock_pc, MockPinecone, MockSpec = _fake_pinecone_module(has_index_return=False)
        with patch.dict(sys.modules, {"pinecone": fake_mod}), \
             patch("vector.embeddings.EmbeddingGenerator.__init__", return_value=None):
            PineconeRetriever()
        mock_pc.create_index.assert_called_once()
        call_kwargs = mock_pc.create_index.call_args
        assert call_kwargs.kwargs.get("dimension") == 1024 or call_kwargs.args[1] == 1024 or \
               any(1024 in str(v) for v in call_kwargs.kwargs.values())
        assert "cosine" in str(call_kwargs)

    def test_pinecone_index_already_exists(self, monkeypatch):
        """PineconeRetriever.__init__ does NOT call create_index when has_index() is True."""
        monkeypatch.setenv("PINECONE_API_KEY", "test-key")
        fake_mod, mock_index, mock_pc, MockPinecone, MockSpec = _fake_pinecone_module(has_index_return=True)
        with patch.dict(sys.modules, {"pinecone": fake_mod}), \
             patch("vector.embeddings.EmbeddingGenerator.__init__", return_value=None):
            PineconeRetriever()
        mock_pc.create_index.assert_not_called()


class TestPineconeNamespaceExists:
    def test_namespace_exists_true(self):
        retriever, mock_pc, _ = _make_pinecone_retriever(has_index=True)
        retriever._index.describe_index_stats.return_value = {
            "namespaces": {"chinook-sqlite:tables": {}, "chinook-sqlite:columns": {}}
        }
        assert retriever.namespace_exists("chinook-sqlite") is True

    def test_namespace_exists_false(self):
        retriever, mock_pc, _ = _make_pinecone_retriever(has_index=True)
        retriever._index.describe_index_stats.return_value = {"namespaces": {}}
        assert retriever.namespace_exists("chinook-sqlite") is False


class TestEmbedSchemaSkip:
    def test_embed_schema_skips_when_exists(self, sample_schema):
        retriever, mock_pc, _ = _make_pinecone_retriever(has_index=True)
        retriever._index.describe_index_stats.return_value = {
            "namespaces": {"test-ns:tables": {}}
        }
        retriever.embed_schema(sample_schema, "test-ns")
        retriever._embedder.embed_documents.assert_not_called()


class TestRetrieveTwoStage:
    def test_retrieve_tables_two_stage(self, sample_schema):
        retriever, mock_pc, _ = _make_pinecone_retriever(has_index=True)

        # Stage 1 returns Invoice
        stage1_result = {
            "matches": [
                {"metadata": {"table_name": "Invoice", "text": "..."}, "score": 0.9}
            ]
        }
        # Stage 2 returns column matches
        stage2_result = {
            "matches": [
                {"metadata": {"table_name": "Invoice", "column_name": "Total", "data_type": "NUMERIC", "sample_values": "[]"}, "score": 0.8}
            ]
        }
        retriever._index.query.side_effect = [stage1_result, stage2_result]
        retriever._schema_cache = sample_schema

        result = retriever.retrieve_tables("show me invoices", "test-ns", top_k=5)

        assert retriever._index.query.call_count == 2
        first_call = retriever._index.query.call_args_list[0]
        second_call = retriever._index.query.call_args_list[1]
        assert first_call.kwargs.get("namespace") == "test-ns:tables"
        assert second_call.kwargs.get("namespace") == "test-ns:columns"
        assert second_call.kwargs.get("filter") == {"table_name": {"$in": ["Invoice"]}}

    def test_retrieve_tables_includes_fk_expansion(self, sample_schema):
        """Invoice match should expand to include Customer via FK."""
        retriever, mock_pc, _ = _make_pinecone_retriever(has_index=True)

        stage1_result = {
            "matches": [
                {"metadata": {"table_name": "Invoice", "text": "..."}, "score": 0.95}
            ]
        }
        stage2_result = {"matches": []}
        retriever._index.query.side_effect = [stage1_result, stage2_result]
        retriever._schema_cache = sample_schema

        result = retriever.retrieve_tables("invoice totals by customer", "test-ns")
        assert "Invoice" in result["tables"]
        assert "Customer" in result["tables"]

    def test_retrieve_tables_includes_join_hints(self, sample_schema):
        """retrieve_tables() result should include join hints from SchemaGraph."""
        retriever, mock_pc, _ = _make_pinecone_retriever(has_index=True)

        stage1_result = {
            "matches": [
                {"metadata": {"table_name": "Invoice", "text": "..."}, "score": 0.9}
            ]
        }
        stage2_result = {"matches": []}
        retriever._index.query.side_effect = [stage1_result, stage2_result]
        retriever._schema_cache = sample_schema

        result = retriever.retrieve_tables("invoices", "test-ns")
        assert len(result["join_hints"]) > 0
        hint = result["join_hints"][0]
        assert "from" in hint and "to" in hint and "on" in hint and "type" in hint


class TestChromaCollectionName:
    def test_chroma_collection_name_sanitized(self):
        retriever, _ = _make_chroma_retriever()
        name = retriever._collection_name("chinook-sqlite", "tables")
        assert name == "chinook-sqlite_tables"
        assert ":" not in name


class TestVectorIdFormat:
    def test_vector_id_format_tables(self, sample_schema):
        """Table vector IDs must be namespace::table_name."""
        retriever, mock_pc, _ = _make_pinecone_retriever(has_index=True)
        retriever._index.describe_index_stats.return_value = {"namespaces": {}}
        retriever._embedder.embed_documents.side_effect = lambda texts: [[0.1] * 1024 for _ in texts]

        retriever.embed_schema(sample_schema, "mydb")

        upsert_calls = retriever._index.upsert.call_args_list
        table_upsert = next(c for c in upsert_calls if c.kwargs.get("namespace") == "mydb:tables")
        ids = [v["id"] for v in table_upsert.kwargs["vectors"]]
        for tid in ids:
            parts = tid.split("::")
            assert parts[0] == "mydb"
            assert len(parts) == 2

    def test_vector_id_format_columns(self, sample_schema):
        """Column vector IDs must be namespace::table_name::col_name."""
        retriever, mock_pc, _ = _make_pinecone_retriever(has_index=True)
        retriever._index.describe_index_stats.return_value = {"namespaces": {}}
        retriever._embedder.embed_documents.side_effect = lambda texts: [[0.1] * 1024 for _ in texts]

        retriever.embed_schema(sample_schema, "mydb")

        upsert_calls = retriever._index.upsert.call_args_list
        col_upsert = next(c for c in upsert_calls if c.kwargs.get("namespace") == "mydb:columns")
        ids = [v["id"] for v in col_upsert.kwargs["vectors"]]
        for cid in ids:
            parts = cid.split("::")
            assert parts[0] == "mydb"
            assert len(parts) == 3
