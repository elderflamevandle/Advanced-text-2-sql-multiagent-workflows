---
phase: 05-execution-safety-layer
plan: 01
subsystem: database
tags: [safety, sql-scanner, executor, langraph, timeout, structured-errors, yaml-config]

# Dependency graph
requires:
  - phase: 04-specialized-agent-nodes
    provides: "AgentState TypedDict, sql_generator node producing generated_sql"
  - phase: 01-foundation
    provides: "DatabaseManager with execute_query() facade, config/config.yaml"
  - phase: 02-langgraph-core
    provides: "AgentState TypedDict base definition, LangGraph node conventions"

provides:
  - "database/safety.py: scan_sql() keyword scanner blocks all DML/DDL, allows SELECT/WITH"
  - "database/safety.py: audit_blocked_query() structured WARNING log with sql/reason/timestamp"
  - "config/safety_config.yaml: allowed/blocked statement lists, timeout and max_rows config"
  - "agents/nodes/executor.py: full executor with safety gate, LIMIT injection, timeout, structured errors"
  - "graph/state.py: AgentState expanded to 17 fields (sql_explanation, execution_metadata, approval_status added)"

affects:
  - 05-execution-safety-layer
  - 06-correction-loop
  - 07-formatter
  - 08-hitl

# Tech tracking
tech-stack:
  added: [yaml (config loading via yaml.safe_load), concurrent.futures (ThreadPoolExecutor timeout)]
  patterns:
    - "lru_cache on _load_config() for single-file YAML load at module level"
    - "Lazy import of database.safety inside executor_node function body"
    - "ThreadPoolExecutor with future.result(timeout=N) for enforced query timeout"
    - "Structured error dict (error_type, message, dialect, failed_sql, hint) as error_log"
    - "_strip_literals_and_comments() before keyword extraction prevents false positives"

key-files:
  created:
    - database/safety.py
    - config/safety_config.yaml
    - tests/safety/__init__.py
    - tests/safety/test_keyword_scanner.py
    - tests/agents/test_executor.py
  modified:
    - agents/nodes/executor.py
    - graph/state.py
    - config/config.yaml
    - tests/agents/conftest.py
    - tests/graph/conftest.py
    - tests/graph/test_state.py

key-decisions:
  - "Strip string literals and block/line comments before extracting first keyword — prevents 'SELECT updated_at' or 'SELECT ... WHERE action = INSERT' from triggering false positives"
  - "lru_cache(maxsize=1) on _load_config() — YAML loaded once per process; test isolation relies on module reload not config file mutation"
  - "error_log is a structured dict (not a plain string) — allows route_after_executor and correction loop to inspect error_type programmatically"
  - "LIMIT injection uses simple regex search — avoids AST complexity; appends to outermost query only (no subquery injection)"
  - "ThreadPoolExecutor(max_workers=1) with future.result(timeout=N) — standard Python pattern for timeout without asyncio.wait_for complications"
  - "_get_timeout() is a module-level function (not a cached constant) — allows tests to patch it via patch('agents.nodes.executor._get_timeout')"
  - "audit_blocked_query uses logger.warning with %r format — structured context without JSON serialization overhead"
  - "AgentState expanded with sql_explanation (sql_generator output), execution_metadata (timing/rows), approval_status (HITL gate) — all three needed for downstream phase correctness"

patterns-established:
  - "Safety gate pattern: scan_sql() → audit + return error dict if blocked, before any execution"
  - "Structured error return: all executor failures return {'error_type': ..., 'message': 'DIALECT error: ...', 'dialect': ..., 'failed_sql': ..., 'hint': ...}"
  - "Null checks at node entry: missing_sql and no_connection errors short-circuit before any I/O"

requirements-completed: [DB-002, DB-003, SAFETY-001, AGENT-005]

# Metrics
duration: 6min
completed: 2026-03-15
---

# Phase 5 Plan 01: SQL Safety Scanner and Executor Node Summary

**Statement-level SQL safety scanner with regex literal stripping, fully implemented executor node with LIMIT injection and 60s ThreadPoolExecutor timeout, and structured error dict protocol — 39 new tests, 134 total green**

## Performance

- **Duration:** 6 min
- **Started:** 2026-03-15T09:00:42Z
- **Completed:** 2026-03-15T09:06:50Z
- **Tasks:** 2
- **Files modified:** 10

## Accomplishments

