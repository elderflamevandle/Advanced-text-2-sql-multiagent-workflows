"""UI-002: Chat Interface tests."""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock


def test_build_initial_state_keys():
    """_build_initial_state returns dict with required keys and correct types."""
    import streamlit as st
    fake_session = {
        "sidebar_db_type": "SQLite",
        "db_manager": None,
    }
    with patch("streamlit.session_state", fake_session):
        from streamlit_app.components.chat import _build_initial_state
        state = _build_initial_state("Show total sales")
    assert state["user_query"] == "Show total sales"
    assert state["db_type"] == "sqlite"
    # DatabaseManager is deliberately not part of the state (not checkpoint-serializable).
    assert "db_manager" not in state
    assert state["retry_count"] == 0
    # Per-query fields are reset so nothing leaks from the previous query on the thread.
    for key in ("usage_metadata", "resolved_query", "generated_sql", "error_log",
                "db_results", "final_answer"):
        assert key in state and state[key] is None, key


def test_handle_graph_interrupt_stores_hitl_state():
    """_handle_graph_interrupt stores hitl_pending and hitl_config from GraphInterrupt."""
    from langgraph.errors import GraphInterrupt
    from streamlit_app.components.chat import _handle_graph_interrupt
    from types import SimpleNamespace

    # Build a fake GraphInterrupt with the expected structure
    mock_interrupt = MagicMock()
    mock_interrupt.value = {"generated_sql": "SELECT * FROM Artist", "sql_explanation": "all artists"}
    fake_exc = GraphInterrupt((mock_interrupt,))

    # Use SimpleNamespace so attribute assignment works (st.session_state uses attrs, not dict keys)
    fake_session = SimpleNamespace()
    config = {"configurable": {"thread_id": "test-thread"}}
    with patch("streamlit.session_state", fake_session):
        _handle_graph_interrupt(fake_exc, config)

    assert fake_session.hitl_pending == {"generated_sql": "SELECT * FROM Artist",
                                          "sql_explanation": "all artists"}
    assert fake_session.hitl_config == config


def test_update_session_cost_accumulates():
    """_update_session_cost adds tokens and cost across multiple entries."""
    from streamlit_app.components.chat import _update_session_cost
    from types import SimpleNamespace
    # Use SimpleNamespace so attribute access works (st.session_state uses attrs)
    fake_session = SimpleNamespace(session_tokens=0, session_cost=0.0)
    usage = [
        {"total_tokens": 300, "estimated_cost_usd": 0.0003},
        {"total_tokens": 150, "estimated_cost_usd": 0.0001},
    ]
    with patch("streamlit.session_state", fake_session):
        _update_session_cost(usage)
    assert fake_session.session_tokens == 450
    assert abs(fake_session.session_cost - 0.0004) < 1e-9


def test_render_hitl_card_importable():
    """render_hitl_card is callable (import-level check)."""
    from streamlit_app.components.chat import render_hitl_card
    assert callable(render_hitl_card)


def test_query_submission_adds_user_message():
    """submit_query adds a user message entry to session_state.messages."""
    # Unit-level: test _build_initial_state produces correct user_query
    import streamlit as st
    fake_session = {"sidebar_db_type": "SQLite", "db_manager": None}
    with patch("streamlit.session_state", fake_session):
        from streamlit_app.components.chat import _build_initial_state
        result = _build_initial_state("Which albums have 10 tracks?")
    assert result["user_query"] == "Which albums have 10 tracks?"


def test_stream_final_answer_displays_the_answer_without_llm_call():
    """_stream_final_answer renders final_answer itself and never calls an LLM."""
    import streamlit_app.components.chat as chat

    text = "Rock has the most tracks.\n\n- Rock: 1297\n- Latin: 579"
    captured = []

    def fake_write_stream(gen):
        captured.extend(gen)
        return "".join(captured)

    with patch("streamlit_app.components.chat.st.write_stream",
               side_effect=fake_write_stream) as write_stream, \
         patch("llm.fallback.get_llm") as get_llm, \
         patch("llm.fallback.FallbackClient.ainvoke") as ainvoke, \
         patch("llm.fallback.FallbackClient.astream") as astream:
        result = chat._stream_final_answer(text)

    assert write_stream.call_count == 1
    assert len(captured) > 1, "text should be yielded in several chunks"
    assert "".join(captured) == text
    assert result == text
    assert get_llm.call_count == 0
    assert ainvoke.call_count == 0 and astream.call_count == 0
    assert not hasattr(chat, "get_llm"), "chat.py must not import the LLM factory"


def test_stream_final_answer_falls_back_to_markdown():
    import streamlit_app.components.chat as chat

    with patch("streamlit_app.components.chat.st.write_stream",
               side_effect=RuntimeError("no script context")), \
         patch("streamlit_app.components.chat.st.markdown") as markdown:
        result = chat._stream_final_answer("plain answer")

    markdown.assert_called_once_with("plain answer")
    assert result == "plain answer"


def test_submit_query_shows_and_stores_final_answer_verbatim():
    """submit_query displays the graph's final_answer and stores exactly that text."""
    import streamlit_app.components.chat as chat
    from types import SimpleNamespace

    final_state = {
        "final_answer": "There are 275 artists.",
        "db_results": None,
        "usage_metadata": [{"total_tokens": 40, "estimated_cost_usd": 0.002}],
    }
    session = SimpleNamespace(messages=[], thread_id="t1", session_tokens=0,
                              session_cost=0.0, last_state=None)
    session.get = lambda key, default=None: getattr(session, key, default)
    st_mock = MagicMock()
    st_mock.session_state = session
    shown = []
    st_mock.write_stream.side_effect = lambda gen: shown.append("".join(gen))

    with patch.object(chat, "st", st_mock), \
         patch.object(chat, "_build_initial_state", return_value={}), \
         patch.object(chat, "_run_graph_streaming", return_value=final_state), \
         patch.object(chat, "_render_assistant_extras"), \
         patch("llm.fallback.get_llm") as get_llm:
        chat.submit_query("How many artists?")

    assert shown == ["There are 275 artists."]
    assert session.messages[-1]["content"] == "There are 275 artists."
    assert session.session_tokens == 40
    assert abs(session.session_cost - 0.002) < 1e-12
    assert get_llm.call_count == 0


def test_no_fake_quality_score_is_rendered():
    """The evaluator is a stub: without a ragas_score in state, no score widget appears."""
    import streamlit_app.components.chat as chat

    st_mock = MagicMock()
    with patch.object(chat, "st", st_mock):
        chat._render_assistant_extras({"final_answer": "x", "db_results": None}, msg_key="k")
    assert st_mock.progress.call_count == 0
    assert st_mock.metric.call_count == 0
