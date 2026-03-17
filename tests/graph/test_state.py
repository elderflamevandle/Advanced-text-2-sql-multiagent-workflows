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
    "db_manager",
    "query_type",
    "schema",
    "relevant_tables",
    "query_plan",
    "generated_sql",
    "sql_explanation",
    "db_results",
    "execution_metadata",
    "approval_status",
    "error_log",
    "correction_plan",
    "sql_history",
    "retry_count",
    "final_answer",
}


def test_agentstate_has_required_fields():
    """GRAPH-001: AgentState must have exactly 19 required fields."""
    from graph.state import AgentState

    hints = get_type_hints(AgentState, include_extras=True)
    assert set(hints.keys()) == EXPECTED_FIELDS, (
        f"Field mismatch. Missing: {EXPECTED_FIELDS - set(hints.keys())}, "
        f"Extra: {set(hints.keys()) - EXPECTED_FIELDS}"
    )
    assert len(hints) == 19


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
    # db_manager=None must be JSON-serializable (None → null in JSON)
    try:
        serialized = json.dumps(state)
    except TypeError as exc:
        pytest.fail(f"AgentState initial state is not JSON-serializable: {exc}")

    # Round-trip check
    deserialized = json.loads(serialized)
    assert deserialized["user_query"] == "test query"
    assert deserialized["retry_count"] == 0
    assert deserialized["db_manager"] is None


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
