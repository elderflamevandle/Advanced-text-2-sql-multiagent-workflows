---
phase: 06-error-correction-loop
plan: "01"
subsystem: error-classification
tags: [error-taxonomy, error-parser, agent-state, tdd, wave-1-foundation]
dependency_graph:
  requires: [05-02-SUMMARY.md]
  provides: [config/error-taxonomy.json, utils/error_parser.py, AgentState-19-fields, tests/agents/test_correction.py]
  affects: [graph/state.py, graph/builder.py, tests/graph/conftest.py]
tech_stack:
  added: [difflib, json, re, pathlib]
  patterns: [taxonomy-json-config, regex-classification, tdd-red-green, test-stubs-nyquist]
key_files:
  created:
    - config/error-taxonomy.json (populated with 20 categories)
    - utils/error_parser.py
    - tests/agents/test_error_parser.py
    - tests/agents/test_correction.py
  modified:
    - graph/state.py (added correction_plan and sql_history fields)
    - graph/builder.py (renamed nodes to avoid LangGraph state key collision)
    - tests/agents/conftest.py (updated to 19 fields, added relevant_tables list)
    - tests/graph/test_state.py (updated EXPECTED_FIELDS and assertions to 19)
    - tests/graph/conftest.py (added correction_plan and sql_history keys)
    - tests/graph/test_graph.py (updated Mermaid node name check)
decisions:
  - "Renamed graph nodes 'correction_plan' and 'correction_sql' to 'correction_plan_node' and 'correction_sql_node' to avoid LangGraph ValueError when state TypedDict has a field with the same name as a node"
  - "_load_taxonomy() reads JSON fresh each call (no lru_cache) to allow test patching via monkeypatch or file replacement"
  - "get_fuzzy_matches imports difflib inside function body for consistency with project lazy-import pattern"
  - "Wave 2 stubs marked with pytest.mark.skip (not assert False) so suite stays green and CI doesn't break"
  - "relevant_tables in make_agent_state() set to Chinook table list instead of None to support fuzzy match tests"
metrics:
  duration_seconds: 382
  completed_date: "2026-03-17"
  tasks_completed: 2
  files_created: 4
  files_modified: 6
  tests_passing: 168
  tests_skipped: 7
  tests_failed: 0
---

# Phase 6 Plan 01: Error Taxonomy Foundation and Test Scaffold Summary

**One-liner:** 20-category SQL error taxonomy with dialect-specific regex patterns, error_parser classification module, AgentState extended to 19 fields, and 12-test correction scaffold (5 passing + 7 Wave 2 stubs).

---

## What Was Built

### Task 1: Populate error-taxonomy.json and create utils/error_parser.py

**config/error-taxonomy.json** — populated with 20 error categories (was empty `[]`):

| # | ID | Severity | Strategy |
|---|-----|----------|---------|
| 1 | syntax_error | recoverable | fix_syntax |
| 2 | missing_table | recoverable | fix_table_name |
| 3 | missing_column | recoverable | fix_column_name |
| 4 | ambiguous_column | recoverable | qualify_column |
| 5 | type_mismatch | recoverable | fix_type_cast |
| 6 | division_by_zero | recoverable | add_null_guard |
| 7 | join_condition_missing | recoverable | add_join_condition |
| 8 | aggregation_error | recoverable | fix_group_by |
| 9 | subquery_returns_multiple_rows | recoverable | add_limit_or_aggregate |
| 10 | invalid_date_format | recoverable | fix_date_literal |
| 11 | null_comparison | recoverable | fix_null_check |
| 12 | like_pattern_error | recoverable | fix_like_pattern |
| 13 | column_alias_in_where | recoverable | move_alias_to_subquery |
| 14 | window_function_error | recoverable | fix_window_function |
| 15 | cte_reference_error | recoverable | fix_cte_reference |
| 16 | permission_denied | unrecoverable | request_permission_fix |
| 17 | connection_error | transient | retry_unchanged |
| 18 | timeout | transient | retry_unchanged |
| 19 | result_too_large | recoverable | add_limit |
| 20 | unknown | recoverable | general_fix |

Each category has: `id`, `name`, `severity`, `strategy`, `prompt_hint`, `patterns` (postgres/mysql/sqlite/duckdb arrays).

