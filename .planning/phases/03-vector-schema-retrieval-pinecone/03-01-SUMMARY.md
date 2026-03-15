---
phase: 03-vector-schema-retrieval-pinecone
plan: "01"
subsystem: vector
tags: [embeddings, schema-graph, pinecone, bge, sentence-transformers, tdd]
dependency_graph:
  requires: []
  provides: [vector/embeddings.py, vector/schema_graph.py]
  affects: [vector/retriever.py, vector/pinecone_store.py]
tech_stack:
  added: [sentence-transformers>=5.0.0, pinecone>=5.1.0, chromadb>=0.5.0]
  patterns: [lazy-import, lru_cache, tdd-red-green, batched-encoding]
key_files:
  created:
    - pyproject.toml (vector extras group added)
    - vector/embeddings.py
    - vector/schema_graph.py
    - tests/vector/__init__.py
    - tests/vector/conftest.py
    - tests/vector/test_embeddings.py
    - tests/vector/test_schema_graph.py
  modified:
    - pyproject.toml
decisions:
  - "pinecone-client removed from core deps; pinecone>=5.1.0 placed in vector optional extras only — avoids import errors when vector deps not installed"
  - "EmbeddingGenerator lazy-loads SentenceTransformer inside _get_model() — follows project lazy-import pattern for optional extras"
  - "lru_cache on embed_query_cached requires __hash__=id(self) and __eq__=is — Python requirement for caching on instance methods"
  - "SchemaGraph expand_tables follows forward-only FK direction — avoids context explosion from high-fan-in tables like Customer"
  - "embed_documents uses show_progress_bar=False with manual INFO logging — consistent with project logger pattern"
metrics:
  duration_seconds: 165
  completed_date: "2026-03-14"
  tasks_completed: 3
  tasks_total: 3
  files_created: 7
  files_modified: 1
  tests_added: 18
  tests_total: 59
---

# Phase 3 Plan 01: Embedding Generation and FK Schema Graph Summary

**One-liner:** BGE embedding layer with lazy model loading and lru-cached query embeddings, plus FK adjacency graph with 1-hop expansion and INNER JOIN hint generation.

---

## Tasks Completed

| # | Task | Commit | Files |
|---|------|--------|-------|
| 1 | Fix pyproject.toml deps and create test scaffold | 6816bb8 | pyproject.toml, tests/vector/__init__.py, tests/vector/conftest.py |
| 2 | Implement EmbeddingGenerator and text builders (TDD) | d8b9145 | vector/embeddings.py, tests/vector/test_embeddings.py |
| 3 | Implement SchemaGraph with FK expansion and JOIN hints (TDD) | 73be607 | vector/schema_graph.py, tests/vector/test_schema_graph.py |

---

## What Was Built

### pyproject.toml
- Removed `pinecone-client>=3.0.0,<5.0.0` from core `[project] dependencies`
- Added `vector` optional extras group with `pinecone>=5.1.0`, `sentence-transformers>=5.0.0,<6.0.0`, `chromadb>=0.5.0`

### vector/embeddings.py
- `QUERY_INSTRUCTION` module constant for BGE instruction prefix
- `build_table_text(name, table)`: formats SchemaTable with columns, FK references, first sample row
- `build_column_text(col_name, col_type, table_name, sample_values)`: formats column info with up to 3 sample values
- `EmbeddingGenerator` class:
  - `_get_model()`: lazy SentenceTransformer load with INFO log before download
  - `embed_query()`: uses `encode_query()` with `encode()` fallback on AttributeError
  - `embed_query_cached()`: `@lru_cache(maxsize=10_000)`, returns `tuple`
  - `embed_documents()`: batches in chunks of 50, logs progress, returns `list[list[float]]`

### vector/schema_graph.py
- `SchemaGraph` class:
  - `__init__`: builds `_adj` dict from `FKInfo` data as `(fk_col, ref_table, ref_col)` tuples
  - `expand_tables()`: forward-only 1-hop expansion, returns sorted list
  - `generate_join_hints()`: produces `{"from", "to", "on", "type": "INNER"}` dicts

### tests/vector/
- `conftest.py`: 3 Chinook-like fixtures (Customer, Invoice, InvoiceLine with realistic FK relationships)
- `test_embeddings.py`: 10 tests covering text formatting, lazy load, caching, batching, logging
- `test_schema_graph.py`: 8 tests covering adjacency construction, 1-hop expansion, JOIN hint format, empty schema

---

## Test Results

- **Before:** 41 passed, 0 failed
- **After:** 59 passed, 0 failed (+18 new vector tests)
- All VECTOR-002 and VECTOR-003 requirements satisfied

---

## Deviations from Plan

None - plan executed exactly as written.

---

## Self-Check: PASSED

- vector/embeddings.py: FOUND
- vector/schema_graph.py: FOUND
- tests/vector/test_embeddings.py: FOUND
- tests/vector/test_schema_graph.py: FOUND
- Commit 6816bb8: FOUND
- Commit d8b9145: FOUND
- Commit 73be607: FOUND
