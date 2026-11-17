"""Chat interface — rendering, query submission, streaming, and HITL approval card."""
import asyncio
import concurrent.futures
import contextlib
import logging
import re
from typing import Generator

import streamlit as st
from langgraph.errors import GraphInterrupt

logger = logging.getLogger(__name__)

NODE_LABELS = {
    "gatekeeper": "Validating query...",
    "schema_linker": "Linking schema...",
    "query_planner": "Planning query...",
    "sql_generator": "Generating SQL...",
    "hitl": "Awaiting approval...",
    "executor": "Executing SQL...",
    "correction_plan_node": "Diagnosing error...",
    "correction_sql_node": "Correcting SQL...",
    "formatter": "Formatting answer...",
    "evaluator": "Evaluating quality...",
}


# ---------------------------------------------------------------------------
# Local chunker for st.write_stream (typing effect without any LLM call)
# ---------------------------------------------------------------------------

def _iter_text_chunks(text: str) -> Generator[str, None, None]:
    """Yield the text word by word, whitespace preserved; "".join(chunks) == text."""
    for match in re.finditer(r"\S+\s*|\s+", text):
        yield match.group(0)


# ---------------------------------------------------------------------------
# Public rendering entry point
# ---------------------------------------------------------------------------

def render_chat():
    """Main chat rendering loop. Called from app.py when messages exist."""
    # Render existing messages — pass stable index-based key to avoid duplicate widget keys
    for i, msg in enumerate(st.session_state.messages):
        _render_message(msg, msg_key=f"msg_{i}")

    # Handle pending HITL decision (set by button click on previous re-run)
    if st.session_state.get("hitl_decision") and st.session_state.get("hitl_pending"):
        _process_hitl_decision()

    # Render HITL approval card if awaiting user action
    if st.session_state.get("hitl_pending"):
        render_hitl_card(
            sql=st.session_state.hitl_pending.get("generated_sql", ""),
            explanation=st.session_state.hitl_pending.get("sql_explanation", ""),
        )

    # Note: chat_input and _pending_query handling is owned by app.py (always visible)


def _render_message(msg: dict, msg_key: str = ""):
    """Render a single stored message (user or assistant)."""
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        # Render embedded results/charts/debug if present
        if msg.get("state"):
            _render_assistant_extras(msg["state"], msg_key=msg_key)


# ---------------------------------------------------------------------------
# Query submission — stage labels + token streaming
# ---------------------------------------------------------------------------

def _export_keys_to_environ(key_map: dict):
    import os
    for state_key, env_key in key_map.items():
        val = st.session_state.get(state_key, "")
        if val:
            os.environ[env_key] = val


def _llm_credentials():
    """Context manager that scopes this session's LLM keys to one graph run.

    os.environ is shared by every browser session, so a key typed into one sidebar must
    not be written there. ``llm.context.use_llm_credentials`` keeps the keys in
    contextvars instead. The returned object is created here, on the script thread
    (session_state is only readable there), and entered inside the coroutine that runs
    the graph. An empty sidebar field passes None, which leaves the environment key in use.

    The Pinecone key has no request-scoped equivalent yet and is still exported to
    os.environ. Without llm.context all keys are exported, as before.
    """
    try:
        from llm.context import use_llm_credentials
    except ImportError:
        _export_keys_to_environ({
            "groq_api_key": "GROQ_API_KEY",
            "openai_api_key": "OPENAI_API_KEY",
            "pinecone_api_key": "PINECONE_API_KEY",
        })
        return contextlib.nullcontext()
    _export_keys_to_environ({"pinecone_api_key": "PINECONE_API_KEY"})
    return use_llm_credentials(
        groq_api_key=st.session_state.get("groq_api_key") or None,
        openai_api_key=st.session_state.get("openai_api_key") or None,
    )


def submit_query(user_query: str):
    """Process a new user query: add to history, run graph, stream response, store result."""
    # Add user message
    st.session_state.messages.append({"role": "user", "content": user_query})
    with st.chat_message("user"):
        st.markdown(user_query)

    # Build initial AgentState
    initial_state = _build_initial_state(user_query)
    config = {"configurable": {"thread_id": st.session_state.thread_id}}

    with st.chat_message("assistant"):
        # Phase 1: Run graph with per-node stage labels inside st.status
        with st.status("Processing query...", expanded=True) as status_container:
            try:
                final_state = _run_graph_streaming(initial_state, config, status_container)
                status_container.update(label="Complete", state="complete", expanded=False)
            except GraphInterrupt as exc:
                _handle_graph_interrupt(exc, config)
                status_container.update(label="Awaiting your approval...", state="running")
                st.rerun()
                return
            except Exception as exc:
                status_container.update(label="Error", state="error")
                st.error(f"Unexpected error: {exc}")
                logger.exception("Graph execution error: %s", exc)
                return

        # Phase 2: show the graph's final_answer itself. No LLM is involved here: the
        # formatter node already wrote the answer; this only renders it progressively.
        answer = final_state.get("final_answer") or "No answer generated."
        _stream_final_answer(answer)

        # Live render uses "live" key — this message has not been appended to messages yet,
        # so it won't be re-rendered by the messages loop on the same rerun.
        _render_assistant_extras(final_state, msg_key="live")

    # Store assistant message with the full answer text
    st.session_state.messages.append({
        "role": "assistant",
        "content": answer,
        "state": final_state,
    })
    st.session_state.last_state = final_state
    _update_session_cost(final_state.get("usage_metadata") or [])


