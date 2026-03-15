---
phase: 04-specialized-agent-nodes
plan: 02
subsystem: agents
tags: [query-planner, sql-generator, llm, groq, tdd, agent-nodes]
dependency_graph:
  requires: [04-01]
  provides: [query_planner_node, sql_generator_node]
  affects: [graph/builder.py, agents/nodes/__init__.py]
tech_stack:
  added: []
  patterns: [lazy-ChatGroq-import, sys.modules-injection-for-mocking, TDD-red-green]
key_files:
  created:
    - agents/nodes/query_planner.py
    - agents/nodes/sql_generator.py
    - tests/agents/test_query_planner.py
    - tests/agents/test_sql_generator.py
  modified: []
decisions:
  - sys.modules injection + importlib.reload for mocking lazy-imported ChatGroq (same pattern as Plan 01)
  - _DEFAULT_PLAN returns fresh dict copy to prevent cross-test mutation
  - EXPLANATION: label split separates SQL from explanation in single LLM response
  - _validate_sql strips fences first, then checks SELECT/WITH prefix
  - conftest.make_agent_state default schema includes Invoice.Total for realistic test fixtures
metrics:
  duration_minutes: 15
  completed_date: "2026-03-15"
  tasks_completed: 2
  tasks_total: 2
  files_created: 4
  files_modified: 2
  tests_added: 12
  tests_total: 95
---

# Phase 4 Plan 02: Query Planner and SQL Generator Agent Nodes Summary

**One-liner:** JSON query plan generation and dialect-specific SQL synthesis via ChatGroq with TDD coverage.

---

## What Was Built

### agents/nodes/query_planner.py

- `_PLANNER_PROMPT` module-level constant (satisfies LLM-003) — instructs ChatGroq to produce a 9-key JSON plan
- `_DEFAULT_PLAN` dict with all empty lists, `limit: None`, `complexity: "simple"` — safe fallback on parse failure
- `_parse_json_response(raw)` — strips ` ```json ` fences via `re.sub`, calls `json.loads`, raises `ValueError` on failure
- `query_planner_node(state)` — async node; extracts `resolved_query or user_query`; formats schema context; lazy-imports ChatGroq; returns `{"query_plan": plan}`

### agents/nodes/sql_generator.py

- `_GENERATOR_PROMPT` module-level constant (satisfies LLM-003) — structured output format with SQL: / EXPLANATION: labels
- `_DIALECT_REMINDERS` dict — postgres/mysql/sqlite/duckdb hints injected into system prompt
- `_validate_sql(sql)` — strips ` ```sql ` fences, asserts SELECT/WITH prefix, raises `ValueError` for destructive statements
- `sql_generator_node(state)` — async node; extracts `resolved_query or user_query`, `query_plan`, `db_type`; splits response on EXPLANATION: label; validates SQL; returns `{"generated_sql": ..., "sql_explanation": ...}` or `{"generated_sql": None, "error_log": ...}` on failure

---

## Test Coverage

| File | Tests | Status |
|------|-------|--------|
| tests/agents/test_query_planner.py | 5 | PASSED |
| tests/agents/test_sql_generator.py | 7 | PASSED |
| Full suite | 95 | PASSED (0 failures) |

**Tests cover:**
- Structured JSON plan with all 9 required keys
- Malformed JSON fallback to `_DEFAULT_PLAN`
- Markdown fence stripping in both nodes
- `resolved_query` preference over `user_query` (inspected via mock call args)
- Module-level prompt constant existence (LLM-003)
- SELECT and WITH CTE responses accepted
- Non-SELECT/WITH (e.g., DROP) rejected with `error_log`
- Human-readable explanation returned
- Postgres dialect reminder present in system message

---

## Deviations from Plan

None — plan executed exactly as written.

---

## Key Decisions

1. **sys.modules injection + importlib.reload for mocking** — same pattern established in Plan 01 for lazy-imported ChatGroq inside function bodies; `patch()` fails because the name is never bound at module level
2. **_DEFAULT_PLAN returns `dict(_DEFAULT_PLAN)` copy** — prevents test mutation leaking across test cases
3. **Single LLM call with SQL: / EXPLANATION: output format** — `re.split(r"EXPLANATION\s*:\s*", raw)` cleanly separates the two parts; avoids two separate LLM calls
4. **_validate_sql strips fences before prefix check** — ` ```sql\nSELECT... ``` ` would fail the SELECT check without stripping first

---

## Requirements Satisfied

- AGENT-003: query_planner_node with structured JSON plan, graceful fallback, fence stripping
- AGENT-004: sql_generator_node with dialect hints, validation, explanation
- LLM-003: module-level `_PLANNER_PROMPT` and `_GENERATOR_PROMPT` constants in both files

---

## Self-Check

All 4 artifact files created/modified, all 12 new tests pass, full suite 95/95 green.
