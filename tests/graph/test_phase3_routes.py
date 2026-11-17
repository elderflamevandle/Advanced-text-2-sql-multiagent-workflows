"""Clarification and table reselection routes through the real graph (fake LLM, fake
registry, fake catalog and memory services)."""
import asyncio
import uuid
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from agents import services
from graph.builder import build_graph
from graph.conditions import route_after_correction_plan, route_after_planner
from llm.fallback import FallbackClient
from llm.usage_tracker import UsageTracker
from tests.agents.phase3_fakes import FakeCatalog, FakeMemory, items_context

FULL_SCHEMA = {
    "items": {"columns": [{"name": "id", "type": "INTEGER"},
                          {"name": "owner", "type": "TEXT"}]},
    "owners": {"columns": [{"name": "name", "type": "TEXT"},
                           {"name": "region", "type": "TEXT"}]},
}
PLAN = '{"select": ["id"], "from": "items", "joins": [], "where": []}'
CLARIFY = ('{"select": [], "from": "items", "needs_clarification": true, '
           '"clarification_question": "Do you mean gross or net revenue?"}')
REPLIES = {
    "gatekeeper": '{"category": "sql", "intent": "x", "response": ""}',
    "query_planner": PLAN,
    "sql_generator": "SQL:\nSELECT id FROM items\nEXPLANATION:\nLists ids.",
    "formatter": "Done.",
    "correction_plan": ('{"error_category": "syntax_error", "root_cause": "typo", '
                        '"correction_steps": ["fix"], "affected_clauses": [], '
                        '"suggested_changes": {}, "confidence": "high"}'),
    "correction_sql": "SQL:\nSELECT id FROM items\nEXPLANATION:\nFixed.",
}


class Manager:
    """Fails with the queued errors first, then returns rows."""

    def __init__(self, errors=()):
        self.errors = list(errors)
        self.executed = []

    def execute_query(self, sql, timeout=None, max_rows=None):
        self.executed.append(sql)
        if self.errors:
            raise self.errors.pop(0)
        return [{"id": 1}]


class Registry:
    def __init__(self, manager):
        self.manager = manager

    def get_manager(self, tenant_id, connection_id):
        return self.manager

    def get_schema(self, tenant_id, connection_id, *, refresh=False):
        return FULL_SCHEMA


class OperationalError(Exception):
    pass


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    import agents.nodes.hitl as hitl
    from database import registry

    monkeypatch.setattr(hitl, "_load_hitl_config", lambda: {"enabled": False})
    yield
    registry.set_registry(None)


def _run(manager, replies=None, *, clarification=True, question="list the items",
         thread_id=None, graph=None):
    from database import registry
    import agents.nodes.query_planner as planner

    replies = dict(REPLIES, **(replies or {}))
    calls = []
    prompts = {}

    def get_llm(node, state=None):
        calls.append(node)
        reply = replies[node]

        # list: one reply per get_llm call; tuple: successive replies of one client.
        text = reply.pop(0) if isinstance(reply, list) else reply
        texts = list(text) if isinstance(text, tuple) else [text] * 4
        model = GenericFakeChatModel(messages=iter([AIMessage(content=t) for t in texts]))
        client = FallbackClient(model, None, None, UsageTracker(), node_name=node, state=state)
        original = client.ainvoke

        async def ainvoke(messages, *args, **kwargs):
            prompts.setdefault(node, []).append(messages[0].content)
            return await original(messages, *args, **kwargs)

        client.ainvoke = ainvoke
        return client

    registry.set_registry(Registry(manager))
    state = {"messages": [], "user_query": question, "db_type": "sqlite", "schema": None,
             "tenant_id": "t1", "connection_id": "c1", "retry_count": 0,
             "usage_metadata": None, "error_log": None, "sql_history": [],
             "final_answer": None, "db_results": None, "generated_sql": None,
             "correction_plan": None, "resolved_query": None}
    config = {"configurable": {"thread_id": thread_id or f"t1:{uuid.uuid4().hex}"}}
    graph = graph or build_graph()
    with patch("llm.fallback.get_llm", get_llm), \
         patch.object(planner, "_clarification_enabled", lambda: clarification), \
         patch("vector.retriever.get_retriever", side_effect=RuntimeError("no vectors")):
        result = asyncio.run(graph.ainvoke(state, config=config))
    return result, calls, prompts


# -- clarification ---------------------------------------------------------------

def test_clarification_ends_the_run_without_sql():
    memory = FakeMemory()
    services.set_memory_service_for_tests(memory)
    manager = Manager()
    result, calls, _ = _run(manager, {"query_planner": CLARIFY}, clarification=True)
    assert calls == ["gatekeeper", "query_planner"], "no SQL generation, no summary call"
    assert manager.executed == [] and result["generated_sql"] is None
    assert result["query_type"] == "clarification"
    assert result["clarification_question"] == "Do you mean gross or net revenue?"
    assert result["final_answer"] == "Do you mean gross or net revenue?"
    assert memory.logged == [], "nothing to log: no SQL was produced"
    assert result["messages"][-1].additional_kwargs == {"clarification": True}


def test_clarification_flag_off_runs_the_query():
    manager = Manager()
    result, calls, prompts = _run(manager, {"query_planner": CLARIFY}, clarification=False)
    assert "sql_generator" in calls and len(manager.executed) == 1
    assert result["final_answer"] == "Done." and not result.get("clarification_question")
    assert "needs_clarification" not in prompts["query_planner"][0]


