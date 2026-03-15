---
phase: 04-specialized-agent-nodes
plan: 01
subsystem: agent-nodes
tags: [gatekeeper, schema-linker, langgraph, llm, tdd, agent-001, agent-002, llm-003]
dependency_graph:
  requires: [graph/state.py, graph/conditions.py, vector/retriever.py]
  provides: [agents/nodes/gatekeeper.py, agents/nodes/schema_linker.py]
  affects: [graph/builder.py routing behavior]
tech_stack:
  added: [langchain_groq.ChatGroq, langchain_core.messages.SystemMessage/HumanMessage]
  patterns: [lazy-import inside function body, sys.modules injection for mocking, TDD RED/GREEN]
key_files:
  created:
    - agents/nodes/gatekeeper.py
    - agents/nodes/schema_linker.py
    - tests/agents/__init__.py
    - tests/agents/conftest.py
    - tests/agents/test_gatekeeper.py
    - tests/agents/test_schema_linker.py
  modified:
    - graph/state.py
    - graph/conditions.py
    - tests/graph/test_state.py
    - tests/graph/conftest.py
decisions:
  - sys.modules injection for langchain_groq/vector.retriever mocks — lazy imports inside function bodies make patch() fail; same pattern used for chromadb/pinecone in Phase 3
  - importlib.reload() in gatekeeper tests — ensures fresh module state after sys.modules injection so lazy import picks up mock
  - Destructive NL check runs before LLM call — saves token cost and prevents any LLM-assisted bypass
  - resolved_query stored in state not overwriting user_query — preserves original for audit trail; schema_linker prefers resolved_query via `or` fallback
  - route_after_gatekeeper returns only schema_linker or formatter — maps exactly to builder.py path dict keys; follow_up treated same as sql (needs schema lookup)
metrics:
  duration: ~30 minutes
  completed_date: "2026-03-15"
  tasks_completed: 3
  files_created: 6
  files_modified: 4
  tests_added: 10
  total_tests: 83
---

# Phase 4 Plan 01: Gatekeeper and Schema Linker Nodes Summary

Implemented LLM-powered gatekeeper (4-category classification, follow-up rewrite, NL safety) and vector-retriever-backed schema linker as the input-side nodes of the pipeline, with 10 new tests all passing and zero regressions.

## What Was Built

### AgentState Expansion (graph/state.py)
- Added `resolved_query: Optional[str]` field (14 fields total, after `user_query`)
- Updated `make_initial_state` in `tests/graph/conftest.py` to include the new field

### Routing Update (graph/conditions.py)
- `route_after_gatekeeper` now handles 4 categories: `sql`/`follow_up` → `"schema_linker"`, `conversational`/`ambiguous` → `"formatter"`
- Returns only values present in builder.py path map

### Gatekeeper Node (agents/nodes/gatekeeper.py)
- Module-level `_GATEKEEPER_PROMPT` constant (LLM-003 requirement)
- Module-level `_DESTRUCTIVE_PATTERNS` list for NL safety
- `gatekeeper_node` flow: db_manager guard → destructive NL check → ChatGroq classify → follow_up rewrite → return partial state
- Uses `ChatGroq(model="llama-3.3-70b-versatile", temperature=0, max_tokens=1024)`
- `_parse_json_response` helper strips markdown fences, falls back to sql category on parse error

### Schema Linker Node (agents/nodes/schema_linker.py)
- Prefers `resolved_query` over `user_query` for retrieval
- Lazy imports `get_retriever` from `vector.retriever`
- Narrows full schema to retrieved tables; falls back to full schema if narrowed is empty
- Exception handler falls back to full schema with all table keys

### Test Infrastructure
- `tests/agents/__init__.py`: package marker
- `tests/agents/conftest.py`: `make_agent_state()` helper with 14-field complete state, non-None db_manager default

## Test Results

| Suite | Tests | Result |
|---|---|---|
| tests/graph/test_state.py | 5 | PASSED |
| tests/agents/test_gatekeeper.py | 7 | PASSED |
| tests/agents/test_schema_linker.py | 3 | PASSED |
| Full suite | 83 | 83 passed, 0 failed |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] sys.modules injection required for lazy-import mocking**
- **Found during:** Task 2 (GREEN phase)
- **Issue:** `patch("agents.nodes.gatekeeper.ChatGroq")` raises `AttributeError` because ChatGroq is imported lazily inside the function body — the name is never bound at module level, making `patch()` unable to find it
- **Fix:** Used `patch.dict(sys.modules, {"langchain_groq": mock_module})` with `importlib.reload()` to force the lazy import to pick up the mock. Same pattern already established in Phase 3 for chromadb/pinecone.
- **Files modified:** `tests/agents/test_gatekeeper.py`
- **Commit:** ff088fe

None for schema linker — plan executed exactly as written.

## Decisions Made

1. **sys.modules injection + importlib.reload()** — lazy imports inside function bodies require this approach; `patch()` only works on already-bound module attributes
2. **Destructive NL check before LLM call** — saves tokens, prevents any LLM-assisted workaround
3. **resolved_query uses `or` fallback** — `state.get("resolved_query") or state.get("user_query")` cleanly handles None and empty string
4. **route_after_gatekeeper: follow_up → schema_linker** — follow-up queries after rewrite need schema lookup before SQL generation

## Self-Check: PASSED

All created files exist. All 3 task commits confirmed (f8d9e93, ff088fe, ca2f617). Full test suite: 83 passed, 0 failed.
