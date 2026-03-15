---
phase: 05-execution-safety-layer
plan: 02
subsystem: agent
tags: [langgraph, hitl, interrupt, human-in-the-loop, graph, routing, approval-gate]

# Dependency graph
requires:
  - phase: 05-01
    provides: AgentState with approval_status field, executor_node safety layer, config/config.yaml hitl section
  - phase: 04-02
    provides: sql_generator_node producing generated_sql and sql_explanation
provides:
  - HITL approval gate node (agents/nodes/hitl.py) with LangGraph interrupt mechanism
  - Simple query auto-approval via _is_simple_query() (JOIN/subquery/UNION/CTE detection)
  - route_after_hitl routing function (graph/conditions.py)
  - Updated graph wiring: sql_generator -> hitl -> executor/formatter
  - 10-node graph (was 9 nodes before HITL insertion)
affects: [06-correction-loop, 07-formatter-evaluator, frontend, end-to-end-testing]

# Tech tracking
tech-stack:
  added: [langgraph.types.interrupt]
  patterns: [LangGraph interrupt() for human-in-the-loop pausing, lazy import of langgraph.types inside node function body]

key-files:
  created:
    - agents/nodes/hitl.py
    - tests/agents/test_hitl.py
  modified:
    - agents/nodes/__init__.py
    - graph/conditions.py
    - graph/builder.py
    - tests/graph/test_graph.py

key-decisions:
  - "interrupt() is called via lazy import inside hitl_node — avoids import-time side effects and allows tests to mock via sys.modules injection before module load"
  - "_is_simple_query uses word-boundary regex for JOIN/UNION and count of SELECT occurrences for subqueries — no AST needed for this safety heuristic"
  - "Resumption handling via approval_status check at top of hitl_node — when LangGraph re-enters the node after interrupt resume, approval_status is already in state; checking it first prevents re-triggering interrupt"
  - "route_after_hitl returns 'executor' for None approval_status — ensures backward compatibility with any path that skips HITL (e.g., unit tests that don't set approval_status)"

patterns-established:
  - "HITL node pattern: check disabled -> check resumption -> check auto-approve -> interrupt() for complex queries"
  - "Resumption states are explicit: approved (pass), rejected (error_log), edited (re-approve with new SQL)"

requirements-completed: [AGENT-010]

# Metrics
duration: 6min
completed: 2026-03-15
---

# Phase 5 Plan 02: HITL Approval Gate Summary

**HITL node with LangGraph interrupt pauses graph execution on complex queries (JOIN/subquery/UNION/CTE); auto-approves simple SELECT queries; graph rewired sql_generator -> hitl -> executor/formatter with route_after_hitl routing**

## Performance

- **Duration:** 6 min
- **Started:** 2026-03-15T09:08:25Z
- **Completed:** 2026-03-15T09:14:26Z
- **Tasks:** 2 (Task 1 TDD, Task 2 graph rewiring)
- **Files modified:** 6

## Accomplishments
- hitl_node with LangGraph `interrupt()` for complex queries (JOIN/subquery/UNION/CTE), auto-approval for simple queries, and resumption handling (approved/rejected/edited)
- _is_simple_query() helper with case-insensitive regex detection of JOIN, nested SELECT, UNION, and WITH (CTE) patterns
- route_after_hitl() routing function added to graph/conditions.py (rejected -> formatter, else -> executor)
- Graph rewired: sql_generator now routes through hitl node before executor, with conditional edges for rejection path to formatter
- 23 new tests (17 HITL unit + 6 graph integration) bringing total from 134 to 157 passed, 0 failed

## Task Commits

Each task was committed atomically:

1. **Task 1: HITL node with LangGraph interrupt and auto-approve logic** - `c703f01` (feat)
2. **Task 2: Graph rewiring -- insert HITL between sql_generator and executor** - `afee0c3` (feat)

**Plan metadata:** (docs commit follows)

_Note: Task 1 used TDD (RED then GREEN in single pass — module not found confirmed RED, all 17 passed confirmed GREEN)_

## Files Created/Modified
- `agents/nodes/hitl.py` - HITL approval gate node with interrupt, auto-approve, and resumption handling
- `tests/agents/test_hitl.py` - 17 AGENT-010 unit tests covering all hitl_node branches and _is_simple_query
- `agents/nodes/__init__.py` - Added hitl_node export (10 nodes total)
- `graph/conditions.py` - Added route_after_hitl routing function
- `graph/builder.py` - Wired hitl node between sql_generator and executor with conditional edges
- `tests/graph/test_graph.py` - Added 6 tests for HITL node, updated Mermaid check to include 'hitl'

## Decisions Made
- interrupt() called via lazy import (`from langgraph.types import interrupt`) inside hitl_node — prevents import-time side effects; allows sys.modules mock injection in tests before module load
- _is_simple_query counts SELECT occurrences to detect subqueries (>1 means nested); no AST needed — sufficient for safety heuristic
- Resumption handling via approval_status check at top of node — LangGraph re-enters the same node on resume; checking existing approval_status first prevents re-triggering interrupt on resumed state
- route_after_hitl returns 'executor' for None approval_status — backward-compatible default; any state without approval_status passes through to executor

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- HITL approval gate complete, satisfying AGENT-010
- Graph now has 10 nodes wired correctly: gatekeeper -> schema_linker -> query_planner -> sql_generator -> hitl -> executor/formatter
- Phase 5 execution safety layer complete (Plan 01: safety scanner + executor; Plan 02: HITL gate)
- Ready for Phase 6: correction loop (correction_plan_node, correction_sql_node full implementations)

## Self-Check: PASSED

All files verified present. All commits verified in git log.

---
*Phase: 05-execution-safety-layer*
*Completed: 2026-03-15*
