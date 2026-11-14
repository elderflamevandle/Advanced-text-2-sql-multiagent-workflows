"""End-to-end graph runs with fake LLMs and a mocked database manager (no network, no DB)."""
import asyncio
import uuid
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from graph.builder import compiled_graph
from llm.errors import LLMUnavailableError
from llm.fallback import FallbackClient
from llm.usage_tracker import UsageTracker

SCHEMA = {"Artist": {"columns": [{"name": "Name", "type": "TEXT"}]}}
ROWS = [{"Name": "AC/DC"}, {"Name": "Accept"}]

REPLIES = {
    "gatekeeper": '{"category": "sql", "intent": "list artists", "response": ""}',
    "query_planner": (
        '{"select": ["Name"], "from": "Artist", "joins": [], "where": [], "group_by": [], '
        '"order_by": [], "limit": null, "ctes": [], "complexity": "simple"}'
    ),
    "sql_generator": "SQL:\nSELECT Name FROM Artist\nEXPLANATION:\nLists artist names.",
    "formatter": "There are two artists: AC/DC and Accept.",
    "correction_plan": (
        '{"error_category": "syntax_error", "root_cause": "typo", "correction_steps": ["fix"], '
        '"affected_clauses": [], "suggested_changes": {}, "confidence": "high"}'
    ),
    "correction_sql": "SQL:\nSELECT Name FROM Artist\nEXPLANATION:\nFixed.",
}


class _DownModel:
    async def ainvoke(self, messages):
        raise ConnectionError("provider unreachable")


def _fake_model(content: str):
    message = AIMessage(
        content=content,
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    model = GenericFakeChatModel(messages=iter([message] * 4))
    return model


def _fake_get_llm(down_nodes=()):
    calls = []

    def get_llm(node, state=None):
        calls.append(node)
        provider = _DownModel() if node in down_nodes else _fake_model(REPLIES[node])
        return FallbackClient(provider, None, None, UsageTracker(), node_name=node, state=state)

    get_llm.calls = calls
    return get_llm


def _initial_state(**overrides):
    state = {
        "messages": [],
        "user_query": "list all artists",
        "db_type": "sqlite",
        "schema": SCHEMA,
        "retry_count": 0,
        "usage_metadata": None,
        "error_log": None,
        "sql_history": [],
        "final_answer": None,
        "db_results": None,
    }
    state.update(overrides)
    return state


def _run(get_llm, manager, state=None, thread_id=None):
    config = {"configurable": {"thread_id": thread_id or str(uuid.uuid4())}}
    with patch("llm.fallback.get_llm", get_llm), \
         patch("database.manager.get_active_manager", return_value=manager), \
         patch("vector.retriever.get_retriever", side_effect=RuntimeError("no vector store")):
        return asyncio.run(compiled_graph.ainvoke(state or _initial_state(), config=config))


@pytest.fixture
def manager():
    mgr = MagicMock()
    mgr.execute_query.return_value = list(ROWS)
    return mgr


@pytest.fixture(autouse=True)
def _hitl_disabled(monkeypatch):
    """Keep runs independent of the hitl section of the repo config."""
    import agents.nodes.hitl as hitl
    monkeypatch.setattr(hitl, "_load_hitl_config", lambda: {"enabled": False})


def test_success_run_sets_final_answer_and_accumulates_usage(manager):
    get_llm = _fake_get_llm()
    result = _run(get_llm, manager)

    assert result["final_answer"] == "There are two artists: AC/DC and Accept."
    assert result["db_results"] == ROWS
    assert result["error_log"] is None
    assert manager.execute_query.call_count == 1

    usage = result["usage_metadata"]
    assert [e["node_name"] for e in usage] == [
        "gatekeeper", "query_planner", "sql_generator", "formatter"]
    assert all(e["total_tokens"] == 15 for e in usage)
    assert sum(e["total_tokens"] for e in usage) == 60


def test_usage_resets_between_queries_on_the_same_thread(manager):
    """A new query passes usage_metadata=None, so the UI never double-counts cost."""
    thread_id = str(uuid.uuid4())
    first = _run(_fake_get_llm(), manager, thread_id=thread_id)
    second = _run(_fake_get_llm(), manager, thread_id=thread_id)
    assert len(first["usage_metadata"]) == 4
    assert len(second["usage_metadata"]) == 4


def test_llm_outage_in_gatekeeper_goes_straight_to_formatter(manager):
    get_llm = _fake_get_llm(down_nodes={"gatekeeper"})
    result = _run(get_llm, manager)

    assert get_llm.calls == ["gatekeeper"], "no other node may call the LLM after the outage"
    assert manager.execute_query.call_count == 0
    assert result["error_log"]["error_type"] == "llm_all_providers_failed"
    assert "no language model provider is reachable" in result["final_answer"]
    assert "attempt(s)" not in result["final_answer"]


def test_llm_outage_in_sql_generator_skips_execution_and_keeps_usage(manager):
    get_llm = _fake_get_llm(down_nodes={"sql_generator"})
    result = _run(get_llm, manager)

    assert get_llm.calls == ["gatekeeper", "query_planner", "sql_generator"]
    assert manager.execute_query.call_count == 0
    assert result["error_log"]["error_type"] == "llm_all_providers_failed"
    assert result["retry_count"] == 0, "the correction loop must not run"
    assert [e["node_name"] for e in result["usage_metadata"]] == ["gatekeeper", "query_planner"]
    assert "no language model provider is reachable" in result["final_answer"]


def test_formatter_outage_falls_back_to_deterministic_summary(manager):
    get_llm = _fake_get_llm(down_nodes={"formatter"})
    result = _run(get_llm, manager)

    assert result["error_log"] is None
    assert "2 rows" in result["final_answer"]
    assert "AC/DC" in result["final_answer"]


def test_correction_loop_stops_at_configured_retry_limit(manager, monkeypatch):
    import graph.conditions as conditions
    monkeypatch.setattr(conditions, "get_max_correction_retries", lambda: 2)
    manager.execute_query.side_effect = RuntimeError("syntax error near FROM")

    result = _run(_fake_get_llm(), manager)

    assert manager.execute_query.call_count == 3  # first try + 2 corrections
    assert result["retry_count"] == 2
    assert result["db_results"] is None
    assert "couldn't generate a working query" in result["final_answer"]


def test_llm_outage_inside_correction_loop_ends_the_run(manager):
    manager.execute_query.side_effect = RuntimeError("syntax error near FROM")
    get_llm = _fake_get_llm(down_nodes={"correction_sql"})
    result = _run(get_llm, manager)

    assert manager.execute_query.call_count == 1
    assert result["error_log"]["error_type"] == "llm_all_providers_failed"
    assert "no language model provider is reachable" in result["final_answer"]


def test_fallback_client_error_type_is_what_the_graph_handles():
    async def _call():
        await FallbackClient(_DownModel(), None, None, UsageTracker(), "n").ainvoke(["x"])

    with pytest.raises(LLMUnavailableError):
        asyncio.run(_call())
