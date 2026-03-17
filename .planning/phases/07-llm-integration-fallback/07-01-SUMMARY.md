---
phase: 07-llm-integration-fallback
plan: 01
subsystem: llm
tags: [groq, openai, ollama, langchain-groq, langchain-openai, langchain-ollama, usage-tracking, cost-calculation, agentstate]

# Dependency graph
requires:
  - phase: 06-error-correction-loop
    provides: AgentState with 19 fields (correction_plan, sql_history added); correction loop nodes operational
provides:
  - AgentState with usage_metadata Optional[list] field (20 fields total)
  - llm/usage_tracker.py with COST_TABLE, calculate_cost(), UsageTracker class
  - llm/groq_client.py with _make_groq_llm(cfg) factory
  - llm/openai_client.py with _make_openai_llm(cfg, complexity) factory
  - 16-test scaffold in tests/llm/test_fallback.py (3 active, 13 skipped for Wave 2)
affects: [07-02-llm-integration-fallback, future-phases]

# Tech tracking
tech-stack:
  added: [langchain-groq>=0.1.0, langchain-openai>=0.1.0, langchain-ollama>=1.0.1]
  patterns:
    - Lazy imports inside factory function bodies (ChatGroq, ChatOpenAI) — avoids import-time side effects, enables sys.modules injection in tests
    - COST_TABLE dict with (input_rate, output_rate) tuples per-1k-tokens — extensible pricing table
    - Wave-based TDD stubs with pytest.skip("Wave 2: ...") — keeps suite green while establishing contracts for Wave 2 implementor
    - Atomic AgentState field expansion — state, conftest, and test assertions updated together to avoid broken intermediate states

key-files:
  created:
    - llm/usage_tracker.py
    - llm/groq_client.py
    - llm/openai_client.py
    - tests/llm/__init__.py
    - tests/llm/test_fallback.py
  modified:
    - graph/state.py
    - tests/agents/conftest.py
    - tests/graph/test_state.py
    - llm/__init__.py
    - pyproject.toml

key-decisions:
  - "langchain-groq, langchain-openai, langchain-ollama added to core deps (not extras) — FallbackClient is core functionality, not optional"
  - "langchain-ollama>=1.0.1 used instead of langchain-community — dedicated package (Dec 2025) avoids community bundle bloat"
  - "ChatGroq and ChatOpenAI imports are lazy (inside factory function bodies) — follows established sys.modules injection pattern for test mocking"
  - "usage_metadata field placed between sql_history and retry_count — maintains logical grouping: sql_history (correction audit), usage_metadata (LLM cost audit), retry_count (control flow)"
  - "test_cost_unknown_model_is_zero is a skipped stub (not active) — Wave 2 will validate boundary behavior together with FallbackClient tests"

patterns-established:
  - "Factory function pattern: _make_groq_llm(cfg) and _make_openai_llm(cfg, complexity) return configured LLM instances from config dict"
  - "UsageTracker.record() mutates state['usage_metadata'] in-place via list concatenation (not append) for immutability safety"
  - "COST_TABLE[(input_per_1k, output_per_1k)] structure — consistent with per-1k billing model of Groq and OpenAI APIs"

requirements-completed: [LLM-001, LLM-003]

# Metrics
duration: 6min
completed: 2026-03-16
---

# Phase 7 Plan 01: LLM Integration Foundation Summary

**20-field AgentState with usage_metadata, COST_TABLE-backed calculate_cost() for Groq/OpenAI/Ollama pricing, and lazy-import factory functions for ChatGroq and ChatOpenAI — Wave 1 foundation ready for FallbackClient in Plan 02**

## Performance

- **Duration:** 6 min
- **Started:** 2026-03-16T05:11:00Z
- **Completed:** 2026-03-16T05:17:00Z
- **Tasks:** 3
- **Files modified:** 10

## Accomplishments
- AgentState expanded from 19 to 20 fields atomically (state.py, conftest.py, test_state.py updated together)
- usage_tracker.py with COST_TABLE, calculate_cost(), and UsageTracker class — correct pricing for llama-3.3-70b-versatile ($0.00138/2k tokens), gpt-4o-mini ($0.00075/2k tokens), gpt-4o ($0.0125/2k tokens), Ollama ($0.00)
- Leaf client factories _make_groq_llm() and _make_openai_llm() with lazy imports — ready for FallbackClient composition in Plan 02
- 16-test scaffold in tests/llm/ — 3 cost tests active and passing, 13 Wave 2 stubs skipped; full suite 178 passed, 13 skipped, 0 failed

