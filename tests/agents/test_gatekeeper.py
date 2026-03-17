"""AGENT-001 unit tests: gatekeeper_node classification, routing, and safety."""
import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from tests.agents.conftest import make_agent_state


def _make_llm_response(content: dict) -> MagicMock:
    """Return a mock LLM message with JSON content."""
    msg = MagicMock()
    msg.content = json.dumps(content)
    return msg


def _patch_get_llm(mock_llm):
    """Return a patch context that patches llm.fallback.get_llm to return mock_llm."""
    return patch("llm.fallback.get_llm", return_value=mock_llm)


def test_classifies_sql():
    """AGENT-001: Gatekeeper classifies SQL query and sets query_type='sql'."""
    import agents.nodes.gatekeeper as mod

    state = make_agent_state("show me total sales by region")
    llm_response = _make_llm_response(
        {"category": "sql", "intent": "user wants total sales", "response": ""}
    )

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=llm_response)

    with _patch_get_llm(mock_llm):
        result = asyncio.run(mod.gatekeeper_node(state))

    assert result.get("query_type") == "sql"
    assert "intent" in result


def test_classifies_conversational():
    """AGENT-001: Gatekeeper classifies conversational query, sets final_answer."""
    import agents.nodes.gatekeeper as mod

    state = make_agent_state("hello, how are you?")
    llm_response = _make_llm_response(
        {"category": "conversational", "intent": "greeting", "response": "Hello! How can I help?"}
    )

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=llm_response)

    with _patch_get_llm(mock_llm):
        result = asyncio.run(mod.gatekeeper_node(state))

    assert result.get("query_type") == "conversational"
    assert result.get("final_answer") == "Hello! How can I help?"


def test_follow_up_rewrite():
    """AGENT-001: Gatekeeper rewrites follow-up to resolved_query, preserves user_query."""
    import agents.nodes.gatekeeper as mod

    state = make_agent_state("what about last year?")
    classify_response = _make_llm_response(
        {"category": "follow_up", "intent": "filter by region", "response": ""}
    )
    rewrite_response = MagicMock()
    rewrite_response.content = "Show total sales for last year"

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=[classify_response, rewrite_response])

    with _patch_get_llm(mock_llm):
        result = asyncio.run(mod.gatekeeper_node(state))

    assert result.get("resolved_query") == "Show total sales for last year"
    # user_query must NOT be overwritten in returned dict
    assert "user_query" not in result


def test_ambiguous_clarification():
    """AGENT-001: Gatekeeper returns ambiguous with clarification in final_answer."""
    import agents.nodes.gatekeeper as mod

    state = make_agent_state("tell me something")
    llm_response = _make_llm_response(
        {"category": "ambiguous", "intent": "unclear", "response": "Could you clarify what you mean?"}
    )

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=llm_response)

    with _patch_get_llm(mock_llm):
        result = asyncio.run(mod.gatekeeper_node(state))

    assert result.get("query_type") == "ambiguous"
    assert "clarify" in result.get("final_answer", "").lower()


def test_no_db_manager():
    """AGENT-001: Gatekeeper returns early with connect message when db_manager is None."""
    import agents.nodes.gatekeeper as mod

    state = make_agent_state("show me sales")
    state["db_manager"] = None

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock()

    with _patch_get_llm(mock_llm):
        result = asyncio.run(mod.gatekeeper_node(state))
        # No LLM call should have occurred
        mock_llm.ainvoke.assert_not_called()

    assert "connect to a database" in result.get("final_answer", "").lower()
    assert result.get("query_type") == "conversational"


def test_blocks_destructive():
    """AGENT-001: Gatekeeper blocks destructive NL queries regardless of LLM classification."""
    import agents.nodes.gatekeeper as mod

    state = make_agent_state("delete all users from the database")
    llm_response = _make_llm_response(
        {"category": "sql", "intent": "delete users", "response": ""}
    )

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=llm_response)

    with _patch_get_llm(mock_llm):
        result = asyncio.run(mod.gatekeeper_node(state))

    assert "read-only" in result.get("final_answer", "").lower()
    assert result.get("query_type") == "conversational"


def test_prompt_constant_exists():
    """LLM-003: Module-level _GATEKEEPER_PROMPT constant must be a non-empty string."""
    import agents.nodes.gatekeeper as mod

    assert hasattr(mod, "_GATEKEEPER_PROMPT"), "_GATEKEEPER_PROMPT constant not found"
    assert isinstance(mod._GATEKEEPER_PROMPT, str), "_GATEKEEPER_PROMPT must be a string"
    assert len(mod._GATEKEEPER_PROMPT) > 0, "_GATEKEEPER_PROMPT must not be empty"