- `database/safety.py`: `scan_sql()` strips string literals + block/line comments before extracting the first SQL keyword token, preventing false positives on column names like `updated_at` or string values containing `DROP`; `audit_blocked_query()` logs structured WARNING with sql, reason, timestamp, user_query
- `agents/nodes/executor.py`: full implementation — null checks (missing_sql, no_connection), safety scan gate with audit logging, LIMIT 1000 auto-injection when absent, ThreadPoolExecutor timeout at 60s default, structured error dicts for all failure paths (blocked_query, timeout, syntax_error, column_not_found, unknown)
- `graph/state.py`: AgentState expanded from 14 to 17 fields — `sql_explanation` (from sql_generator), `execution_metadata` (timing/row_count from executor), `approval_status` (HITL gate in Phase 8)

## Task Commits

Each task was committed atomically:

1. **Task 1: Safety scanner module + config + AgentState expansion** - `f91d5c3` (feat)
2. **Task 2: Executor node implementation with structured errors and timeout** - `d33d828` (feat)

_Note: TDD tasks used inline RED-then-GREEN flow within single commits per task_

## Files Created/Modified

- `database/safety.py` - scan_sql() keyword scanner with literal/comment stripping, audit_blocked_query()
- `config/safety_config.yaml` - allowed [SELECT, WITH], blocked 9 DDL/DML types, timeout 60s, max_rows 1000
- `agents/nodes/executor.py` - full executor: null checks, safety gate, LIMIT injection, ThreadPoolExecutor timeout, structured errors
- `graph/state.py` - AgentState 14→17 fields: sql_explanation, execution_metadata, approval_status added
- `config/config.yaml` - query_timeout 30→60, safety section (enabled: true), hitl section (enabled, timeout_seconds, auto_approve_simple)
- `tests/safety/__init__.py` - package marker
- `tests/safety/test_keyword_scanner.py` - 25 tests: SAFETY-001 (allowed/blocked/edge cases/false-positives/comments) and DB-002 (audit logging)
- `tests/agents/test_executor.py` - 14 tests: AGENT-005/DB-002/DB-003 (success, blocked, missing SQL, missing db_manager, dialect prefix, LIMIT injection, exception, timeout)
- `tests/agents/conftest.py` - make_agent_state updated to 17 fields
- `tests/graph/conftest.py` - make_initial_state updated to 17 fields
- `tests/graph/test_state.py` - EXPECTED_FIELDS updated to 17 fields

## Decisions Made

- Strip string literals and block/line comments before keyword extraction — "SELECT updated_at FROM logs" is safe (not UPDATE); "SELECT * FROM t WHERE msg = 'DROP TABLE'" is safe (not DROP)
- error_log changed to structured dict from plain string — enables route_after_executor and correction loop to inspect error_type programmatically without string parsing
- _get_timeout() is a plain function (not cached constant) — allows tests to patch via patch('agents.nodes.executor._get_timeout', return_value=0.1) for fast timeout tests
- ThreadPoolExecutor(max_workers=1) over asyncio.wait_for — cleaner integration with synchronous execute_query API; avoids event loop nesting issues
- LIMIT injection via simple regex — no AST needed; simple append to outermost query is correct for SELECT and WITH statements

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated test_state.py EXPECTED_FIELDS and field count from 14 to 17**
- **Found during:** Task 1 (AgentState expansion)
- **Issue:** tests/graph/test_state.py hardcoded `EXPECTED_FIELDS` with 14 fields and `assert len(hints) == 14`; adding 3 new fields caused test failure
- **Fix:** Updated EXPECTED_FIELDS set to include sql_explanation, execution_metadata, approval_status; updated assertion to `len(hints) == 17`; updated tests/graph/conftest.py make_initial_state to include 3 new fields
- **Files modified:** tests/graph/test_state.py, tests/graph/conftest.py
- **Verification:** python -m pytest tests/ -x — 120 passed (pre-Task 2)
- **Committed in:** f91d5c3 (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 — Bug: pre-existing test checked exact field count, broke when plan-specified fields were added)
**Impact on plan:** Necessary correctness fix. No scope creep.

## Issues Encountered

None — implementation matched plan spec exactly. Safety scanner false-positive prevention required care in ordering: strip block comments → line comments → single-quoted strings → double-quoted identifiers → backtick identifiers before extracting first keyword token.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- executor_node fully implemented: safety scan, LIMIT injection, 60s timeout, structured error dicts
- AgentState has all fields needed for correction loop (error_log as dict with error_type) and HITL gate (approval_status)
- route_after_executor in graph/conditions.py checks `error_log` truthy — structured dict is truthy, so routing still works correctly
- Phase 6 (correction loop) can inspect `error_log["error_type"]` and `error_log["hint"]` to generate targeted correction prompts
- 134 tests passing, 0 failures — safe to proceed

---
*Phase: 05-execution-safety-layer*
*Completed: 2026-03-15*