def _stream_final_answer(answer_text: str) -> str:
    """Render the final answer text as-is and return it unchanged.

    The text is fed to st.write_stream() from a local generator, purely for the typing
    effect. It is never sent to an LLM. Falls back to st.markdown() if streaming fails.
    """
    try:
        st.write_stream(_iter_text_chunks(answer_text))
    except Exception as exc:
        logger.warning("st.write_stream failed (%s); falling back to markdown", exc)
        st.markdown(answer_text)
    return answer_text


# ---------------------------------------------------------------------------
# Graph streaming helper (per-node stage labels)
# ---------------------------------------------------------------------------

def _run_async(coro):
    """Run an async coroutine in a dedicated thread with its own event loop.

    All agent nodes are async, so the graph must use the async API (astream/ainvoke).
    Running in an isolated thread ensures LangGraph's internal ContextVars
    (including CONFIG_KEY used by interrupt()/get_config()) are initialised cleanly
    without interference from Streamlit's event loop context on Python 3.10.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()


async def _astream_graph(initial_state: dict, config: dict, labels: list,
                         credentials=None) -> dict:
    """Async: stream graph, collect node labels, then return the full checkpoint state.

    Uses stream_mode="updates" to get per-node labels for the status bar, then calls
    aget_state() to fetch the complete checkpoint state (includes initial-state-only fields
    like user_query and db_type that node output dicts never contain).
    """
    from graph.builder import compiled_graph as graph
    # Entered here, not by the caller: this coroutine runs in its own thread, and
    # contextvars set on the script thread would not be visible in it.
    with credentials if credentials is not None else contextlib.nullcontext():
        async for chunk in graph.astream(initial_state, config=config, stream_mode="updates"):
            for node_name in chunk:
                labels.append(NODE_LABELS.get(node_name, f"Running {node_name}..."))
    # Fetch full checkpoint state — captures every field, not just node output diffs
    snapshot = await graph.aget_state(config)
    return dict(snapshot.values) if snapshot else {}


def _run_graph_streaming(initial_state: dict, config: dict, status_container) -> dict:
    """Run the async graph in an isolated thread, updating status labels as nodes complete."""
    labels: list = []
    credentials = _llm_credentials()
    # Run graph in dedicated thread — isolates LangGraph ContextVars from Streamlit's loop
    final_state = _run_async(_astream_graph(initial_state, config, labels, credentials))
    # Replay node labels back into the status container from the main thread
    for label in labels:
        status_container.update(label=label, state="running")
    return final_state


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _build_initial_state(user_query: str) -> dict:
    """Build the initial AgentState dict for graph invocation.

    Only identifiers go into the state (it is checkpointed): the session's tenant_id and
    connection_id. Nodes resolve the database from them through the connection registry,
    and the schema is read from the registry here and passed as a plain dict.

    Without a registered connection (registry unavailable, legacy path) the schema comes
    from the process-wide get_active_manager() and the ids stay None.
    """
    tenant_id = st.session_state.get("tenant_id")
    connection_id = st.session_state.get("connection_id")
    # The dialect of the connected database, not whatever the selectbox shows right now.
    db_type = (st.session_state.get("connection_db_type")
               or st.session_state.get("sidebar_db_type") or "SQLite").lower()
    schema = None
    if connection_id:
        try:
            from database.registry import get_registry
            schema = get_registry().get_schema(tenant_id, connection_id)
        except Exception as exc:
            logger.warning("_build_initial_state: no schema from registry: %s", exc)
    else:
        from database.manager import get_active_manager
        mgr = get_active_manager()
        if mgr is not None:
            try:
                schema = mgr.get_schema()
            except Exception as exc:
                logger.warning("_build_initial_state: failed to extract schema: %s", exc)
    return {
        "messages": [],
        "user_query": user_query,
        "db_type": db_type,
        "tenant_id": tenant_id if connection_id else None,
        "connection_id": connection_id,
        "schema": schema,
        "retry_count": 0,
        # Explicitly reset per-query fields to prevent MemorySaver checkpoint leakage
        # across separate queries on the same thread_id.
        "usage_metadata": None,  # reducer field: None resets the accumulated list
        "resolved_query": None,
        "query_type": None,
        "query_plan": None,
        "generated_sql": None,
        "sql_explanation": None,
        "execution_metadata": None,
        "error_log": None,
        "sql_history": [],
        "correction_plan": None,
        "approval_status": None,
        "final_answer": None,
        "db_results": None,
    }


def _handle_graph_interrupt(exc: GraphInterrupt, config: dict):
    """Store HITL interrupt data in session_state for the approval card."""
    interrupt_list = exc.args[0] if exc.args else []
    interrupt_value = interrupt_list[0].value if interrupt_list else {}
    st.session_state.hitl_pending = interrupt_value
    st.session_state.hitl_config = config


def _process_hitl_decision():
    """Resume graph with the user's HITL decision stored in session_state."""
    from graph.builder import compiled_graph as graph
    from langgraph.types import Command  # lazy import — avoids sys.modules pollution from test_hitl.py
    decision = st.session_state.hitl_decision
    config = st.session_state.hitl_config

    # Clear HITL flags before resuming
    st.session_state.hitl_pending = None
    st.session_state.hitl_config = None
    st.session_state.hitl_decision = None

    credentials = _llm_credentials()
    try:
        async def _resume():
            with credentials:
                return await graph.ainvoke(Command(resume=decision), config=config)
        final_state = _run_async(_resume())
    except Exception as exc:
        st.error(f"Error resuming after HITL: {exc}")
        return

    answer = final_state.get("final_answer") or "Query completed."
    # Determine key based on the 0-based index this message will occupy once appended
    live_key = f"msg_{len(st.session_state.messages)}"
    with st.chat_message("assistant"):
        st.markdown(answer)
        _render_assistant_extras(final_state, msg_key=live_key)
    st.session_state.messages.append({
        "role": "assistant",
        "content": answer,
        "state": final_state,
    })
    st.session_state.last_state = final_state
    _update_session_cost(final_state.get("usage_metadata") or [])


