---
phase: 06-error-correction-loop
plan: 02
subsystem: agents
tags: [langgraph, error-correction, groq, llm, taxonomy, fuzzy-matching, sql-rewrite]

# Dependency graph
requires:
  - phase: 06-01
    provides: error taxonomy JSON, utils/error_parser.py (classify_error, get_fuzzy_matches), AgentState correction_plan/sql_history fields, Wave 2 test stubs

provides:
  - correction_plan_node: classifies error via taxonomy + LLM, returns 9-key correction_plan dict
  - correction_sql_node: rewrites SQL via LLM using correction plan, always clears error_log
  - formatter_node: success path + graceful degradation path + conversational path
  - All 12 test_correction.py tests passing (0 skipped)

affects: [phase 07-llm-integration, graph/builder.py correction loop, end-to-end pipeline behavior]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Lazy import of langchain_groq inside function body for all LLM nodes"
    - "sys.modules injection + importlib.reload() for ChatGroq mock in tests"
    - "list() copy pattern for sql_history to prevent cross-test mutation"
    - "Taxonomy metadata merged into LLM response dict (error_category, severity, strategy, prompt_hint always present)"

key-files:
  created: []
  modified:
    - agents/nodes/correction_plan.py
    - agents/nodes/correction_sql.py
    - agents/nodes/formatter.py
    - tests/agents/test_correction.py

key-decisions:
  - "correction_plan_node merges taxonomy metadata (error_category, strategy, prompt_hint) into LLM JSON response — ensures all 9 keys present even when LLM omits them"
  - "Transient early-return checks category severity before any LLM call — prevents LLM latency for connection_error and timeout categories"
  - "correction_sql_node always returns error_log: None — CRITICAL to prevent routing loop in route_after_executor"
  - "formatter_node PATH B triggers on error_log is not None OR sql_history is not empty — handles both mid-correction and post-exhaustion states"
  - "Fuzzy suggestions built only for name-related errors (missing_table, missing_column, ambiguous_column) from relevant_tables + column names"

patterns-established:
  - "Error node pattern: classify first, early-return for transient, LLM call only for recoverable"
  - "sql_history entry format: {sql, error, attempt_num} — always append before LLM call using list() copy"
  - "All LLM nodes: lazy import ChatGroq inside function body, same model string 'llama-3.3-70b-versatile'"

requirements-completed: [ERROR-002, ERROR-003]

# Metrics
duration: 25min
completed: 2026-03-16
---

# Phase 6 Plan 02: Error Correction Loop — Node Implementations Summary

**Correction loop completed: correction_plan_node (taxonomy + LLM diagnosis), correction_sql_node (targeted SQL rewrite), and formatter_node (graceful degradation) replace placeholder stubs, making all 12 test_correction.py tests pass.**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-03-16T00:00:00Z
- **Completed:** 2026-03-16
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- correction_plan_node: classifies errors against 20-category taxonomy, transient errors skip LLM entirely, non-transient errors get LLM diagnosis with schema context + fuzzy suggestions
- correction_sql_node: LLM-based SQL rewrite path using correction plan context, transient passthrough path, always returns error_log: None and increments retry_count
- formatter_node: three-path logic — success (db_results), graceful degradation (error_log or sql_history), conversational rejection
- All 12 test_correction.py tests pass (7 Wave 2 stubs unskipped), full suite 175 passed, 0 failed

## Task Commits

Each task was committed atomically:

1. **Task 1: Implement correction_plan_node and unskip its test stubs** - `d60d678` (feat)
2. **Task 2: Implement correction_sql_node, formatter graceful degradation, unskip remaining stubs** - `468d47a` (feat)

**Plan metadata:** (see final commit below)

## Files Created/Modified
- `agents/nodes/correction_plan.py` - Full LangGraph node: taxonomy classification, transient early-return, LLM diagnosis with _CORRECTION_PLAN_PROMPT, fuzzy suggestions, JSON parsing with fallback
- `agents/nodes/correction_sql.py` - Full LangGraph node: transient passthrough path, LLM SQL rewrite with _CORRECTION_SQL_PROMPT, SQL:/EXPLANATION: output split, always clears error_log
- `agents/nodes/formatter.py` - Three-path formatter: success, graceful degradation with attempt audit trail, conversational/rejection
- `tests/agents/test_correction.py` - Unskipped all 7 Wave 2 stubs; 12 tests now active and passing

## Decisions Made
- correction_plan_node merges `error_category` from taxonomy classification into LLM JSON response — ensures required key present even when LLM omits it from its output
- Transient severity early-return happens before LLM import — saves latency for connection_error and timeout categories where SQL rewrite is not needed
- correction_sql_node always returns `error_log: None` as CRITICAL invariant to prevent infinite routing loop in route_after_executor
- formatter PATH B triggers on `error_log is not None or sql_history` — covers both: route reached formatter mid-correction (has error_log) and post-exhaustion (may have cleared error_log but has sql_history)
- _extract_unrecognized_name() tries quoted identifier first (single/double quotes) then falls back to last colon-split token — handles Postgres/MySQL/SQLite/DuckDB error message formats

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] correction_plan_node LLM response missing error_category key**
- **Found during:** Task 1 (correction_plan_node implementation)
- **Issue:** Test mock returned `{"strategy": "fix_syntax"}` without `error_category`; plan specified key must always be present
- **Fix:** Added `plan["error_category"] = plan.get("error_category") or category.get("id", "unknown")` in taxonomy merge step so taxonomy classification always provides the fallback
- **Files modified:** agents/nodes/correction_plan.py
- **Verification:** test_correction_plan_returns_structured_plan passes
- **Committed in:** d60d678 (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 - Bug)
**Impact on plan:** Essential correctness fix — the plan required `error_category` always present but LLM response is not guaranteed to include it. Taxonomy merge guarantees it.

## Issues Encountered
None - all fixes handled via auto-fix Rule 1.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Phase 6 complete: ERROR-001 (taxonomy), ERROR-002 (correction_plan_node), ERROR-003 (correction_sql_node + formatter) all satisfied
- Error correction loop fully wired: executor failure → correction_plan_node → correction_sql_node → executor (retry) → formatter (after max retries)
- Full test suite: 175 passed, 0 failed, 0 skipped
- Ready for Phase 7 (LLM Integration and Fallback: LLM-001, LLM-002, LLM-003)

---
*Phase: 06-error-correction-loop*
*Completed: 2026-03-16*