def test_answer_to_a_clarification_is_folded_into_the_next_turn():
    graph = build_graph()
    thread = f"t1:{uuid.uuid4().hex}"
    manager = Manager()
    _run(manager, {"query_planner": CLARIFY}, question="revenue by region",
         thread_id=thread, graph=graph)
    # Second turn: classify, then the follow-up rewrite (same fake "gatekeeper" model).
    replies = {"gatekeeper": (REPLIES["gatekeeper"], "Net revenue by region")}
    result, calls, prompts = _run(manager, replies, question="net", thread_id=thread,
                                  graph=graph)
    assert calls[:2] == ["gatekeeper", "query_planner"]
    assert result["query_type"] == "follow_up"
    assert result["resolved_query"] == "Net revenue by region"
    assert result["clarification_question"] is None and result["final_answer"] == "Done."
    assert len(manager.executed) == 1 and manager.executed[0].startswith("SELECT id FROM")


def test_route_after_planner():
    assert route_after_planner({}) == "continue"
    assert route_after_planner({"query_type": "clarification",
                                "clarification_question": "Which?"}) == "formatter"
    assert route_after_planner({"query_type": "sql",
                                "clarification_question": "stale"}) == "continue"
    assert route_after_planner(
        {"error_log": {"error_type": "llm_all_providers_failed"}}) == "formatter"


# -- reselection -------------------------------------------------------------------

def _catalog():
    def context(widen, must_include, **_):
        return items_context(("items", "owners") if widen else ("items",))

    return FakeCatalog(context)


def test_missing_table_selects_tables_again_once():
    catalog, memory = _catalog(), FakeMemory()
    services.set_catalog_service_for_tests(catalog)
    services.set_memory_service_for_tests(memory)
    manager = Manager([OperationalError("no such table: owners")])
    replies = {"sql_generator": ["SQL:\nSELECT region FROM owners\nEXPLANATION:\nx",
                                 "SQL:\nSELECT id FROM items\nEXPLANATION:\nx"]}
    result, calls, prompts = _run(manager, replies)

    assert calls == ["gatekeeper", "query_planner", "sql_generator",
                     "query_planner", "sql_generator", "formatter"], \
        "reselection makes no correction LLM call and goes through planner and generator"
    assert [(c["widen"], c["must_include"]) for c in catalog.select_calls] == [
        (False, ()), (True, ("owners",))]
    assert result["reselect_count"] == 1 and result["required_tables"] == ["owners"]
    assert result["relevant_tables"] == ["items", "owners"]
    assert result["retry_count"] == 1 and result["error_log"] is None
    assert result["db_results"] == [{"id": 1}]
    assert len(result["sql_history"]) == 1
    assert result["sql_history"][0]["sql"] == "SELECT region FROM owners"
    assert "no such table: owners" in result["sql_history"][0]["error"]["message"]
    assert result["correction_plan"]["action"] == "reselect"
    # The second planner pass is told what failed and sees the wider context.
    assert "A previous attempt failed" in prompts["query_planner"][1]
    assert "CATALOG TABLE owners" in prompts["query_planner"][1]
    assert "A previous attempt failed" not in prompts["query_planner"][0]
    # Learning: the run is logged and the corrected run leaves exactly one lesson.
    assert len(memory.logged) == 1 and memory.logged[0]["status"] == "completed"
    assert result["query_id"] == "q1"
    assert len(memory.added_lessons) == 1
    assert "not in the tables selected" in memory.added_lessons[0]["text"]


def test_second_missing_table_error_uses_the_normal_correction_loop():
    catalog = _catalog()
    services.set_catalog_service_for_tests(catalog)
    manager = Manager([OperationalError("no such table: owners"),
                       OperationalError("no such column: regionn")])
    result, calls, _ = _run(manager)
    assert len(catalog.select_calls) == 2, "table selection is redone at most once"
    assert calls.count("correction_plan") == 1 and calls.count("correction_sql") == 1
    assert result["retry_count"] == 2 and len(result["sql_history"]) == 2
    assert result["db_results"] == [{"id": 1}]


def test_missing_column_names_the_tables_that_have_it():
    catalog = _catalog()
    services.set_catalog_service_for_tests(catalog)
    manager = Manager([OperationalError("no such column: region")])
    _run(manager)
    assert catalog.select_calls[1]["must_include"] == ("owners",)
    assert catalog.select_calls[1]["widen"] is True


def test_other_errors_do_not_reselect():
    catalog = _catalog()
    services.set_catalog_service_for_tests(catalog)
    manager = Manager([OperationalError('near "SELEC": syntax error')])
    result, calls, _ = _run(manager)
    assert len(catalog.select_calls) == 1
    assert calls.count("correction_plan") == 1 and calls.count("correction_sql") == 1
    assert not result.get("reselect_count")


def test_missing_table_without_a_catalog_does_not_reselect():
    manager = Manager([OperationalError("no such table: owners")])
    result, calls, _ = _run(manager)  # no catalog service: legacy schema path
    assert calls.count("correction_plan") == 1 and calls.count("correction_sql") == 1
    assert not result.get("reselect_count") and result["db_results"] == [{"id": 1}]


def test_route_after_correction_plan():
    assert route_after_correction_plan({"correction_plan": {"action": "reselect"}}) \
        == "schema_linker"
    assert route_after_correction_plan({"correction_plan": {"x": 1}}) == "continue"
    assert route_after_correction_plan({}) == "continue"
    assert route_after_correction_plan(
        {"correction_plan": {"action": "reselect"},
         "error_log": {"error_type": "llm_all_providers_failed"}}) == "formatter"


def test_registered_connection_runs_with_no_schema_in_the_initial_state():
    manager = Manager()
    result, _, prompts = _run(manager)  # initial state has schema None, no catalog
    assert result["db_results"] == [{"id": 1}]
    assert set(result["schema"]) == {"items", "owners"}, "loaded by the linker (registry)"
    assert "Table items: id INTEGER, owner TEXT" in prompts["query_planner"][0]