**utils/error_parser.py** — public API:
- `_load_taxonomy() -> dict` — reads JSON fresh from disk each call (no caching)
- `classify_error(error_log: dict, taxonomy: dict) -> tuple[dict, str]` — returns (category, "high"|"low")
- `get_fuzzy_matches(name: str, candidates: list, n=3, cutoff=0.6) -> list[str]` — difflib wrapper

### Task 2: Extend AgentState, update conftest.py and test_state.py, create test scaffold

**graph/state.py** — extended from 17 to 19 fields:
- `correction_plan: Optional[dict]` — structured diagnosis from correction_plan_node
- `sql_history: Optional[list]` — `[{sql, error, attempt_num}]` audit trail

**tests/agents/conftest.py** — `make_agent_state()` updated to 19 fields; `relevant_tables` set to Chinook table list for fuzzy match tests.

**tests/graph/test_state.py** — `EXPECTED_FIELDS` updated to 19 entries; assertion updated.

**tests/agents/test_correction.py** — 12 tests:
- 5 passing (Wave 1): `test_taxonomy_structure`, `test_classify_postgres_syntax_error`, `test_classify_sqlite_missing_table`, `test_llm_fallback_on_unknown_error`, `test_fuzzy_match_table_name`
- 7 skipped stubs (Wave 2): `test_correction_plan_returns_structured_plan`, `test_transient_error_no_llm_call`, `test_retry_count_increments`, `test_error_log_cleared_after_correction`, `test_sql_history_accumulates`, `test_sql_rewrite_uses_plan`, `test_wrong_table_self_corrects`

---

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Renamed graph nodes to avoid LangGraph state key collision**
- **Found during:** Task 2 — full suite run after AgentState extension
- **Issue:** LangGraph raises `ValueError: 'correction_plan' is already being used as a state key` when a node name matches a TypedDict field name. Adding `correction_plan` to AgentState conflicted with the existing `correction_plan` node in `graph/builder.py`.
- **Fix:** Renamed graph nodes `"correction_plan"` -> `"correction_plan_node"` and `"correction_sql"` -> `"correction_sql_node"` in `graph/builder.py`. Updated conditional edge map key target, edge definitions, Mermaid check in `test_graph.py`. Routing function return value `"correction_plan"` unchanged (it maps to the renamed target node via the path dict).
- **Files modified:** `graph/builder.py`, `tests/graph/test_graph.py`
- **Commit:** 379a0d9

---

## Test Count Breakdown

| Category | Count | Status |
|----------|-------|--------|
| Pre-existing tests (phases 1-5) | 157 | All pass |
| New: test_error_parser.py (TDD RED commit) | 6 | All pass |
| New: test_correction.py taxonomy tests | 5 | All pass |
| New: test_correction.py Wave 2 stubs | 7 | Skipped |
| **Total passing** | **168** | |
| **Total skipped** | **7** | |
| **Failed** | **0** | |

---

## Key Decisions Made

1. **Node rename over field rename** — kept `correction_plan` as the AgentState field name (plan requirement) and renamed the graph nodes to resolve the LangGraph collision. Routing function string `"correction_plan"` used as the path key (not the node name), so `conditions.py` is unchanged.

2. **No lru_cache on _load_taxonomy()** — follows the pattern from `database/safety.py`. Fresh reads allow test patching without importlib.reload tricks.

3. **Wave 2 stubs use pytest.mark.skip** — cleaner than `assert False` or `raise NotImplementedError`. The suite shows 7 skipped (not 7 failed), CI passes, and Wave 2 implementor knows exactly which tests to unskip.

---

## Self-Check: PASSED

Files verified:
- config/error-taxonomy.json: FOUND, 20 categories
- utils/error_parser.py: FOUND, exports _load_taxonomy, classify_error, get_fuzzy_matches
- graph/state.py: FOUND, correction_plan and sql_history present, 19 fields total
- tests/agents/conftest.py: FOUND, correction_plan and sql_history in make_agent_state()
- tests/graph/test_state.py: FOUND, EXPECTED_FIELDS has 19 entries
- tests/agents/test_correction.py: FOUND, 12 tests (5 pass + 7 skip)

Commits verified:
- 03d507c: test(06-01): add failing tests for error_parser taxonomy and classification
- 643374b: feat(06-01): populate error-taxonomy.json (20 categories) and create utils/error_parser.py
- 379a0d9: feat(06-01): extend AgentState to 19 fields and create correction test scaffold
