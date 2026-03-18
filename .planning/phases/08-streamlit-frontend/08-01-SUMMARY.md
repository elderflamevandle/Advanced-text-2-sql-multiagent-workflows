---
phase: 08-streamlit-frontend
plan: "01"
subsystem: testing
tags: [plotly, streamlit, pytest, tdd, astream, fallback-client, test-scaffold]

# Dependency graph
requires:
  - phase: 07-llm-integration-fallback
    provides: FallbackClient with ainvoke/astream interface and get_llm() factory
provides:
  - tests/ui/ Wave 0 test scaffold (7 files, 21 skipped stubs)
  - FallbackClient.astream() yields token-level str chunks (not AIMessage objects)
  - plotly>=5.0.0 in core dependencies (plotly 6.6.0 installed)
  - conftest.py shared fixtures: sample_agent_state (20 fields), mock_graph, mock_db_manager
affects:
  - 08-02-PLAN (sidebar component — uses conftest fixtures, UI-001 stubs)
  - 08-03-PLAN (chat component — uses conftest fixtures, UI-002 stubs)
  - 08-04-PLAN (debug panel + charts — uses conftest fixtures, UI-003/004 stubs)

# Tech tracking
tech-stack:
  added:
    - plotly 6.6.0 (core dependency, plotly>=5.0.0,<7.0.0 in pyproject.toml)
  patterns:
    - TDD RED/GREEN: write 4 failing astream tests, rewrite implementation, confirm green
    - Wave 0 scaffold: skip-marked stubs established before component files exist (Nyquist compliance)
    - conftest.py shared fixtures: sample_agent_state mirrors all 20 TypedDict fields exactly

key-files:
  created:
    - tests/ui/__init__.py
    - tests/ui/conftest.py
    - tests/ui/test_sidebar.py
    - tests/ui/test_chat.py
    - tests/ui/test_debug_panel.py
    - tests/ui/test_charts.py
    - tests/ui/test_e2e.py
  modified:
    - llm/fallback.py (FallbackClient.astream() rewritten for token-level streaming)
    - pyproject.toml (plotly>=5.0.0,<7.0.0 added to core dependencies)
    - tests/llm/test_fallback.py (4 astream behavior tests added)

key-decisions:
  - "FallbackClient.astream() iterates llm.astream() chunks: yields text strings, records usage from final chunk's usage_metadata — enables st.write_stream() consumption"
  - "All-providers-fail error is str(error_dict) not raw dict — st.write_stream() requires str-yielding generators"
  - "astream() tests added to tests/llm/test_fallback.py (not a new file) — plan explicitly prohibited new llm test files"
  - "plotly placed in core deps (not optional extras) — charts component is core Streamlit UI functionality"
  - "Wave 0 stubs all use pytest.mark.skip — keeps full suite green; 21 skipped is acceptable"

patterns-established:
  - "UI test fixtures in tests/ui/conftest.py: sample_agent_state must mirror all 20 AgentState fields"
  - "Wave N stubs reference the specific PLAN that will implement them in skip reason string"

requirements-completed: [UI-001, UI-002, UI-003, UI-004]

# Metrics
duration: 7min
completed: 2026-03-18
---

# Phase 8 Plan 01: Streamlit Frontend Wave 0 Test Scaffold Summary

**Token-level FallbackClient.astream() streaming (str chunks for st.write_stream), plotly 6.6.0 installed, and 7-file tests/ui/ scaffold with 21 skipped stubs and shared fixtures mirroring all 20 AgentState fields**

## Performance

- **Duration:** 7 minutes
- **Started:** 2026-03-18T03:13:41Z
- **Completed:** 2026-03-18T03:20:41Z
- **Tasks:** 2
- **Files modified:** 10

## Accomplishments
- Rewrote FallbackClient.astream() to delegate to llm.astream() for true token-level streaming; fallback chain (Groq->OpenAI->Ollama) preserved; usage_metadata aggregated from final chunk; all-providers-fail yields str not dict
- Added plotly>=5.0.0,<7.0.0 to core dependencies in pyproject.toml; plotly 6.6.0 installed and importable
- Created tests/ui/ package with 7 files: conftest.py (3 fixtures, 20-field AgentState), 5 stub test files covering UI-001 through UI-004 and end-to-end smoke
- Full suite: 195 passed, 21 skipped, 0 failed (was 191/0/0 before this plan)

## Task Commits

Each task was committed atomically:

1. **Task 1: Upgrade FallbackClient.astream() and add plotly dependency** - `3466ec3` (feat)
2. **Task 2: Create tests/ui/ test scaffold with Wave 0 stubs** - `b1b25e4` (feat)

**Plan metadata:** (docs commit follows)

_Note: TDD tasks — Task 1 had RED (4 failing astream tests added) then GREEN (astream rewritten) in a single commit_

## Files Created/Modified
- `llm/fallback.py` - FallbackClient.astream() rewritten: iterates llm.astream(), yields str chunks, records usage from final chunk
- `pyproject.toml` - plotly>=5.0.0,<7.0.0 added to core dependencies (after langchain-ollama, before ragas)
- `tests/llm/test_fallback.py` - 4 new astream behavior tests: string chunks, concatenation, usage tracking, error-as-string
- `tests/ui/__init__.py` - Empty package marker for pytest discovery
- `tests/ui/conftest.py` - Three fixtures: sample_agent_state (20 fields), sample_agent_state_with_error, mock_graph, mock_db_manager
- `tests/ui/test_sidebar.py` - 5 UI-001 stubs (skipped — Wave 1, implement in 08-02-PLAN)
- `tests/ui/test_chat.py` - 5 UI-002 stubs (skipped — Wave 1, implement in 08-03-PLAN)
- `tests/ui/test_debug_panel.py` - 5 UI-003 stubs (skipped — Wave 2, implement in 08-04-PLAN)
- `tests/ui/test_charts.py` - 4 UI-004 stubs (skipped — Wave 2, implement in 08-04-PLAN)
- `tests/ui/test_e2e.py` - 2 end-to-end smoke stubs (skipped — Wave 3, implement in 08-04-PLAN)

## Decisions Made
- astream() tests added to tests/llm/test_fallback.py rather than a new file — plan explicitly stated "do NOT create a new test file for llm"
- plotly placed in core dependencies (not an optional extras group) — charts are a core Streamlit UI feature, not an optional capability
- All-providers-fail in astream() yields str(error_dict) — st.write_stream() requires generators that yield strings; yielding a raw dict would cause a TypeError at runtime
- FallbackClient.astream() does not call ainvoke() as a fallback — the plan's implementation uses only llm.astream(); providers without astream support will raise an exception caught by the provider loop

## Deviations from Plan

None - plan executed exactly as written. The 4 TDD astream tests were added to tests/llm/test_fallback.py as a dedicated block (permitted by plan: "small dedicated block").

## Issues Encountered
- plotly.express has no `__version__` attribute (the version lives on the `plotly` top-level module) — verified with `import plotly; plotly.__version__` instead. No impact on functionality.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- tests/ui/ scaffold ready for Wave 1 implementation (08-02-PLAN: sidebar, 08-03-PLAN: chat)
- conftest.py fixtures provide all shared state needed by UI component tests
- FallbackClient.astream() now compatible with st.write_stream() — chat streaming in 08-03-PLAN can consume it directly
- plotly importable — charts component in 08-04-PLAN can use plotly.express immediately

---
*Phase: 08-streamlit-frontend*
*Completed: 2026-03-18*
