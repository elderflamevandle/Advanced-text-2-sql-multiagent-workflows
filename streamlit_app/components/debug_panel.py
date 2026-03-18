"""Debugging panel — expandable SQL/plan/retry/usage display with Edit & Rerun."""
import asyncio
import logging

import streamlit as st

logger = logging.getLogger(__name__)


def render_debug_panel(state: dict):
    """Render the collapsible debug expander for an assistant response.

    Auto-expands when retry_count > 0 or error_log is present (signals a correction loop ran).
    """
    retry_count = state.get("retry_count", 0)
    has_error = state.get("error_log") is not None
    auto_expand = retry_count > 0 or has_error

    with st.expander("Debug Details", expanded=auto_expand):
        _render_sql_section(state)
        _render_query_plan_section(state)
        _render_retry_section(state)
        _render_usage_section(state)
        _render_edit_rerun_section(state)


def _render_sql_section(state: dict):
    """Section 1: Generated SQL + Explanation."""
    st.subheader("Generated SQL")
    generated_sql = state.get("generated_sql")
    if generated_sql:
        st.code(generated_sql, language="sql", line_numbers=True)
    else:
        st.caption("No SQL generated.")

    sql_explanation = state.get("sql_explanation")
    if sql_explanation:
        st.markdown(f"**Explanation:** {sql_explanation}")


def _render_query_plan_section(state: dict):
    """Section 2: Query Plan (Chain-of-Thought breakdown)."""
    st.subheader("Query Plan")
    query_plan = state.get("query_plan")
    if query_plan:
        if isinstance(query_plan, dict):
            import json
            st.json(query_plan)
        else:
            st.markdown(str(query_plan))
    else:
        st.caption("No query plan available.")


def _render_retry_section(state: dict):
    """Section 3: Retry Logs & Error Details."""
    retry_count = state.get("retry_count", 0)
    error_log = state.get("error_log")
    sql_history = state.get("sql_history") or []
    correction_plan = state.get("correction_plan")

    st.subheader(f"Retry Logs (attempts: {retry_count})")

    if error_log:
        if isinstance(error_log, dict):
            st.error(f"Error [{error_log.get('error_type', 'unknown')}]: {error_log.get('message', '')}")
        else:
            st.error(str(error_log))

    if correction_plan:
        st.info(
            f"Diagnosis: {correction_plan.get('error_category', 'unknown')} "
            f"(severity: {correction_plan.get('severity', '?')}) — "
            f"Strategy: {correction_plan.get('strategy', '?')}"
        )

    if sql_history:
        for entry in sql_history:
            num = entry.get("attempt_num", 0) + 1
            sql_preview = (entry.get("sql") or "")[:200]
            err = entry.get("error") or {}
            err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            with st.expander(f"Attempt {num}", expanded=False):
                st.code(sql_preview, language="sql")
                if err_msg:
                    st.caption(f"Error: {err_msg[:200]}")
    elif retry_count == 0:
        st.caption("No retries — query succeeded on first attempt.")


def _render_usage_section(state: dict):
    """Section 4: LLM Usage — provider/model/tokens/cost per node call."""
    st.subheader("LLM Usage")
    usage_metadata = state.get("usage_metadata") or []
    if not usage_metadata:
        st.caption("No LLM usage recorded.")
        return

    import pandas as pd
    rows = []
    for entry in usage_metadata:
        rows.append({
            "Node": entry.get("node_name", "?"),
            "Provider": entry.get("provider", "?"),
            "Model": entry.get("model", "?"),
            "In Tokens": entry.get("input_tokens", 0),
            "Out Tokens": entry.get("output_tokens", 0),
            "Total": entry.get("total_tokens", 0),
            "Cost USD": f"${entry.get('estimated_cost_usd', 0.0):.6f}",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # Ragas score breakdown (conditional — evaluator_node may not be active)
    ragas_score = state.get("ragas_score")
    if ragas_score is not None:
        st.metric("Ragas Confidence", f"{ragas_score:.3f}")


def _render_edit_rerun_section(state: dict):
    """Edit & Rerun SQL — bypasses full graph, calls executor_node directly."""
    st.subheader("Edit & Rerun SQL")
    st.caption("Bypass the planner/generator — execute modified SQL directly against the database.")
    generated_sql = state.get("generated_sql") or ""
    # Use unique key based on state id to avoid widget conflicts between messages
    edit_key = f"edit_sql_{id(state)}"
    btn_key = f"rerun_btn_{id(state)}"
    edited_sql = st.text_area("SQL to execute:", value=generated_sql, height=120, key=edit_key)
    if st.button("Rerun", key=btn_key, type="secondary"):
        result = rerun_sql(edited_sql, state)
        if result.get("db_results") is not None:
            import pandas as pd
            df = pd.DataFrame(result["db_results"])
            st.success(f"Rerun succeeded: {len(df)} row(s)")
            st.dataframe(df, use_container_width=True)
        elif result.get("error_log"):
            err = result["error_log"]
            msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            st.error(f"Rerun failed: {msg}")


def rerun_sql(edited_sql: str, last_state: dict) -> dict:
    """Bypass planning/generation — execute edited SQL directly via executor_node."""
    from agents.nodes.executor import executor_node
    modified_state = {**last_state, "generated_sql": edited_sql}
    try:
        return asyncio.run(executor_node(modified_state))
    except Exception as exc:
        logger.error("rerun_sql failed: %s", exc)
        return {"error_log": {"error_type": "rerun_error", "message": str(exc)}}