## Task Commits

Each task was committed atomically:

1. **Task 1: Expand AgentState and fix test fixtures atomically** - `34aac25` (feat)
2. **Task 2: Implement usage_tracker.py and leaf client factories** - `69e5fdb` (feat)
3. **Task 3: Create test scaffold — 16 stubs in tests/llm/test_fallback.py** - `eb19e0f` (test)

**Plan metadata:** (docs commit — see state updates)

## Files Created/Modified
- `graph/state.py` - Added usage_metadata: Optional[list] as 20th field with comment documenting entry schema
- `tests/agents/conftest.py` - make_agent_state() updated to 20 fields; usage_metadata=None added
- `tests/graph/test_state.py` - EXPECTED_FIELDS set updated; len==19 assertion changed to len==20
- `llm/usage_tracker.py` - COST_TABLE dict, calculate_cost() function, UsageTracker in-memory accumulator class
- `llm/groq_client.py` - _make_groq_llm(cfg) factory with lazy ChatGroq import
- `llm/openai_client.py` - _make_openai_llm(cfg, complexity) factory with lazy ChatOpenAI import
- `llm/__init__.py` - Re-exports COST_TABLE, calculate_cost, UsageTracker
- `tests/llm/__init__.py` - Empty package marker
- `tests/llm/test_fallback.py` - 16 tests: 3 cost tests active, 13 Wave 2 stubs skipped
- `pyproject.toml` - langchain-groq, langchain-openai, langchain-ollama>=1.0.1 added to core deps

## Decisions Made
- langchain-groq, langchain-openai, langchain-ollama added to core deps (not extras) — FallbackClient is core functionality, not optional
- langchain-ollama>=1.0.1 (dedicated package, Dec 2025) used instead of langchain-community to avoid bundle bloat
- ChatGroq and ChatOpenAI imports are lazy (inside factory function bodies) — follows established sys.modules injection pattern for test mocking
- usage_metadata field placed between sql_history and retry_count for logical grouping
- test_cost_unknown_model_is_zero kept as skipped stub — Wave 2 will validate boundary behavior together with FallbackClient tests; the unknown-model path returns 0.0 which is already covered by COST_TABLE.get() default

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Added test_cost_unknown_model_is_zero as 16th stub to match plan's 16-test count**
- **Found during:** Task 3 (test scaffold creation)
- **Issue:** Plan specified 16 test stubs (13 skipped + 3 active) but the explicitly listed stubs in the plan body only totalled 15. The behavior section of Task 2 listed 5 cost-related assertions including unknown model.
- **Fix:** Added test_cost_unknown_model_is_zero as a skipped stub (Wave 2) to reach 16 total tests matching the plan's must_haves truth
- **Files modified:** tests/llm/test_fallback.py
- **Verification:** pytest tests/llm/ shows 3 passed, 13 skipped; total 16 collected
- **Committed in:** eb19e0f (Task 3 commit)

---

**Total deviations:** 1 auto-fixed (missing stub to match plan's 16-test count specification)
**Impact on plan:** Minor — adds one Wave 2 stub for boundary behavior validation. No scope creep.

## Issues Encountered
None - all three tasks executed cleanly on first attempt.

## User Setup Required
None - no external service configuration required for Wave 1 foundation. API keys (GROQ_API_KEY, OPENAI_API_KEY) will be needed when FallbackClient is implemented in Plan 02.

## Next Phase Readiness
- Wave 1 foundation complete — all contracts established for Plan 02 FallbackClient
- llm/usage_tracker.py and leaf client factories ready for FallbackClient import
- 13 Wave 2 stubs in tests/llm/test_fallback.py ready to be unskipped in Plan 02
- LLM-001 (Groq primary path) and LLM-003 (cost calculation) requirements satisfied; LLM-002 (fallback chain) implemented in Plan 02

---
*Phase: 07-llm-integration-fallback*
*Completed: 2026-03-16*

## Self-Check: PASSED

- graph/state.py: FOUND
- llm/usage_tracker.py: FOUND
- llm/groq_client.py: FOUND
- llm/openai_client.py: FOUND
- tests/llm/__init__.py: FOUND
- tests/llm/test_fallback.py: FOUND
- .planning/phases/07-llm-integration-fallback/07-01-SUMMARY.md: FOUND
- Commit 34aac25: FOUND (feat: AgentState expansion)
- Commit 69e5fdb: FOUND (feat: usage_tracker and leaf clients)
- Commit eb19e0f: FOUND (test: 16-stub scaffold)