# ---------------------------------------------------------------------------
# HITL approval card
# ---------------------------------------------------------------------------

def render_hitl_card(sql: str, explanation: str):
    """Render the inline HITL approval card as an assistant message."""
    with st.chat_message("assistant"):
        st.warning("SQL Review Required — please approve, edit, or reject before execution.")
        st.code(sql, language="sql")
        if explanation:
            st.caption(explanation)

        edited_sql = st.text_area("Edit SQL (optional):", value=sql, key="hitl_edit_area",
                                   height=120)
        col1, col2, col3 = st.columns(3)
        if col1.button("Approve", key="hitl_approve", type="primary"):
            st.session_state.hitl_decision = {"action": "approved", "sql": sql}
            st.rerun()
        if col2.button("Save & Run Edited", key="hitl_save"):
            st.session_state.hitl_decision = {"action": "edited", "sql": edited_sql}
            st.rerun()
        if col3.button("Reject", key="hitl_reject"):
            st.session_state.hitl_decision = {"action": "rejected", "sql": sql}
            st.rerun()


# ---------------------------------------------------------------------------
# Assistant extras (results table, chart, debug panel)
# ---------------------------------------------------------------------------

def _render_assistant_extras(state: dict, msg_key: str = ""):
    """Render results table, download button, and debug panel for an assistant message.

    ``msg_key`` must be unique per logical message across the entire rerun so that
    all child widgets (download button, chart toggle, edit/rerun text_area) get
    stable, non-colliding keys.  Callers pass either ``f"msg_{i}"`` (history loop)
    or ``"live"`` / a positional key (live render during submit_query).
    """
    db_results = state.get("db_results")
    if db_results:
        import pandas as pd
        df = pd.DataFrame(db_results)
        st.dataframe(df, use_container_width=True)
        csv = df.to_csv(index=False)
        # Use msg_key for a stable download button key that doesn't rely on id()
        st.download_button("Download CSV", data=csv, file_name="results.csv",
                           mime="text/csv", key=f"dl_{msg_key}")
        # Chart rendering delegated to charts.py (Plan 04)
        try:
            from components.charts import render_chart_with_toggle
            render_chart_with_toggle(db_results, state, msg_key=msg_key)
        except ImportError:
            pass  # charts.py not yet created in Wave 2

    # Debug panel — delegated to debug_panel.py (Plan 04)
    try:
        from components.debug_panel import render_debug_panel
        render_debug_panel(state, msg_key=msg_key)
    except ImportError:
        pass  # debug_panel.py not yet created in Wave 2

    # Ragas confidence score (conditional — placeholder node may not set ragas_score)
    ragas_score = state.get("ragas_score")
    if ragas_score is not None:
        st.progress(float(ragas_score), text=f"Confidence: {ragas_score:.2f}")


def _update_session_cost(usage_metadata: list):
    """Accumulate token/cost totals into session_state for the sidebar ticker."""
    for entry in usage_metadata:
        st.session_state.session_tokens += entry.get("total_tokens", 0)
        st.session_state.session_cost += entry.get("estimated_cost_usd", 0.0)
