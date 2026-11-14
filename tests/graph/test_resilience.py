"""Tests for graph.resilience.resilient_node (the wrapper applied to every graph node)."""
import asyncio

from graph.resilience import resilient_node
from llm.errors import LLMUnavailableError


def _entry(node):
    return {"node_name": node, "total_tokens": 15, "estimated_cost_usd": 0.001}


def test_passes_through_normal_result():
    async def node(state):
        return {"query_plan": {"from": "Artist"}}

    result = asyncio.run(resilient_node(node)({"user_query": "q"}))
    assert result == {"query_plan": {"from": "Artist"}}


def test_converts_llm_unavailable_to_error_log():
    async def node(state):
        raise LLMUnavailableError("All LLM providers failed (groq). Last error: boom")

    result = asyncio.run(resilient_node(node, "sql_generator")({"db_type": "duckdb"}))
    assert result == {
        "error_log": {
            "error_type": "llm_all_providers_failed",
            "message": "All LLM providers failed (groq). Last error: boom",
            "dialect": "duckdb",
            "failed_sql": None,
            "hint": "Check GROQ_API_KEY, OPENAI_API_KEY, and Ollama server status.",
        }
    }


def test_other_exceptions_propagate():
    async def node(state):
        raise ValueError("a real bug")

    try:
        asyncio.run(resilient_node(node)({}))
    except ValueError as exc:
        assert "a real bug" in str(exc)
    else:
        raise AssertionError("ValueError should propagate")


def test_propagates_only_new_usage_entries_and_does_not_mutate_state():
    async def node(state):
        # What UsageTracker.record does: rebind the key on the state it was given.
        state["usage_metadata"] = (state.get("usage_metadata") or []) + [_entry("planner")]
        return {"query_plan": {}}

    original = {"usage_metadata": [_entry("gatekeeper")], "user_query": "q"}
    result = asyncio.run(resilient_node(node)(original))
    assert result["usage_metadata"] == [_entry("planner")]
    assert result["query_plan"] == {}
    assert original["usage_metadata"] == [_entry("gatekeeper")]


def test_usage_recorded_before_an_outage_is_kept():
    async def node(state):
        state["usage_metadata"] = (state.get("usage_metadata") or []) + [_entry("first_call")]
        raise LLMUnavailableError("down")

    result = asyncio.run(resilient_node(node)({"usage_metadata": None, "db_type": "sqlite"}))
    assert result["error_log"]["error_type"] == "llm_all_providers_failed"
    assert result["usage_metadata"] == [_entry("first_call")]


def test_no_usage_key_when_node_made_no_llm_call():
    async def node(state):
        return {}

    assert asyncio.run(resilient_node(node)({"usage_metadata": [_entry("x")]})) == {}


def test_real_fallback_client_usage_reaches_wrapper_output():
    """get_llm-style client created with the node's state: its entry is returned."""
    from unittest.mock import AsyncMock, MagicMock

    from llm.fallback import FallbackClient
    from llm.usage_tracker import UsageTracker

    response = MagicMock()
    response.content = "ok"
    response.usage_metadata = {"input_tokens": 7, "output_tokens": 3}
    provider = MagicMock()
    provider.ainvoke = AsyncMock(return_value=response)
    provider.model_name = "llama-3.3-70b-versatile"

    async def node(state):
        client = FallbackClient(provider, None, None, UsageTracker(), "planner", state=state)
        await client.ainvoke(["hi"])  # no state= argument, like the real nodes
        return {"query_plan": {}}

    result = asyncio.run(resilient_node(node)({"usage_metadata": None}))
    assert [e["node_name"] for e in result["usage_metadata"]] == ["planner"]
    assert result["usage_metadata"][0]["total_tokens"] == 10


def test_wrapper_resolves_patched_node_at_call_time(monkeypatch):
    """Patching a node at its module path affects an already-wrapped node."""
    import agents.nodes.evaluator as evaluator_module

    wrapped = resilient_node(evaluator_module.evaluator_node, "evaluator")

    async def patched(state):
        return {"final_answer": "patched"}

    monkeypatch.setattr(evaluator_module, "evaluator_node", patched)
    assert asyncio.run(wrapped({})) == {"final_answer": "patched"}
