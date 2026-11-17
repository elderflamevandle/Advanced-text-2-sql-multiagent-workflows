"""GRAPH-001 unit tests: AgentState schema and routing function verification."""
import json
from typing import Annotated, get_args, get_origin, get_type_hints

import pytest

from tests.graph.conftest import make_initial_state


EXPECTED_FIELDS = {
    "messages",
    "user_query",
    "resolved_query",
    "db_type",
    "tenant_id",
    "connection_id",
    "query_type",
    "schema",
    "relevant_tables",
    "schema_context",
    "join_hints",
    "examples",
    "lessons",
    "query_id",
    "clarification_question",
    "reselect_count",
    "required_tables",
    "query_plan",
    "generated_sql",
    "sql_explanation",
    "db_results",
    "execution_metadata",
    "approval_status",
    "error_log",
    "correction_plan",
    "sql_history",
    "usage_metadata",
    "retry_count",
    "final_answer",
}


def test_agentstate_has_required_fields():
    """GRAPH-001: AgentState fields. db_manager is intentionally absent: it is not
    serializable by the checkpointer; nodes use database.manager.get_active_manager()."""
    from graph.state import AgentState

    hints = get_type_hints(AgentState, include_extras=True)
    assert set(hints.keys()) == EXPECTED_FIELDS, (
        f"Field mismatch. Missing: {EXPECTED_FIELDS - set(hints.keys())}, "
        f"Extra: {set(hints.keys()) - EXPECTED_FIELDS}"
    )
    assert len(hints) == 29
    assert "db_manager" not in hints


def test_messages_uses_add_messages_reducer():
    """GRAPH-001: messages field must be Annotated[list, add_messages] for LangGraph reduction."""
    from langgraph.graph.message import add_messages
    from graph.state import AgentState

    hints = get_type_hints(AgentState, include_extras=True)
    messages_type = hints["messages"]

    # Must be Annotated
    assert get_origin(messages_type) is Annotated, (
        "messages field must be Annotated[list, add_messages], not a plain list"
    )

    args = get_args(messages_type)
    assert len(args) >= 2, "Annotated must have at least two arguments"
    # Second arg must be the add_messages function
    assert args[1] is add_messages, (
        f"messages reducer must be add_messages, got {args[1]}"
    )


def test_agentstate_is_json_serializable():
    """GRAPH-001: AgentState dict with all None optional values must be JSON-serializable."""
    state = make_initial_state()
    try:
        serialized = json.dumps(state)
    except TypeError as exc:
        pytest.fail(f"AgentState initial state is not JSON-serializable: {exc}")

    # Round-trip check
    deserialized = json.loads(serialized)
    assert deserialized["user_query"] == "test query"
    assert deserialized["retry_count"] == 0
    assert "db_manager" not in deserialized


def test_routing_gatekeeper():
    """GRAPH-001: route_after_gatekeeper routing logic for all query_type values."""
    from graph.conditions import route_after_gatekeeper

    # None query_type → schema_linker (SQL path)
    assert route_after_gatekeeper({"query_type": None}) == "schema_linker"
    # Explicit sql query_type → schema_linker
    assert route_after_gatekeeper({"query_type": "sql"}) == "schema_linker"
    # Conversational → formatter (bypass SQL pipeline)
    assert route_after_gatekeeper({"query_type": "conversational"}) == "formatter"
    # follow_up → schema_linker (needs schema lookup after rewrite)
    assert route_after_gatekeeper({"query_type": "follow_up"}) == "schema_linker"
    # ambiguous → formatter (needs clarification response)
    assert route_after_gatekeeper({"query_type": "ambiguous"}) == "formatter"


def test_routing_executor():
    """GRAPH-001: route_after_executor routing logic for success, retry, and exhausted paths."""
    from graph.conditions import route_after_executor

    # Success path: no error → formatter
    assert route_after_executor({"error_log": None, "retry_count": 0}) == "formatter"
    # Error + retry available (count=0) → correction_plan
    assert route_after_executor({"error_log": "syntax error", "retry_count": 0}) == "correction_plan"
    # Error + retry available (count=1) → correction_plan
    assert route_after_executor({"error_log": "column not found", "retry_count": 1}) == "correction_plan"
    # Error + retries exhausted (count=2) → formatter
    assert route_after_executor({"error_log": "persistent error", "retry_count": 2}) == "formatter"


