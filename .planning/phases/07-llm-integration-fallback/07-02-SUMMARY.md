---
phase: 07-llm-integration-fallback
plan: "02"
subsystem: llm
tags: [fallback-chain, groq, openai, ollama, usage-tracking, agent-nodes]
dependency_graph:
  requires: [07-01]
  provides: [llm.fallback.FallbackClient, llm.fallback.get_llm]
  affects: [agents/nodes/gatekeeper, agents/nodes/query_planner, agents/nodes/sql_generator, agents/nodes/correction_plan, agents/nodes/correction_sql]
tech_stack:
  added: [langchain-ollama>=1.0.1]
  patterns: [Groq->OpenAI->Ollama fallback chain, lazy-import factory pattern, patch('llm.fallback.get_llm') test mock pattern]
key_files:
  created: [llm/fallback.py]
  modified:
    - llm/__init__.py
    - config/config.yaml
    - agents/nodes/gatekeeper.py
    - agents/nodes/query_planner.py
    - agents/nodes/sql_generator.py
    - agents/nodes/correction_plan.py
    - agents/nodes/correction_sql.py
    - tests/llm/test_fallback.py
    - tests/agents/test_gatekeeper.py
    - tests/agents/test_query_planner.py
    - tests/agents/test_sql_generator.py
    - tests/agents/test_correction.py
decisions:
  - "FallbackClient constructed with three LLM instances (not classes) — allows direct injection in tests and clean provider chain"
  - "Existing node tests migrated from sys.modules injection to patch('llm.fallback.get_llm') — simpler, no importlib.reload() needed"
  - "langchain-ollama installed as direct pip install (was in pyproject.toml but missing from environment)"
  - "_groq_exc() and _openai_exc() are functions not tuples — lazy import avoids import-time side effects, required for test isolation"
  - "astream() wraps ainvoke() for Phase 7 — full streaming deferred to Phase 8 where chunked usage_metadata aggregation will be added"
metrics:
  duration_seconds: 504
  completed_date: "2026-03-17"
  tasks_completed: 2
  tasks_total: 2
  files_created: 1
  files_modified: 11
  tests_before: 178
  tests_after: 191
  tests_skipped_before: 13
  tests_skipped_after: 0
---

# Phase 7 Plan 02: FallbackClient Implementation Summary

**One-liner:** Groq->OpenAI->Ollama fallback chain with UsageTracker per-call recording, wired into all 5 agent nodes via get_llm() factory replacing direct ChatGroq instantiation.

---

## What Was Built

### llm/fallback.py
- **FallbackClient** class: three-provider fallback chain (Groq, OpenAI, Ollama), `ainvoke()` tries each in order and returns a structured error dict if all fail (never raises), `astream()` wraps `ainvoke()` for Phase 8 Streamlit interface
- **get_llm()** factory: fresh YAML config read on each call (enables test patching), builds all three LLM instances, selects OpenAI model based on `query_plan.complexity`, returns `FallbackClient` with `UsageTracker`
- Lazy exception helper functions `_groq_exc()` / `_openai_exc()` preserve the project's lazy-import pattern and avoid import-time side effects

### config/config.yaml
Extended `llm:` block from 3 to 8 keys:
- `groq_model: llama-3.3-70b-versatile`
- `openai_model_default: gpt-4o-mini`
- `openai_model_complex: gpt-4o`
- `ollama_model: qwen3:8b`
- `ollama_base_url: http://localhost:11434`

### llm/__init__.py
Added `FallbackClient` and `get_llm` to re-exports and `__all__`.

### Agent Node Migration (5 nodes)
Each of the 5 agent nodes (`gatekeeper`, `query_planner`, `sql_generator`, `correction_plan`, `correction_sql`) had the old two-line `ChatGroq` instantiation:
```python
from langchain_groq import ChatGroq
llm = ChatGroq(model="llama-3.3-70b-versatile", temperature=0, max_tokens=...)
```
replaced with:
```python
from llm.fallback import get_llm
llm = get_llm(node="<node_name>", state=state)
```

### Test Migration (4 test files)
The previous sys.modules injection pattern (`patch.dict(sys.modules, {"langchain_groq": mock})` + `importlib.reload(mod)`) no longer reached the LLM after nodes switched to `get_llm()`. All 4 affected test files migrated to `patch("llm.fallback.get_llm", return_value=mock_llm)` — simpler, no reload required.

### tests/llm/test_fallback.py
All 13 Wave 2 stubs unskipped and implemented:
- Groq primary success path
- Usage metadata extraction via UsageTracker
- 4 Groq exception types trigger OpenAI (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)
- OpenAI fallback success when Groq fails
- Ollama fallback success when both Groq and OpenAI fail
- Complex query selects gpt-4o model, simple query selects gpt-4o-mini
- All-3-fail returns structured error dict with `error_type == "llm_all_providers_failed"`
- Provider name recorded in tracker entries
- `test_cost_unknown_model_is_zero` also unskipped (4th cost test)

---

## Test Results

| Suite | Before | After |
|-------|--------|-------|
| tests/llm/ | 3 passed, 13 skipped | **16 passed, 0 skipped** |
| tests/agents/ | 71 passed, 0 skipped | **71 passed, 0 skipped** |
| Full suite | 178 passed, 13 skipped | **191 passed, 0 skipped, 0 failed** |

---

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] langchain-ollama missing from Python environment**
- **Found during:** Task 2 (first run of tests/agents/ after node wiring)
- **Issue:** `pyproject.toml` lists `langchain-ollama>=1.0.1` as a core dependency but it was not installed in the active environment — `ModuleNotFoundError: No module named 'langchain_ollama'`
- **Fix:** `pip install langchain-ollama>=1.0.1` — installed version 1.0.1
- **Files modified:** None (environment change only)

**2. [Rule 1 - Pattern Migration] Existing node tests used sys.modules injection for langchain_groq**
- **Found during:** Task 2 after node wiring
- **Issue:** After nodes switched to `get_llm()`, the old `patch.dict(sys.modules, {"langchain_groq": mock})` pattern no longer intercepted LLM construction. Tests then called `get_llm()` which tried to instantiate real OpenAI/Groq clients and failed with API key errors.
- **Fix:** Migrated all 4 affected node test files to `patch("llm.fallback.get_llm", return_value=mock_llm)` pattern — cleaner, no `importlib.reload()` needed
- **Files modified:** tests/agents/test_gatekeeper.py, test_query_planner.py, test_sql_generator.py, test_correction.py

---

## Requirements Satisfied

- **LLM-001:** Groq primary path works, ainvoke with usage_metadata extraction — SATISFIED
- **LLM-002:** OpenAI fallback on Groq errors, Ollama fallback on OpenAI errors, all-fail returns error dict — SATISFIED
- **LLM-003:** calculate_cost(), UsageTracker, per-call usage_metadata accumulation — SATISFIED (Wave 1 + Wave 2)

---

## Self-Check: PASSED

All files exist: llm/fallback.py, config/config.yaml, llm/__init__.py, tests/llm/test_fallback.py, all 5 agent nodes
All commits exist: 0ebb48c (Task 1), 34c463f (Task 2)
Full test suite: 191 passed, 0 skipped, 0 failed
