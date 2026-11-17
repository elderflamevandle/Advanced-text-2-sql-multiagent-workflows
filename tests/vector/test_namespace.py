"""Namespace derivation and the process-wide retriever/embedder (retrieval namespace fix)."""
from __future__ import annotations

import copy
import re
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

import vector.embeddings as embeddings
import vector.retriever as retriever_module
from vector.namespace import schema_fingerprint, schema_namespace
from vector.retriever import ChromaRetriever, PineconeRetriever, get_retriever
from tests.vector.test_retrieval import (
    _fake_chromadb_module,
    _fake_pinecone_module,
    _make_chroma_retriever,
    _make_pinecone_retriever,
)

OTHER_SCHEMA = {
    "Patient": {
        "columns": [{"name": "PatientId", "type": "INTEGER"}],
        "primary_keys": ["PatientId"], "foreign_keys": [], "sample_rows": [],
    },
    "Visit": {
        "columns": [{"name": "VisitId", "type": "INTEGER"},
                    {"name": "PatientId", "type": "INTEGER"}],
        "primary_keys": ["VisitId"],
        "foreign_keys": [{"column": "PatientId", "references_table": "Patient",
                          "references_column": "PatientId"}],
        "sample_rows": [],
    },
}

# Chroma: 3-63 chars of [A-Za-z0-9._-], alphanumeric at both ends.
_CHROMA_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,61}[A-Za-z0-9]$")


# --- namespace ---------------------------------------------------------------

def test_namespace_format(sample_schema):
    ns = schema_namespace("3f2b8c1e-9a4d-4c55-8e21-0d6f7a9b1c2d", sample_schema)
    assert ns == "3f2b8c1e-9a4d-4c55-8e21-0d6f7a9b1c2d-" + schema_fingerprint(sample_schema)[:12]
    assert schema_namespace(None, sample_schema).startswith("local-")
    assert schema_namespace("", sample_schema).startswith("local-")


def test_namespace_differs_per_connection_and_per_schema(sample_schema):
    assert schema_namespace("a", sample_schema) != schema_namespace("b", sample_schema)
    assert schema_namespace("a", sample_schema) != schema_namespace("a", OTHER_SCHEMA)
    assert schema_namespace(None, sample_schema) != schema_namespace(None, OTHER_SCHEMA)


def test_fingerprint_is_stable_and_ignores_table_order_and_sample_rows(sample_schema):
    reordered = dict(reversed(list(copy.deepcopy(sample_schema).items())))
    reordered["Customer"]["sample_rows"] = [{"CustomerId": 999}]
    assert schema_fingerprint(reordered) == schema_fingerprint(sample_schema)


@pytest.mark.parametrize("change", ["add_column", "retype_column", "add_table", "drop_fk"])
def test_fingerprint_changes_with_structure(sample_schema, change):
    changed = copy.deepcopy(sample_schema)
    if change == "add_column":
        changed["Invoice"]["columns"].append({"name": "Currency", "type": "TEXT", "nullable": True})
    elif change == "retype_column":
        changed["Invoice"]["columns"][3]["type"] = "TEXT"
    elif change == "add_table":
        changed["Refund"] = {"columns": [{"name": "Id", "type": "INTEGER"}]}
    else:
        changed["Invoice"]["foreign_keys"] = []
    assert schema_fingerprint(changed) != schema_fingerprint(sample_schema)


@pytest.mark.parametrize("connection_id", [
    "conn-1",
    "3f2b8c1e-9a4d-4c55-8e21-0d6f7a9b1c2d",
    "tenant:42/conn id#7",
    "__weird__",
    "x" * 200,
    "::",
    "日本語",
])
def test_namespace_is_a_valid_collection_name(sample_schema, connection_id):
    ns = schema_namespace(connection_id, sample_schema)
    for tier in ("tables", "columns"):
        assert _CHROMA_NAME.match(f"{ns}_{tier}"), ns
    assert ".." not in ns


def test_sanitised_ids_do_not_collide(sample_schema):
    assert schema_namespace("a/b", sample_schema) != schema_namespace("a:b", sample_schema)
    assert schema_namespace("a/b", sample_schema) != schema_namespace("a-b", sample_schema)
    long_a, long_b = "x" * 100 + "a", "x" * 100 + "b"
    assert schema_namespace(long_a, sample_schema) != schema_namespace(long_b, sample_schema)