def test_usage_metadata_is_reducer_field():
    """usage_metadata accumulates across nodes; None resets it for a new query."""
    from graph.state import AgentState, merge_usage_metadata

    hints = get_type_hints(AgentState, include_extras=True)
    assert get_origin(hints["usage_metadata"]) is Annotated
    assert get_args(hints["usage_metadata"])[1] is merge_usage_metadata

    assert merge_usage_metadata(None, [{"a": 1}]) == [{"a": 1}]
    assert merge_usage_metadata([{"a": 1}], [{"b": 2}]) == [{"a": 1}, {"b": 2}]
    assert merge_usage_metadata([{"a": 1}], []) == [{"a": 1}]
    assert merge_usage_metadata([{"a": 1}], None) == []
    current = [{"a": 1}]
    merge_usage_metadata(current, [{"b": 2}])
    assert current == [{"a": 1}], "reducer must not mutate its input"


@pytest.mark.parametrize(
    "error_type", ["llm_all_providers_failed", "blocked_query", "no_connection", "user_rejected"]
)
def test_routing_executor_non_correctable_errors_skip_correction(error_type):
    from graph.conditions import route_after_executor

    state = {"error_log": {"error_type": error_type, "message": "x"}, "retry_count": 0}
    assert route_after_executor(state) == "formatter"


def test_routing_executor_correctable_dict_error_enters_correction():
    from graph.conditions import route_after_executor

    state = {"error_log": {"error_type": "syntax_error", "message": "x"}, "retry_count": 0}
    assert route_after_executor(state) == "correction_plan"


def test_routing_executor_retry_limit_comes_from_config(monkeypatch):
    import graph.conditions as conditions

    state = {"error_log": {"error_type": "syntax_error"}, "retry_count": 2}
    assert conditions.get_max_correction_retries() == 2, "config/config.yaml should say 2"
    assert conditions.route_after_executor(state) == "formatter"

    monkeypatch.setattr(conditions, "get_max_correction_retries", lambda: 4)
    assert conditions.route_after_executor(state) == "correction_plan"
    assert conditions.route_after_executor(dict(state, retry_count=4)) == "formatter"

    monkeypatch.setattr(conditions, "get_max_correction_retries", lambda: 0)
    assert conditions.route_after_executor(dict(state, retry_count=0)) == "formatter"


def test_max_correction_retries_reads_app_section(monkeypatch):
    import graph.conditions as conditions
    import llm.config as llm_config

    monkeypatch.setattr(llm_config, "load_config",
                        lambda: {"app": {"max_correction_retries": 5}})
    assert conditions.get_max_correction_retries() == 5
    monkeypatch.setattr(llm_config, "load_config",
                        lambda: {"app": {"max_correction_retries": "oops"}})
    assert conditions.get_max_correction_retries() == conditions.DEFAULT_MAX_CORRECTION_RETRIES
    monkeypatch.setattr(llm_config, "load_config", lambda: {})
    assert conditions.get_max_correction_retries() == conditions.DEFAULT_MAX_CORRECTION_RETRIES


def test_routing_llm_failure_goes_to_formatter():
    from graph.conditions import (
        route_after_gatekeeper,
        route_after_hitl,
        route_unless_llm_failed,
    )

    failed = {"error_log": {"error_type": "llm_all_providers_failed"}, "query_type": "sql"}
    assert route_after_gatekeeper(failed) == "formatter"
    assert route_unless_llm_failed(failed) == "formatter"
    assert route_unless_llm_failed({"error_log": None}) == "continue"
    assert route_unless_llm_failed({"error_log": "plain string error"}) == "continue"
    assert route_unless_llm_failed({"error_log": {"error_type": "syntax_error"}}) == "continue"
    assert route_after_hitl({"error_log": {"error_type": "user_rejected"}}) == "formatter"
