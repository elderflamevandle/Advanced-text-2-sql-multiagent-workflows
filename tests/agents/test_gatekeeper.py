"""AGENT-001 unit tests: gatekeeper_node classification, routing, and safety."""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


def test_no_schema_returns_connect_message():
    """AGENT-001: Gatekeeper returns early with connect message when no schema is loaded.

    The node treats a missing schema as "no database connected" (db_manager is not kept
    in state because it is not serializable).
    """
    import agents.nodes.gatekeeper as mod

    state = make_agent_state("show me sales")
    state["schema"] = None

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock()

    with _patch_get_llm(mock_llm):
        result = asyncio.run(mod.gatekeeper_node(state))
        # No LLM call should have occurred
        mock_llm.ainvoke.assert_not_called()

    assert "connect to a database" in result.get("final_answer", "").lower()
    assert result.get("query_type") == "conversational"


def test_missing_db_manager_does_not_block_when_schema_present():
    """db_manager is irrelevant to the gatekeeper: a loaded schema means connected."""
    import agents.nodes.gatekeeper as mod

    state = make_agent_state("show me sales")
    state.pop("db_manager", None)

    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=_make_llm_response({"category": "sql", "intent": "sales", "response": ""})
    )

    with _patch_get_llm(mock_llm):
        result = asyncio.run(mod.gatekeeper_node(state))

    mock_llm.ainvoke.assert_called_once()
    assert result.get("query_type") == "sql"


@pytest.mark.parametrize("question", [
    "which customers dropped off last month",
    "show deleted accounts",
    "how many orders were removed all time vs truncated descriptions",
    "list tracks whose name contains 'Destroy' or 'Erase'",
    "what is the drop in revenue between Q1 and Q2",
    "show rows where delete_flag = 1",
    "delete all users from the database",
])
def test_question_wording_is_not_keyword_blocked(question):
    """Words like drop/delete in the question never short-circuit the gatekeeper.

    Read-only enforcement happens on the generated SQL (database.safety.scan_sql).
    """
    import agents.nodes.gatekeeper as mod

    state = make_agent_state(question)
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(
        return_value=_make_llm_response({"category": "sql", "intent": "lookup", "response": ""})
    )

    with _patch_get_llm(mock_llm):
        result = asyncio.run(mod.gatekeeper_node(state))

    mock_llm.ainvoke.assert_called_once()
    assert result.get("query_type") == "sql"
    assert "final_answer" not in result


def test_no_substring_filter_left_in_module():
    import agents.nodes.gatekeeper as mod

    assert not hasattr(mod, "_DESTRUCTIVE_PATTERNS")


def test_llm_can_still_decline_modification_requests():
    """A real modification request is declined by the classifier, not by keywords."""
    import agents.nodes.gatekeeper as mod

    state = make_agent_state("please delete all users")
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=_make_llm_response({
        "category": "conversational",
        "intent": "modify data",
        "response": "I can only run read-only queries.",
    }))

    with _patch_get_llm(mock_llm):
        result = asyncio.run(mod.gatekeeper_node(state))

    assert result.get("query_type") == "conversational"
    assert "read-only" in result.get("final_answer", "").lower()
    assert "read-only" in mod._GATEKEEPER_PROMPT


# ---------------------------------------------------------------------------
# Chat history rendering
# ---------------------------------------------------------------------------

def test_format_chat_history_renders_role_content_lines():
    from langchain_core.messages import AIMessage, HumanMessage

    import agents.nodes.gatekeeper as mod

    text = mod._format_chat_history([
        HumanMessage(content="total sales by region"),
        AIMessage(content="Here are the totals."),
        {"role": "user", "content": "and last year?"},
        ("assistant", "Last year was lower."),
    ])

    assert text.splitlines() == [
        "user: total sales by region",
        "assistant: Here are the totals.",
        "user: and last year?",
        "assistant: Last year was lower.",
    ]
    assert "HumanMessage" not in text


def test_format_chat_history_limits_to_last_six():
    import agents.nodes.gatekeeper as mod

    history = [{"role": "user", "content": f"q{i}"} for i in range(10)]
    lines = mod._format_chat_history(history).splitlines()

    assert lines == [f"user: q{i}" for i in range(4, 10)]


def test_format_chat_history_edge_cases():
    import agents.nodes.gatekeeper as mod

    assert mod._format_chat_history(None) == "(none)"
    assert mod._format_chat_history([]) == "(none)"
    # current question is not repeated as history
    assert mod._format_chat_history([{"role": "user", "content": "hi"}], "hi") == "(none)"
    # multi-part content and long messages
    text = mod._format_chat_history([
        {"role": "assistant", "content": [{"type": "text", "text": "part one"}, "part two"]},
        {"role": "user", "content": "x" * 5000},
    ])
    first, second = text.splitlines()
    assert first == "assistant: part one part two"
    assert len(second) < 600 and second.endswith("...")


def test_prompt_contains_readable_history_not_message_reprs():
    from langchain_core.messages import AIMessage, HumanMessage

    import agents.nodes.gatekeeper as mod

    state = make_agent_state("what about last year?")
    state["messages"] = [
        HumanMessage(content="total sales by region"),
        AIMessage(content="Here are the totals."),
        HumanMessage(content="what about last year?"),
    ]
    classify = _make_llm_response({"category": "follow_up", "intent": "x", "response": ""})
    rewrite = MagicMock()
    rewrite.content = "Total sales by region last year"
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=[classify, rewrite])

    with _patch_get_llm(mock_llm):
        result = asyncio.run(mod.gatekeeper_node(state))

    system_prompt = mock_llm.ainvoke.call_args_list[0].args[0][0].content
    rewrite_prompt = mock_llm.ainvoke.call_args_list[1].args[0][1].content
    for prompt in (system_prompt, rewrite_prompt):
        assert "user: total sales by region" in prompt
        assert "assistant: Here are the totals." in prompt
        assert "HumanMessage(" not in prompt and "AIMessage(" not in prompt
    assert result.get("resolved_query") == "Total sales by region last year"


def test_prompt_constant_exists():
    """LLM-003: Module-level _GATEKEEPER_PROMPT constant must be a non-empty string."""
    import agents.nodes.gatekeeper as mod

    assert hasattr(mod, "_GATEKEEPER_PROMPT"), "_GATEKEEPER_PROMPT constant not found"
    assert isinstance(mod._GATEKEEPER_PROMPT, str), "_GATEKEEPER_PROMPT must be a string"
    assert len(mod._GATEKEEPER_PROMPT) > 0, "_GATEKEEPER_PROMPT must not be empty"
