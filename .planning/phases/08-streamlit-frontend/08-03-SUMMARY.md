---
phase: 08-streamlit-frontend
plan: "03"
subsystem: ui
tags: [streamlit, chat, streaming, hitl, langgraph, fallback-client, tdd, pytest]

# Dependency graph
requires:
  - phase: 08-streamlit-frontend
    plan: "01"
    provides: FallbackClient.astream() yields str chunks for st.write_stream(); tests/ui/ scaffold
  - phase: 08-streamlit-frontend
    plan: "02"
    provides: streamlit_app/app.py with get_graph(), init_session(), reset_session(), sidebar
provides:
  - streamlit_app/components/chat.py: render_chat(), submit_query(), render_hitl_card(),
      _build_initial_state(), _handle_graph_interrupt(), _update_session_cost(),
      _stream_final_answer(), _sync_aiter()
  - 6 passing tests in tests/ui/test_chat.py covering all public API functions
affects:
  - 08-04-PLAN (debug panel + charts — render_chat() calls render_chart_with_toggle() and render_debug_panel())
  - 08-05-PLAN (end-to-end tests — submit_query() is the primary user interaction surface)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "_sync_aiter(): drive async generator synchronously via dedicated event loop for st.write_stream() compatibility"
    - "Lazy Command import inside _process_hitl_decision() — avoids sys.modules pollution from test_hitl.py injection"
    - "SimpleNamespace for session_state mocks — required when code uses attribute assignment (st.session_state.key = val)"
    - "TDD RED/GREEN: test stubs replaced atomically with full assertions; both tasks committed together"

key-files:
  created:
    - streamlit_app/components/__init__.py
    - streamlit_app/components/chat.py
  modified:
    - tests/ui/test_chat.py (5 skipped stubs replaced with 6 real passing tests)

key-decisions:
  - "get_llm(node='chat_stream') used instead of FallbackClient(...) directly — FallbackClient constructor requires groq_llm/openai_llm/ollama_llm/tracker arguments; get_llm() is the proper factory"
  - "Command import is lazy (inside _process_hitl_decision body) — test_hitl.py injects a mock sys.modules['langgraph.types'] that lacks Command; module-level import caused all 6 tests to fail in full suite run"
  - "SimpleNamespace for session_state mocks — st.session_state uses attribute assignment; plain dict raises AttributeError on st.session_state.key = val pattern"
  - "graph accessed via 'from graph.builder import compiled_graph' (not streamlit_app.app.get_graph()) — avoids circular import; compiled_graph is the same MemorySaver-backed singleton"
  - "GraphInterrupt from langgraph.errors at module level is safe — test_hitl.py only mocks langgraph.types, not langgraph.errors"

patterns-established:
  - "SimpleNamespace for Streamlit session_state mocks: any function using st.session_state.attr = val needs SimpleNamespace not plain dict"
  - "Lazy langgraph.types imports: Command must be imported inside function body to survive test_hitl.py sys.modules injection"

requirements-completed: [UI-002]

# Metrics
duration: 8min
completed: 2026-03-18
---

# Phase 8 Plan 03: Chat Interface Summary

**Token-by-token streaming chat interface with FallbackClient.astream() + st.write_stream(), per-node stage labels via st.status, HITL inline approval card, and 6 passing TDD tests**

## Performance

- **Duration:** 8 minutes
- **Started:** 2026-03-18T03:25:41Z
- **Completed:** 2026-03-18T03:33:41Z
- **Tasks:** 2 (TDD RED+GREEN committed together)
- **Files modified:** 3

## Accomplishments
- Created streamlit_app/components/chat.py with all 8 public/private functions: render_chat(), submit_query(), render_hitl_card(), _build_initial_state(), _handle_graph_interrupt(), _update_session_cost(), _stream_final_answer(), _sync_aiter()
- _stream_final_answer() uses get_llm(node='chat_stream') factory to get FallbackClient, then calls astream() with the answer as a HumanMessage, draining it via _sync_aiter() into st.write_stream()
- GraphInterrupt caught in submit_query(), HITL pending stored via _handle_graph_interrupt(), approval card renders inline; Command(resume=decision) resumes graph in _process_hitl_decision()
- Replaced 5 skipped Wave 1 stubs in tests/ui/test_chat.py with 6 real assertions; full suite: 205 passed, 16 skipped, 0 failed