# --- per-namespace schema (FK expansion) ---------------------------------------

def _pinecone_hit(table):
    return [{"matches": [{"metadata": {"table_name": table}, "score": 0.9}]}, {"matches": []}]


def test_two_databases_on_one_retriever_keep_their_own_schema(sample_schema):
    """The retriever is shared, so FK expansion must use the schema of the asked namespace."""
    retriever, _, _ = _make_pinecone_retriever()
    retriever._embedder.embed_documents.side_effect = lambda texts: [[0.1] * 4 for _ in texts]
    ns_a = schema_namespace("conn-a", sample_schema)
    ns_b = schema_namespace("conn-b", OTHER_SCHEMA)
    retriever.embed_schema(sample_schema, ns_a)
    retriever.embed_schema(OTHER_SCHEMA, ns_b)     # used to overwrite the single schema cache

    upserted = {c.kwargs["namespace"] for c in retriever._index.upsert.call_args_list}
    assert upserted == {f"{ns_a}:tables", f"{ns_a}:columns", f"{ns_b}:tables", f"{ns_b}:columns"}

    retriever._index.query.side_effect = _pinecone_hit("Invoice") + _pinecone_hit("Visit")
    result_a = retriever.retrieve_tables("invoices", ns_a)
    result_b = retriever.retrieve_tables("visits", ns_b)

    assert result_a["tables"] == ["Customer", "Invoice"]
    assert result_b["tables"] == ["Patient", "Visit"]
    assert retriever._index.query.call_args_list[0].kwargs["namespace"] == f"{ns_a}:tables"
    assert retriever._index.query.call_args_list[2].kwargs["namespace"] == f"{ns_b}:tables"


def test_fk_expansion_works_when_vectors_already_exist(sample_schema):
    """After a restart the namespace exists; the schema must still be registered."""
    retriever, _, _ = _make_pinecone_retriever()
    retriever._index.describe_index_stats.return_value = {"namespaces": {"ns:tables": {}}}
    retriever.embed_schema(sample_schema, "ns")
    retriever._embedder.embed_documents.assert_not_called()

    retriever._index.query.side_effect = _pinecone_hit("Invoice")
    result = retriever.retrieve_tables("invoices", "ns")
    assert "Customer" in result["tables"]
    assert result["join_hints"]


def test_store_is_asked_once_per_namespace(sample_schema):
    retriever, _, _ = _make_pinecone_retriever()
    retriever._embedder.embed_documents.side_effect = lambda texts: [[0.1] * 4 for _ in texts]
    for _ in range(3):
        retriever.embed_schema(sample_schema, "ns")
    assert retriever._index.describe_index_stats.call_count == 1
    assert retriever._embedder.embed_documents.call_count == 2   # tables + columns, once


def test_retrieved_tables_missing_from_schema_are_dropped(sample_schema):
    retriever, _, _ = _make_pinecone_retriever()
    retriever.register_schema("ns", sample_schema)
    retriever._index.query.side_effect = [
        {"matches": [{"metadata": {"table_name": "Invoice"}, "score": 0.9},
                     {"metadata": {"table_name": "Ghost"}, "score": 0.8}]},
        {"matches": []},
    ]
    assert retriever.retrieve_tables("q", "ns")["tables"] == ["Customer", "Invoice"]


def test_unregistered_namespace_skips_expansion(sample_schema):
    retriever, _, _ = _make_pinecone_retriever()
    retriever._index.query.side_effect = _pinecone_hit("Invoice")
    result = retriever.retrieve_tables("q", "never-registered")
    assert result["tables"] == ["Invoice"] and result["join_hints"] == []


def test_schema_cache_is_bounded(sample_schema, monkeypatch):
    retriever, _, _ = _make_pinecone_retriever()
    monkeypatch.setattr(PineconeRetriever, "MAX_CACHED_SCHEMAS", 3)
    for i in range(10):
        retriever.register_schema(f"ns{i}", sample_schema)
    assert retriever.schema_for("ns0") is None
    assert retriever.schema_for("ns9") is sample_schema
    assert len(retriever._schemas) == 3


