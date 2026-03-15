"""Unit tests for agents/nodes/query_planner.py — AGENT-003 / LLM-003."""
import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.agents.conftest import make_agent_state


def _make_llm_mock(content: str) -> MagicMock:
    """Return a fully configured ChatGroq mock whose ainvoke returns content."""
    mock_response = MagicMock()
    mock_response.content = content
    mock_llm_instance = MagicMock()
    mock_llm_instance.ainvoke = AsyncMock(return_value=mock_response)
    mock_llm_class = MagicMock(return_value=mock_llm_instance)
    return mock_llm_class


def _inject_chatgroq_mock(mock_class: MagicMock):
    """Inject mock into sys.modules so lazy import inside function picks it up."""
    fake_module = types.ModuleType("langchain_groq")
    fake_module.ChatGroq = mock_class
    sys.modules["langchain_groq"] = fake_module

    # Force reload so the module re-executes its lazy import
    import agents.nodes.query_planner as qp_mod
    importlib.reload(qp_mod)
    return qp_mod


# ---------------------------------------------------------------------------
# test_prompt_constant_exists
# ---------------------------------------------------------------------------

def test_prompt_constant_exists():
    """_PLANNER_PROMPT module-level constant must be a non-empty string (LLM-003)."""
    import agents.nodes.query_planner as qp_mod
    assert hasattr(qp_mod, "_PLANNER_PROMPT"), "_PLANNER_PROMPT constant missing"
    assert isinstance(qp_mod._PLANNER_PROMPT, str), "_PLANNER_PROMPT must be a str"
    assert len(qp_mod._PLANNER_PROMPT.strip()) > 0, "_PLANNER_PROMPT must be non-empty"


# ---------------------------------------------------------------------------
# test_returns_json_plan
# ---------------------------------------------------------------------------

def test_returns_json_plan():
    """query_planner_node returns a dict with all required plan keys."""
    llm_json = (
        '{"select": ["Total"], "from": "Invoice", "joins": [], '
        '"where": [], "group_by": [], "order_by": [], '
        '"limit": null, "ctes": [], "complexity": "simple"}'
    )
    mock_class = _make_llm_mock(llm_json)
    qp_mod = _inject_chatgroq_mock(mock_class)

    state = make_agent_state(user_query="What is the total revenue?")
    result = asyncio.run(qp_mod.query_planner_node(state))

    assert "query_plan" in result
    plan = result["query_plan"]
    assert isinstance(plan, dict)
    required_keys = {"select", "from", "joins", "where", "group_by", "order_by", "limit", "ctes", "complexity"}
    missing = required_keys - set(plan.keys())
    assert not missing, f"Missing keys in query_plan: {missing}"


# ---------------------------------------------------------------------------
# test_malformed_json_fallback
# ---------------------------------------------------------------------------

def test_malformed_json_fallback():
    """query_planner_node falls back to _DEFAULT_PLAN when LLM returns invalid JSON."""
    mock_class = _make_llm_mock("Here is the plan: {invalid json...")
    qp_mod = _inject_chatgroq_mock(mock_class)

    state = make_agent_state(user_query="malformed test")
    result = asyncio.run(qp_mod.query_planner_node(state))

    assert "query_plan" in result
    plan = result["query_plan"]
    assert isinstance(plan, dict)
    assert plan.get("complexity") == "simple"
    assert plan.get("select") == []
    assert plan.get("limit") is None


# ---------------------------------------------------------------------------
# test_strips_json_fences
# ---------------------------------------------------------------------------

def test_strips_json_fences():
    """query_planner_node strips markdown code fences before parsing."""
    llm_output = (
        "```json\n"
        '{"select": ["*"], "from": "Invoice", "joins": [], "where": [], '
        '"group_by": [], "order_by": [], "limit": 10, "ctes": [], "complexity": "simple"}'
        "\n```"
    )
    mock_class = _make_llm_mock(llm_output)
    qp_mod = _inject_chatgroq_mock(mock_class)

    state = make_agent_state(user_query="Get all invoices")
    result = asyncio.run(qp_mod.query_planner_node(state))

    plan = result["query_plan"]
    assert isinstance(plan, dict)
    assert plan.get("select") == ["*"]
    assert plan.get("limit") == 10


# ---------------------------------------------------------------------------
# test_uses_resolved_query
# ---------------------------------------------------------------------------

def test_uses_resolved_query():
    """query_planner_node prefers resolved_query over user_query when building the prompt."""
    llm_json = (
        '{"select": ["Total"], "from": "Invoice", "joins": [], '
        '"where": [], "group_by": [], "order_by": [], '
        '"limit": null, "ctes": [], "complexity": "simple"}'
    )
    mock_class = _make_llm_mock(llm_json)
    qp_mod = _inject_chatgroq_mock(mock_class)

    state = make_agent_state(user_query="original question")
    state["resolved_query"] = "standalone rewritten question"

    asyncio.run(qp_mod.query_planner_node(state))

    # Inspect call args: HumanMessage content should contain the resolved_query
    mock_llm_instance = mock_class.return_value
    call_args = mock_llm_instance.ainvoke.call_args
    assert call_args is not None
    messages = call_args[0][0]  # first positional arg = list of messages
    human_msgs = [m for m in messages if hasattr(m, "content") and "standalone rewritten question" in m.content]
    assert human_msgs, "resolved_query value not found in HumanMessage content"
