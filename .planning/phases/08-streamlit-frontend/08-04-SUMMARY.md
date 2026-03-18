---
phase: 08-streamlit-frontend
plan: "04"
subsystem: ui
tags: [streamlit, debug-panel, charts, plotly, tdd, ui]
dependency_graph:
  requires: [08-02, 08-03]
  provides: [debug_panel.py, charts.py]
  affects: [streamlit_app/app.py, tests/ui/]
tech_stack:
  added: [plotly.express]
  patterns: [TDD-RED-GREEN, asyncio.run-executor-bypass, column-heuristic-chart-detection]
key_files:
  created:
    - streamlit_app/components/debug_panel.py
    - streamlit_app/components/charts.py
  modified:
    - streamlit_app/app.py
    - tests/ui/test_debug_panel.py
    - tests/ui/test_charts.py
    - tests/ui/test_e2e.py
decisions:
  - "detect_chart_type() uses column-name heuristics (no chart_type in state): date/time keyword -> Line; string+numeric -> Bar; otherwise None"
  - "render_debug_panel() auto-expands when retry_count > 0 OR error_log is not None — signals correction loop ran"
  - "rerun_sql() calls asyncio.run(executor_node({**state, generated_sql: edited})) — bypasses planner/generator entirely"
  - "app.py condition updated: render_chat() called when messages OR hitl_pending — unifies HITL path with normal chat flow"
  - "widget keys use id(state)/id(results) for uniqueness — prevents Streamlit DuplicateWidgetID across message history"
metrics:
  duration: "4 minutes"
  completed_date: "2026-03-18"
  tasks: 2
  files_created: 2
  files_modified: 4
requirements_satisfied: [UI-003, UI-004]
---

# Phase 8 Plan 04: Debug Panel and Visualization Dashboard Summary

**One-liner:** Plotly auto-chart with Line/Bar/Table toggle and 4-section SQL debug expander with Edit & Rerun executor bypass.

## Tasks Completed

| # | Task | Commit | Files |
|---|------|--------|-------|
| 1 | Implement debug_panel.py with 4 sections and Edit & Rerun | 6f12d7e | streamlit_app/components/debug_panel.py, tests/ui/test_debug_panel.py |
| 2 | Implement charts.py, wire app.py, fill test_charts.py and test_e2e.py | 918e61e | streamlit_app/components/charts.py, streamlit_app/app.py, tests/ui/test_charts.py, tests/ui/test_e2e.py |

## What Was Built

### debug_panel.py (`render_debug_panel(state)`)

Collapsible `st.expander("Debug Details")` with 4 sections:

1. **Generated SQL + Explanation** — `st.code(language="sql", line_numbers=True)` + `st.markdown` for sql_explanation
2. **Query Plan** — `st.json()` for dict plans, `st.markdown()` for string plans
3. **Retry Logs** — `st.error()` for error_log, `st.info()` for correction_plan diagnosis, nested expanders per sql_history attempt
4. **LLM Usage** — pandas DataFrame table (Node/Provider/Model/In Tokens/Out Tokens/Total/Cost USD) + conditional Ragas metric

Auto-expand logic: `retry_count > 0 OR error_log is not None` — surfaces the debug panel automatically when a correction loop ran.

**Edit & Rerun section:** `st.text_area` pre-filled with generated_sql + "Rerun" button that calls `rerun_sql()`. The `rerun_sql()` function uses `asyncio.run(executor_node({**state, "generated_sql": edited_sql}))` — bypasses planner/generator/HITL entirely.

### charts.py (`detect_chart_type()`, `render_chart_with_toggle()`)

**`detect_chart_type(results)`** — heuristic-based (no state field):
- Any column name containing date/time/year/month/day/week/timestamp/period/quarter keyword → `"Line"`
- At least one object (string) column + at least one numeric column → `"Bar"`
- Empty results, single column, or all-numeric → `None` (no chart rendered)

**`render_chart_with_toggle(results, state)`** — renders a `st.radio` toggle (Line/Bar/Table) defaulting to detected type, then `px.line()` or `px.bar()` via plotly.express. Table option exits early (table already shown by chat.py's st.dataframe).

### app.py update

Changed the main() conditional from `if not messages` / `else render_chat()` to `if not messages AND not hitl_pending` / `else render_chat()`. This ensures the HITL approval card is rendered even when the message list is empty (e.g., first query that triggers HITL review).

## Test Results

```
tests/ui/test_debug_panel.py: 6 passed (was 5 skipped)
tests/ui/test_charts.py: 5 passed (was 4 skipped)
tests/ui/test_e2e.py: 1 passed, 1 skipped (AppTest.from_file stub — requires live Streamlit)
Full suite: 222 passed, 1 skipped, 0 failed (was 210 passed, 11 skipped)
```

## Deviations from Plan

### Auto-fixed Issues

None — plan executed exactly as written, with one minor addition:

**App.py: HITL pending condition** — The plan described making `render_chat()` the unconditional path. Implemented as `if not messages AND not hitl_pending` rather than pure `if not messages` to ensure the HITL approval card renders correctly on first-query HITL scenarios. This is aligned with the plan's stated intent ("If messages OR pending hitl: call render_chat() unconditionally").

## Self-Check: PASSED

- FOUND: streamlit_app/components/debug_panel.py
- FOUND: streamlit_app/components/charts.py
- FOUND: .planning/phases/08-streamlit-frontend/08-04-SUMMARY.md
- FOUND: commit 6f12d7e (debug_panel.py)
- FOUND: commit 918e61e (charts.py + app.py + tests)
- Full suite: 222 passed, 1 skipped, 0 failed
