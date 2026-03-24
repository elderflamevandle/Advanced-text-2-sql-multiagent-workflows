"""Chat interface — rendering, query submission, streaming, and HITL approval card."""
import asyncio
import concurrent.futures
import logging
from typing import Generator

import streamlit as st
from langgraph.errors import GraphInterrupt
from llm.fallback import get_llm

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
# Async-to-sync adapter for st.write_stream compatibility
# ---------------------------------------------------------------------------

def _sync_aiter(async_gen) -> Generator[str, None, None]:
    """Drive an async generator synchronously for use with st.write_stream().

    st.write_stream() expects a synchronous iterable of str chunks.
    We can't use asyncio.run() inside a running loop, so we create a dedicated
    event loop in the current thread to drain the async generator.
    """
    loop = asyncio.new_event_loop()
    try:
        while True:
            try:
                chunk = loop.run_until_complete(async_gen.__anext__())
                yield chunk
            except StopAsyncIteration:
                break
    finally:
        loop.close()


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

def _sync_api_keys():
    """Push sidebar API keys from session_state into os.environ for LLM clients."""
    import os
    key_map = {
        "groq_api_key": "GROQ_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "pinecone_api_key": "PINECONE_API_KEY",
    }
    for state_key, env_key in key_map.items():
        val = st.session_state.get(state_key, "")
        if val:
            os.environ[env_key] = val


def submit_query(user_query: str):
    """Process a new user query: add to history, run graph, stream response, store result."""
    _sync_api_keys()
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

        # Phase 2: Stream the final answer token-by-token using FallbackClient.astream()
        # This uses the upgraded astream() from 08-01 which yields str chunks.
        # st.write_stream() renders each chunk as it arrives and returns the full text.
        raw_answer = final_state.get("final_answer") or "No answer generated."
        streamed_text = _stream_final_answer(raw_answer)
        # streamed_text is the full accumulated string returned by st.write_stream()
        answer = streamed_text if streamed_text else raw_answer

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
    """Stream the final answer token-by-token using FallbackClient.astream() + st.write_stream().

    Uses get_llm(node='chat_stream') to obtain a configured FallbackClient,
    then streams the answer token-by-token via FallbackClient.astream().

    If FallbackClient is unavailable or streaming fails, falls back to st.markdown().

    Returns the full answer text (either from write_stream or the fallback).
    """
    try:
        from langchain_core.messages import HumanMessage

        # Use get_llm factory to get a configured FallbackClient
        client = get_llm(node="chat_stream")
        async_gen = client.astream([HumanMessage(content=answer_text)])
        # st.write_stream() requires a sync iterable — use the _sync_aiter adapter
        result = st.write_stream(_sync_aiter(async_gen))
        return result if isinstance(result, str) else answer_text
    except Exception as exc:
        logger.warning("FallbackClient.astream streaming failed (%s) — falling back to markdown", exc)
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


async def _astream_graph(initial_state: dict, config: dict, labels: list) -> dict:
    """Async: stream graph, collect node labels, then return the full checkpoint state.

    Uses stream_mode="updates" to get per-node labels for the status bar, then calls
    aget_state() to fetch the complete checkpoint state (includes initial-state-only fields
    like user_query and db_type that node output dicts never contain).
    """
    from graph.builder import compiled_graph as graph
    async for chunk in graph.astream(initial_state, config=config, stream_mode="updates"):
        for node_name in chunk:
            labels.append(NODE_LABELS.get(node_name, f"Running {node_name}..."))
    # Fetch full checkpoint state — captures every field, not just node output diffs
    snapshot = await graph.aget_state(config)
    return dict(snapshot.values) if snapshot else {}


def _run_graph_streaming(initial_state: dict, config: dict, status_container) -> dict:
    """Run the async graph in an isolated thread, updating status labels as nodes complete."""
    labels: list = []
    # Run graph in dedicated thread — isolates LangGraph ContextVars from Streamlit's loop
    final_state = _run_async(_astream_graph(initial_state, config, labels))
    # Replay node labels back into the status container from the main thread
    for label in labels:
        status_container.update(label=label, state="running")
    return final_state


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _build_initial_state(user_query: str) -> dict:
    """Build the initial AgentState dict for graph invocation.

    DatabaseManager is intentionally excluded — it is not msgpack-serializable
    and cannot be stored in LangGraph's MemorySaver checkpoint. Instead, the
    schema is extracted here and passed as a plain dict, and agent nodes that
    need to execute SQL use get_active_manager() from database.manager.
    """
    from database.manager import get_active_manager
    db_type = (st.session_state.get("sidebar_db_type") or "SQLite").lower()
    mgr = get_active_manager()
    schema = None
    if mgr is not None:
        try:
            schema = mgr.get_schema()
        except Exception as exc:
            logger.warning("_build_initial_state: failed to extract schema: %s", exc)
    return {
        "messages": [],
        "user_query": user_query,
        "db_type": db_type,
        "schema": schema,
        "retry_count": 0,
        # Explicitly reset per-query fields to prevent MemorySaver checkpoint leakage
        # across separate queries on the same thread_id.
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

    try:
        async def _resume():
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
