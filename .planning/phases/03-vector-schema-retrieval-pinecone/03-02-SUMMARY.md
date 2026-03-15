---
phase: 03-vector-schema-retrieval-pinecone
plan: "02"
subsystem: vector
tags: [retrieval, pinecone, chromadb, two-stage, fk-expansion, abc]
dependency_graph:
  requires: ["03-01"]
  provides: ["vector/retriever.py", "config/pinecone_config.yaml", "tests/vector/test_retrieval.py"]
  affects: ["Phase 4 schema_linker_node"]
tech_stack:
  added: []
  patterns:
    - "BaseRetriever ABC — enforces embed_schema/retrieve_tables/namespace_exists contracts across backends"
    - "Lazy import of pinecone/chromadb inside __init__ — optional extras never imported at module level"
    - "sys.modules injection in tests — mocks uninstalled optional packages without ModuleNotFoundError"
    - "Two-stage retrieval: table namespace first, then column namespace filtered to matched tables"
    - "Schema cache (self._schema_cache) stored during embed_schema for FK expansion in retrieve_tables"
key_files:
  created:
    - vector/retriever.py
    - config/pinecone_config.yaml
    - tests/vector/test_retrieval.py
  modified:
    - vector/__init__.py
decisions:
  - "Lazy import pinecone/chromadb inside __init__ — keeps optional extras from causing ImportError at package import time"
  - "schema_cache stored on instance during embed_schema — cleaner than passing schema to every retrieve_tables call"
  - "ChromaRetriever._collection_name sanitizes colons to underscores — ChromaDB rejects colons in collection names"
  - "sys.modules injection for chromadb/pinecone mocks — both are uninstalled optional extras; patch.dict(sys.modules) is the correct approach"
  - "get_retriever() factory checks os.getenv not os.environ[] — avoids KeyError when PINECONE_API_KEY absent"
metrics:
  duration_seconds: 166
  completed_date: "2026-03-14"
  tasks_completed: 2
  tasks_total: 2
  files_created: 3
  files_modified: 1
---

# Phase 3 Plan 02: BaseRetriever with Pinecone and ChromaDB Backends Summary

**One-liner:** Two-stage vector retrieval (table then column namespace) with FK expansion via BaseRetriever ABC supporting Pinecone serverless and ChromaDB local backends.

---

## What Was Built

### vector/retriever.py (257 lines)

**BaseRetriever ABC** — defines three abstract methods:
- `embed_schema(schema, namespace)` — upserts table + column embeddings into the vector store
- `retrieve_tables(query, namespace, top_k)` — returns FK-expanded tables + column metadata + join hints
- `namespace_exists(namespace)` — skips re-embedding if already indexed

**PineconeRetriever** — serverless index backend:
- `_ensure_index()` creates index on first use (dimension=1024, metric=cosine, cloud=aws/us-east-1)
- `embed_schema()` skips when namespace exists; upserts to `namespace:tables` and `namespace:columns` sub-namespaces
- `retrieve_tables()` two-stage: query `:tables` first, then query `:columns` with `filter={"table_name": {"$in": ...}}`
- FK expansion via SchemaGraph when `_schema_cache` is set
- Vector IDs: `namespace::table_name` for tables, `namespace::table_name::col_name` for columns

**ChromaRetriever** — local PersistentClient fallback:
- Collections stored at `~/.text2sql/chroma/`
- `_collection_name()` sanitizes colons to underscores for ChromaDB compatibility
- Same two-stage logic and FK expansion as PineconeRetriever

**get_retriever()** factory — returns PineconeRetriever when PINECONE_API_KEY is set, ChromaRetriever otherwise.

### config/pinecone_config.yaml

```yaml
index_name: text2sql-schema
dimension: 1024
metric: cosine
cloud: aws
region: us-east-1
top_k: 5
```

### vector/__init__.py (updated)

Re-exports: `BaseRetriever`, `get_retriever`, `EmbeddingGenerator`, `build_table_text`, `build_column_text`, `SchemaGraph`.

### tests/vector/test_retrieval.py (14 tests)

All backends fully mocked — no network calls, no model downloads:
- `test_base_retriever_is_abstract` — TypeError on direct instantiation
- `test_factory_no_api_key` / `test_factory_with_api_key` — factory selects backend
- `test_pinecone_index_creation` / `test_pinecone_index_already_exists`
- `test_namespace_exists_true` / `test_namespace_exists_false`
- `test_embed_schema_skips_when_exists` — no embed_documents call
- `test_retrieve_tables_two_stage` — two query calls with correct namespaces and filter
- `test_retrieve_tables_includes_fk_expansion` — Invoice match expands to Customer
- `test_retrieve_tables_includes_join_hints` — hint dict has from/to/on/type keys
- `test_chroma_collection_name_sanitized`
- `test_vector_id_format_tables` / `test_vector_id_format_columns`

---

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] chromadb not installed — sys.modules injection needed**
- **Found during:** Task 2 RED phase (test run)
- **Issue:** `patch("chromadb.PersistentClient", ...)` raises `ModuleNotFoundError: No module named 'chromadb'` because chromadb is an optional extra not installed in dev environment
- **Fix:** Replaced all chromadb patches with `patch.dict(sys.modules, {"chromadb": fake_module})` injection — same pattern already used for pinecone in the plan's recommended approach
- **Files modified:** `tests/vector/test_retrieval.py`
- **Commit:** 8acae0a

---

## Test Results

```
tests/vector/ — 32 passed (18 from Plan 01 + 14 new)
Full suite    — 73 passed, 0 failed, 0 xfailed
```

---

## Requirements Satisfied

- **VECTOR-001:** BaseRetriever ABC + PineconeRetriever + ChromaRetriever + get_retriever factory
- **VECTOR-003:** retrieve_tables integrates FK expansion (SchemaGraph.expand_tables) and JOIN hints (SchemaGraph.generate_join_hints)

---

## Self-Check: PASSED

Files verified present:
- `vector/retriever.py` — FOUND
- `config/pinecone_config.yaml` — FOUND
- `tests/vector/test_retrieval.py` — FOUND
- `vector/__init__.py` — FOUND (modified)

Commits verified:
- `ce05af1` — feat(03-02): add BaseRetriever ABC, PineconeRetriever, ChromaRetriever, get_retriever factory — FOUND
- `8acae0a` — test(03-02): add 14 retrieval unit tests with fully mocked backends — FOUND