def test_chroma_keeps_schema_per_namespace(sample_schema):
    retriever, client = _make_chroma_retriever()
    retriever._embedder.embed_documents.side_effect = lambda texts: [[0.1] * 4 for _ in texts]
    ns_a = schema_namespace("conn-a", sample_schema)
    ns_b = schema_namespace("conn-b", OTHER_SCHEMA)
    retriever.embed_schema(sample_schema, ns_a)
    retriever.embed_schema(OTHER_SCHEMA, ns_b)

    created = {c.args[0] for c in client.get_or_create_collection.call_args_list}
    assert created == {f"{ns_a}_tables", f"{ns_a}_columns", f"{ns_b}_tables", f"{ns_b}_columns"}

    collection = client.get_or_create_collection.return_value
    collection.query.side_effect = [
        {"metadatas": [[{"table_name": "Invoice"}]], "distances": [[0.1]]},
        {"metadatas": [[]]},
    ]
    assert retriever.retrieve_tables("invoices", ns_a)["tables"] == ["Customer", "Invoice"]


# --- process-wide singletons ----------------------------------------------------

def _chroma_patches():
    client = MagicMock()
    client.list_collections.return_value = []
    module = _fake_chromadb_module(client)
    return module


def test_get_retriever_returns_the_same_instance(monkeypatch):
    monkeypatch.delenv("PINECONE_API_KEY", raising=False)
    module = _chroma_patches()
    with patch.dict(sys.modules, {"chromadb": module}), \
         patch("vector.embeddings.EmbeddingGenerator.__init__", return_value=None):
        first = get_retriever()
        second = get_retriever()
    assert first is second
    assert isinstance(first, ChromaRetriever)
    assert module.PersistentClient.call_count == 1, "the client must be created once"


def test_get_retriever_is_built_once_under_concurrency(monkeypatch):
    monkeypatch.delenv("PINECONE_API_KEY", raising=False)
    module = _chroma_patches()
    seen, barrier = [], threading.Barrier(16)

    def worker():
        barrier.wait()
        seen.append(get_retriever())

    with patch.dict(sys.modules, {"chromadb": module}), \
         patch("vector.embeddings.EmbeddingGenerator.__init__", return_value=None):
        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(seen) == 16 and len({id(r) for r in seen}) == 1
    assert module.PersistentClient.call_count == 1


def test_retrievers_are_keyed_by_backend_and_share_one_embedder(monkeypatch):
    chroma_module = _chroma_patches()
    pinecone_module = _fake_pinecone_module(has_index_return=True)[0]
    with patch.dict(sys.modules, {"chromadb": chroma_module, "pinecone": pinecone_module}):
        monkeypatch.delenv("PINECONE_API_KEY", raising=False)
        chroma = get_retriever()
        monkeypatch.setenv("PINECONE_API_KEY", "key-1")
        pinecone = get_retriever()
        assert get_retriever() is pinecone
        monkeypatch.setenv("PINECONE_API_KEY", "key-2")     # another Pinecone project
        other_project = get_retriever()
        monkeypatch.delenv("PINECONE_API_KEY")
        assert get_retriever() is chroma

    assert isinstance(chroma, ChromaRetriever) and isinstance(pinecone, PineconeRetriever)
    assert other_project is not pinecone
    assert chroma._embedder is pinecone._embedder is embeddings.get_embedder()


def test_reset_retrievers_drops_the_cache(monkeypatch):
    monkeypatch.delenv("PINECONE_API_KEY", raising=False)
    with patch.dict(sys.modules, {"chromadb": _chroma_patches()}):
        first = get_retriever()
        retriever_module.reset_retrievers()
        assert get_retriever() is not first


def test_embedder_loads_the_model_once_under_concurrency(monkeypatch):
    loads = []

    class FakeSentenceTransformer:
        def __init__(self, name):
            loads.append(name)

    fake_module = MagicMock()
    fake_module.SentenceTransformer = FakeSentenceTransformer
    barrier = threading.Barrier(8)
    models = []

    def worker():
        barrier.wait()
        models.append(embeddings.get_embedder()._get_model())

    with patch.dict(sys.modules, {"sentence_transformers": fake_module}):
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert loads == ["BAAI/bge-large-en-v1.5"]
    assert len({id(m) for m in models}) == 1
