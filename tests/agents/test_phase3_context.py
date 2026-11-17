"""Schema catalog and query memory in the nodes: linker, prompts, evaluator, gatekeeper."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import database.registry
import pytest

from agents import services
from agents.prompt_context import (examples_prompt, lessons_prompt, render_legacy_schema,
                                   schema_prompt)
from tests.agents.conftest import make_agent_state
from tests.agents.phase3_fakes import FakeCatalog, FakeExample, FakeMemory, items_context

LEGACY = {"Invoice": {"columns": [{"name": "Total", "type": "NUMERIC"}]}}


@pytest.fixture(autouse=True)
def _no_real_registry(monkeypatch):
    def unavailable():
        raise NotImplementedError("no registry in these tests")

    monkeypatch.setattr(database.registry, "get_registry", unavailable)


def _state(**overrides):
    state = make_agent_state("how many items per owner")
    state.update(tenant_id="t1", connection_id="c1", schema=None, relevant_tables=None)
    state.update(overrides)
    return state


def _link(state):
    from agents.nodes.schema_linker import schema_linker_node

    with patch("vector.retriever.get_retriever", side_effect=RuntimeError("no vectors")):
        return asyncio.run(schema_linker_node(state))


# -- schema linker -------------------------------------------------------------

def test_linker_uses_the_catalog_context():
    catalog = FakeCatalog(items_context())
    services.set_catalog_service_for_tests(catalog)
    result = _link(_state())

    assert result["relevant_tables"] == ["items"]
    assert set(result["schema"]) == {"items"}
    assert result["schema_context"].startswith("CATALOG TABLE items")
    assert result["join_hints"][0]["on"] == "items.owner = owners.name"
    assert catalog.select_calls == [{"tenant_id": "t1", "connection_id": "c1",
                                     "question": "how many items per owner",
                                     "widen": False, "must_include": ()}]


def test_linker_passes_widen_and_required_tables_on_reselection():
    catalog = FakeCatalog(items_context())
    services.set_catalog_service_for_tests(catalog)
    _link(_state(reselect_count=1, required_tables=["owners"], resolved_query="rewritten"))
    call = catalog.select_calls[0]
    assert call["widen"] is True and call["must_include"] == ("owners",)
    assert call["question"] == "rewritten"


@pytest.mark.parametrize("catalog", [
    None,                                            # module missing / not installed
    FakeCatalog(None),                               # no built catalog
    FakeCatalog(error=RuntimeError("boom")),         # service failure
    FakeCatalog(error=NotImplementedError()),        # interface stub
])
def test_linker_falls_back_to_the_legacy_path(catalog):
    services.set_catalog_service_for_tests(catalog)
    result = _link(_state(schema=LEGACY))
    assert result["relevant_tables"] == ["Invoice"] and result["schema"] == LEGACY
    assert "schema_context" not in result and "join_hints" not in result


def test_linker_ignores_the_catalog_without_a_connection_id():
    catalog = FakeCatalog(items_context())
    services.set_catalog_service_for_tests(catalog)
    result = _link(_state(connection_id=None, schema=LEGACY))
    assert catalog.select_calls == [] and result["schema"] == LEGACY
    assert "examples" not in result


def test_linker_fetches_examples_and_lessons():
    memory = FakeMemory(examples=[FakeExample("q1", "SELECT 1", verified=True, score=0.9)],
                        lessons=["Use owner, not owner_name."])
    services.set_memory_service_for_tests(memory)
    result = _link(_state(schema=LEGACY))
    assert result["examples"] == [{"question": "q1", "sql": "SELECT 1", "verified": True,
                                   "score": 0.9}]
    assert result["lessons"] == ["Use owner, not owner_name."]
    assert memory.example_calls == [("t1", "c1", "how many items per owner", 3)]
    assert memory.lesson_calls == [("t1", "c1", 8)]


def test_linker_survives_a_raising_memory_service():
    services.set_catalog_service_for_tests(FakeCatalog(items_context()))
    services.set_memory_service_for_tests(FakeMemory(error=RuntimeError("down")))
    result = _link(_state())
    assert result["relevant_tables"] == ["items"]
    assert result["examples"] == [] and result["lessons"] == []


def test_services_return_none_when_the_module_is_missing(monkeypatch):
    import sys

    services.reset_services_for_tests()
    monkeypatch.setitem(sys.modules, "catalog.service", None)  # import raises ImportError
    monkeypatch.setitem(sys.modules, "memory.service", None)
    assert services.catalog_service() is None and services.memory_service() is None


# -- prompt helpers -------------------------------------------------------------

def test_schema_prompt_prefers_the_catalog_context():
    assert schema_prompt({"schema_context": "CTX", "schema": LEGACY}) == "CTX"


def test_legacy_rendering_includes_keys():
    schema = {"Track": {"columns": [{"name": "TrackId", "type": "INTEGER"},
                                    {"name": "AlbumId", "type": "INTEGER"}],
                        "primary_keys": ["TrackId"],
                        "foreign_keys": [{"column": "AlbumId", "references_table": "Album",
                                          "references_column": "AlbumId"}]}}
    text = schema_prompt({"schema": schema})
    assert "Table Track: TrackId INTEGER, AlbumId INTEGER" in text
    assert "Primary key: TrackId" in text
    assert "Foreign keys: Track.AlbumId -> Album.AlbumId" in text
    # Without keys the rendering is the old one-line form.
    assert render_legacy_schema(LEGACY) == "Table Invoice: Total NUMERIC"
    assert render_legacy_schema({}) == "(no schema provided)"


def test_examples_prompt_puts_verified_first_and_caps():
    examples = [{"question": f"q{i}", "sql": f"SELECT {i}", "verified": i == 3}
                for i in range(5)]
    text = examples_prompt({"examples": examples})
    assert text.index("q3") < text.index("q0") and "(verified by a user)" in text
    assert text.count("SQL: SELECT") == 3 and "q4" not in text
    assert examples_prompt({}) == "" and examples_prompt({"examples": []}) == ""


def test_lessons_prompt():
    assert lessons_prompt({}) == ""
    text = lessons_prompt({"lessons": ["  a  b ", "", "c"]})
    assert text.endswith("- a b\n- c")


# -- prompts of the nodes --------------------------------------------------------

def _captured_system_prompt(node_module, node_name, state, reply):
    captured = {}

    async def ainvoke(messages):
        captured["system"] = messages[0].content
        return MagicMock(content=reply)

    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=ainvoke)
    with patch("llm.fallback.get_llm", return_value=llm):
        result = asyncio.run(getattr(node_module, node_name)(state))
    return captured["system"], result


RICH = dict(
    schema_context="CATALOG TABLE items (id INTEGER, owner TEXT)",
    join_hints=[{"from": "items", "to": "owners", "on": "items.owner = owners.name",
                 "kind": "fk"}],
    examples=[{"question": "count the items", "sql": "SELECT COUNT(*) FROM items",
               "verified": True}],
    lessons=["owner holds the login name."],
)
PLAN = '{"select": ["id"], "from": "items"}'


@pytest.mark.parametrize("module_name,node_name,reply,has_examples", [
    ("agents.nodes.query_planner", "query_planner_node", PLAN, True),
    ("agents.nodes.sql_generator", "sql_generator_node",
     "SQL:\nSELECT id FROM items\nEXPLANATION:\nx", True),
    ("agents.nodes.correction_sql", "correction_sql_node",
     "SQL:\nSELECT id FROM items\nEXPLANATION:\nx", False),
])
def test_prompts_carry_catalog_context_examples_and_lessons(module_name, node_name, reply,
                                                            has_examples, monkeypatch):
    import importlib

    module = importlib.import_module(module_name)
    state = _state(schema=LEGACY, generated_sql="SELECT 1", correction_plan={"x": 1}, **RICH)
    prompt, _ = _captured_system_prompt(module, node_name, state, reply)
    assert "CATALOG TABLE items (id INTEGER, owner TEXT)" in prompt
    assert "Table Invoice" not in prompt, "the catalog context replaces the legacy rendering"
    assert "items.owner = owners.name" in prompt
    assert "owner holds the login name." in prompt
    assert ("SELECT COUNT(*) FROM items" in prompt) is has_examples
    assert "only the tables and columns listed" in prompt
    assert "verbatim" in prompt

    plain, _ = _captured_system_prompt(module, node_name,
                                       _state(schema=LEGACY, generated_sql="SELECT 1",
                                              correction_plan={"x": 1}), reply)
    assert "Table Invoice: Total NUMERIC" in plain
    for absent in ("Known joins", "Earlier questions", "Lessons from earlier",
                   "A previous attempt failed"):
        assert absent not in plain


def test_correction_plan_prompt_uses_the_catalog_context():
    import agents.nodes.correction_plan as module

    state = _state(schema=LEGACY, generated_sql="SELEC 1", **RICH,
                   error_log={"message": 'near "SELEC": syntax error', "dialect": "sqlite"})
    prompt, result = _captured_system_prompt(module, "correction_plan_node", state, "{}")
    assert "CATALOG TABLE items" in prompt and "Table Invoice" not in prompt
    assert "action" not in result["correction_plan"]


# -- planner clarification -------------------------------------------------------

CLARIFY = ('{"select": [], "from": "items", "needs_clarification": true, '
           '"clarification_question": "Do you mean gross or net revenue?"}')


def test_planner_asks_for_clarification_when_enabled(monkeypatch):
    import agents.nodes.query_planner as module

    monkeypatch.setattr(module, "_clarification_enabled", lambda: True)
    prompt, result = _captured_system_prompt(module, "query_planner_node",
                                             _state(schema=LEGACY), CLARIFY)
    assert '"needs_clarification"' in prompt
    assert result["clarification_question"] == "Do you mean gross or net revenue?"
    assert result["final_answer"] == result["clarification_question"]
    assert result["query_type"] == "clarification"


def test_planner_ignores_clarification_when_disabled_or_on_reselection(monkeypatch):
    import agents.nodes.query_planner as module

    monkeypatch.setattr(module, "_clarification_enabled", lambda: False)
    prompt, result = _captured_system_prompt(module, "query_planner_node",
                                             _state(schema=LEGACY), CLARIFY)
    assert "needs_clarification" not in prompt and set(result) == {"query_plan"}

    monkeypatch.setattr(module, "_clarification_enabled", lambda: True)
    prompt, result = _captured_system_prompt(
        module, "query_planner_node", _state(schema=LEGACY, reselect_count=1), CLARIFY)
    assert "needs_clarification" not in prompt and set(result) == {"query_plan"}


def test_planner_needs_a_real_question_to_clarify(monkeypatch):
    import agents.nodes.query_planner as module

    monkeypatch.setattr(module, "_clarification_enabled", lambda: True)
    for reply in ('{"needs_clarification": true, "clarification_question": "  "}',
                  '{"needs_clarification": "yes", "clarification_question": "Which?"}',
                  '{"needs_clarification": false, "clarification_question": "Which?"}'):
        _, result = _captured_system_prompt(module, "query_planner_node",
                                            _state(schema=LEGACY), reply)
        assert set(result) == {"query_plan"}


# -- gatekeeper -------------------------------------------------------------------

def _gatekeeper(state, replies):
    from agents.nodes.gatekeeper import gatekeeper_node

    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=[MagicMock(content=r) for r in replies])
    with patch("llm.fallback.get_llm", return_value=llm):
        return asyncio.run(gatekeeper_node(state)), llm


def test_gatekeeper_accepts_a_connection_id_without_schema():
    result, llm = _gatekeeper(_state(), ['{"category": "sql", "intent": "x", "response": ""}'])
    assert result["query_type"] == "sql" and llm.ainvoke.await_count == 1
    assert result["reselect_count"] == 0 and result["schema_context"] is None

    result, llm = _gatekeeper(_state(connection_id=None),
                              ['{"category": "sql", "intent": "x", "response": ""}'])
    assert result["query_type"] == "conversational" and llm.ainvoke.await_count == 0


def test_gatekeeper_folds_a_clarification_answer_into_the_question():
    from langchain_core.messages import AIMessage, HumanMessage

    history = [HumanMessage(content="revenue by region"),
               AIMessage(content="Do you mean gross or net revenue?",
                         additional_kwargs={"clarification": True})]
    state = _state(user_query="net", messages=history)
    result, llm = _gatekeeper(state, ['{"category": "sql", "intent": "x", "response": ""}',
                                      "Net revenue by region"])
    assert result["query_type"] == "follow_up"
    assert result["resolved_query"] == "Net revenue by region"
    rewrite_messages = llm.ainvoke.await_args_list[1].args[0]
    assert "clarifying question" in rewrite_messages[0].content
    assert "assistant: Do you mean gross or net revenue?" in rewrite_messages[1].content
    assert "user: revenue by region" in rewrite_messages[1].content

    # Without the marker a plain sql question is not rewritten.
    plain = [HumanMessage(content="a"), AIMessage(content="b")]
    result, llm = _gatekeeper(_state(user_query="list items", messages=plain),
                              ['{"category": "sql", "intent": "x", "response": ""}'])
    assert result["query_type"] == "sql" and "resolved_query" not in result


# -- evaluator ----------------------------------------------------------------------

def _evaluate(state):
    from agents.nodes.evaluator import evaluator_node

    return asyncio.run(evaluator_node(state))


DONE = dict(generated_sql="SELECT id FROM items", db_results=[{"id": 1}], error_log=None,
            relevant_tables=["items"], final_answer="One item.")
CORRECTION = {"error_category": "missing_column", "severity": "recoverable",
              "root_cause": "Column owner_name does not exist; the column is owner.",
              "correction_steps": ["Replace owner_name with owner", "x", "y"]}


def test_evaluator_logs_a_clean_run_without_a_lesson():
    memory = FakeMemory()
    services.set_memory_service_for_tests(memory)
    result = _evaluate(_state(**DONE, sql_history=[]))
    assert result["query_id"] == "q1" and memory.added_lessons == []
    assert memory.logged[0] == {
        "tenant_id": "t1", "connection_id": "c1", "question": "how many items per owner",
        "sql": "SELECT id FROM items", "status": "completed", "error_type": None,
        "tables": ("items",), "thread_id": None, "query_id": "q1"}
    assert [m.content for m in result["messages"]] == ["how many items per owner", "One item."]


def test_evaluator_adds_exactly_one_lesson_after_a_corrected_run():
    memory = FakeMemory()
    services.set_memory_service_for_tests(memory)
    history = [{"sql": "SELECT owner_name FROM items", "error": {"message": "x"},
                "attempt_num": 0}]
    result = _evaluate(_state(**DONE, sql_history=history, correction_plan=CORRECTION))
    assert result["query_id"] == "q1" and len(memory.added_lessons) == 1
    lesson = memory.added_lessons[0]
    assert lesson["source"] == "correction" and len(lesson["text"]) <= 300
    assert lesson["text"] == ("Column owner_name does not exist; the column is owner. "
                              "Fix: Replace owner_name with owner; x.")


def test_evaluator_adds_no_lesson_for_failed_or_transient_runs():
    memory = FakeMemory()
    services.set_memory_service_for_tests(memory)
    history = [{"sql": "x", "error": {}, "attempt_num": 0}]
    failed = dict(DONE, db_results=None,
                  error_log={"error_type": "sql_error", "message": "no such column"})
    _evaluate(_state(**failed, sql_history=history, correction_plan=CORRECTION))
    transient = {"severity": "transient", "root_cause": "Transient error",
                 "correction_steps": ["retry_unchanged"]}
    _evaluate(_state(**DONE, sql_history=history, correction_plan=transient))
    assert memory.added_lessons == []
    assert [q["status"] for q in memory.logged] == ["failed", "completed"]
    assert memory.logged[0]["error_type"] == "sql_error"


def test_evaluator_survives_a_raising_memory_service():
    services.set_memory_service_for_tests(FakeMemory(error=RuntimeError("down")))
    result = _evaluate(_state(**DONE, sql_history=[{"sql": "x"}], correction_plan=CORRECTION))
    assert "query_id" not in result and len(result["messages"]) == 2


def test_evaluator_skips_memory_without_connection_or_sql():
    memory = FakeMemory()
    services.set_memory_service_for_tests(memory)
    _evaluate(_state(**dict(DONE, generated_sql=None)))
    _evaluate(_state(**DONE, connection_id=None))
    assert memory.logged == []


def test_evaluator_marks_a_clarification_in_the_history():
    result = _evaluate(_state(final_answer="Gross or net?", query_type="clarification",
                              clarification_question="Gross or net?"))
    assert result["messages"][1].additional_kwargs == {"clarification": True}
    assert "query_id" not in result