## Task Commits

Each task was committed atomically:

1. **Task 1+2: Implement chat.py and fill test_chat.py stubs (TDD RED+GREEN)** - `af6e55a` (feat)

**Plan metadata:** (docs commit follows)

_Note: Tasks 1 and 2 were combined into a single TDD cycle — tests written alongside implementation_

## Files Created/Modified
- `streamlit_app/components/__init__.py` - Package marker for components subpackage
- `streamlit_app/components/chat.py` - Full chat interface: streaming, HITL card, session helpers, async-to-sync adapter
- `tests/ui/test_chat.py` - 6 real assertions replacing 5 skipped Wave 1 stubs; uses SimpleNamespace for session_state mocking

## Decisions Made
- Used `get_llm(node="chat_stream")` instead of `FallbackClient(node_name="chat_stream")` — the FallbackClient constructor requires `groq_llm`, `openai_llm`, `ollama_llm`, `tracker` arguments; `get_llm()` is the correct factory
- Made `Command` import lazy (inside `_process_hitl_decision`) — `test_hitl.py` injects a minimal `langgraph.types` mock lacking `Command`; module-level import caused all 6 tests to fail when running full suite
- Used `SimpleNamespace` for session_state mocks in tests — Streamlit's `session_state` uses attribute assignment; plain `dict` raises `AttributeError` on `st.session_state.key = val`

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] FallbackClient constructor call fixed to use get_llm() factory**
- **Found during:** Task 1 (Implementation)
- **Issue:** Plan code called `FallbackClient(node_name="chat_stream")` but actual constructor signature requires `groq_llm, openai_llm, ollama_llm, tracker, node_name` — would raise `TypeError` at runtime
- **Fix:** Replaced `FallbackClient(node_name="chat_stream")` with `get_llm(node="chat_stream")` and `patch("streamlit_app.components.chat.get_llm", ...)` in the test
- **Files modified:** streamlit_app/components/chat.py, tests/ui/test_chat.py
- **Verification:** test_stream_final_answer_calls_fallback_client_astream passes; astream() called correctly
- **Committed in:** af6e55a

**2. [Rule 1 - Bug] Command import made lazy to survive test_hitl.py sys.modules pollution**
- **Found during:** Task 2 (full suite verification)
- **Issue:** Module-level `from langgraph.types import Command` failed in full pytest run because `test_hitl.py` injects a minimal mock module for `langgraph.types` that has `interrupt` but not `Command`; this persisted in `sys.modules` for the rest of the session
- **Fix:** Moved `Command` import inside `_process_hitl_decision()` function body (lazy import)
- **Files modified:** streamlit_app/components/chat.py
- **Verification:** Full suite: 205 passed, 16 skipped, 0 failed (was 6 failed before fix)
- **Committed in:** af6e55a

**3. [Rule 1 - Bug] Test session_state mocks converted to SimpleNamespace**
- **Found during:** Task 2 (test execution)
- **Issue:** Tests used `patch("streamlit.session_state", {})` (plain dict); `_handle_graph_interrupt` and `_update_session_cost` use attribute assignment (`st.session_state.hitl_pending = ...`); plain dict raises `AttributeError`
- **Fix:** Replaced `fake_session = {}` with `fake_session = SimpleNamespace(...)` in affected tests; assertions updated to use `.attr` access instead of `["key"]` access
- **Files modified:** tests/ui/test_chat.py
- **Verification:** All 6 tests pass independently and in full suite run
- **Committed in:** af6e55a

---

**Total deviations:** 3 auto-fixed (3 Rule 1 bugs)
**Impact on plan:** All fixes necessary for correctness. No scope creep — all fixes affect only files specified in the plan.

## Issues Encountered
- Python 3.14 `asyncio.iscoroutinefunction` deprecation warnings from langgraph.utils.runnable — pre-existing, not introduced by this plan

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- streamlit_app/components/chat.py ready for Plan 04 (debug panel, charts) — `_render_assistant_extras()` already has graceful ImportError fallback for charts.py and debug_panel.py
- 6 chat tests passing; 16 skipped stubs still waiting for Wave 2 (debug panel, charts, e2e)
- Full suite: 205 passed, 16 skipped, 0 failed

---
*Phase: 08-streamlit-frontend*
*Completed: 2026-03-18*
