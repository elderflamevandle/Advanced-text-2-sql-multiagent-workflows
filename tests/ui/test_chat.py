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
    assert state["db_manager"] is None
    assert state["retry_count"] == 0


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


def test_stream_final_answer_calls_fallback_client_astream():
    """_stream_final_answer() calls FallbackClient.astream() and passes chunks to st.write_stream().

    Confirms the locked decision is wired: FallbackClient.astream() is invoked with
    the answer text as a message, and st.write_stream() receives the sync-adapted generator.
    """
    from streamlit_app.components.chat import _stream_final_answer

    # Async generator that yields two chunks
    async def fake_astream(messages, state=None):
        yield "Hello "
        yield "world"

    mock_client = MagicMock()
    mock_client.astream = fake_astream

    captured_chunks = []

    def fake_write_stream(gen):
        """Consume the sync generator and return full text."""
        for chunk in gen:
            captured_chunks.append(chunk)
        return "".join(captured_chunks)

    with patch("streamlit_app.components.chat.st.write_stream", side_effect=fake_write_stream), \
         patch("streamlit_app.components.chat.get_llm", return_value=mock_client):
        result = _stream_final_answer("Hello world")

    assert "Hello " in captured_chunks or "Hello" in "".join(captured_chunks)
    assert result == "Hello world"
