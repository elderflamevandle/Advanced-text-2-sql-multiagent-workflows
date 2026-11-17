"""AGENT-002 unit tests: schema_linker_node retrieval and fallback behavior."""
import asyncio
import sys
from unittest.mock import MagicMock, patch

# Imported here, before any test swaps sys.modules entries with patch.dict: a module first
# imported inside patch.dict is dropped from sys.modules again when the patch ends.
import database.registry
import pytest
from tests.agents.conftest import make_agent_state


@pytest.fixture(autouse=True)
def _no_real_registry(monkeypatch):
    """States with a connection_id make the node ask the registry. The real one would build
    its metadata store (by default a file under .t2s/ in the working directory), so these
    tests get an unavailable registry unless they install their own."""
    def unavailable():
        raise NotImplementedError("no registry in schema linker tests")

    monkeypatch.setattr(database.registry, "get_registry", unavailable)


def test_retriever_populates_tables():
    """AGENT-002: Schema linker calls retriever and narrows schema to relevant tables."""
    from agents.nodes.schema_linker import schema_linker_node

    full_schema = {
        "Invoice": {"columns": [{"name": "Total", "type": "NUMERIC"}]},
        "Customer": {"columns": [{"name": "CustomerId", "type": "INTEGER"}]},
        "Track": {"columns": [{"name": "TrackId", "type": "INTEGER"}]},
    }
    state = make_agent_state("total sales by customer")
    state["schema"] = full_schema

    mock_retriever = MagicMock()
    mock_retriever.retrieve_tables.return_value = {
        "tables": ["Invoice", "Customer"],
        "table_metadata": {},
        "join_hints": [],
        "scores": {},
    }

    mock_vector_module = MagicMock()
    mock_vector_module.get_retriever = MagicMock(return_value=mock_retriever)

    with patch.dict(sys.modules, {"vector.retriever": mock_vector_module}):
        result = asyncio.run(schema_linker_node(state))

    assert result["relevant_tables"] == ["Invoice", "Customer"]
    assert set(result["schema"].keys()) == {"Invoice", "Customer"}
    assert "Track" not in result["schema"]


def test_fallback_full_schema():
    """AGENT-002: Schema linker falls back to full schema when retriever raises."""
    from agents.nodes.schema_linker import schema_linker_node

    full_schema = {
        "Invoice": {"columns": []},
        "Customer": {"columns": []},
    }
    state = make_agent_state("show me all data")
    state["schema"] = full_schema

    mock_vector_module = MagicMock()
    mock_vector_module.get_retriever.side_effect = Exception("Pinecone unavailable")

    with patch.dict(sys.modules, {"vector.retriever": mock_vector_module}):
        result = asyncio.run(schema_linker_node(state))

    assert set(result["relevant_tables"]) == {"Invoice", "Customer"}
    assert result["schema"] == full_schema


def test_uses_resolved_query():
    """AGENT-002: Schema linker uses resolved_query when present, not user_query."""
    from agents.nodes.schema_linker import schema_linker_node

    state = make_agent_state("what about last year?")
    state["resolved_query"] = "total sales last year"
    state["schema"] = {"Invoice": {"columns": []}}

    mock_retriever = MagicMock()
    mock_retriever.retrieve_tables.return_value = {
        "tables": ["Invoice"],
        "table_metadata": {},
        "join_hints": [],
        "scores": {},
    }

    mock_vector_module = MagicMock()
    mock_vector_module.get_retriever = MagicMock(return_value=mock_retriever)

    with patch.dict(sys.modules, {"vector.retriever": mock_vector_module}):
        result = asyncio.run(schema_linker_node(state))

    # retrieve_tables must have been called with resolved_query, not user_query
    call_args = mock_retriever.retrieve_tables.call_args
    assert call_args[0][0] == "total sales last year"


# ---------------------------------------------------------------------------
# Namespace per connection + schema hash, registry schema, unknown tables
# ---------------------------------------------------------------------------

SCHEMA_A = {
    "Invoice": {"columns": [{"name": "Total", "type": "NUMERIC"}], "foreign_keys": []},
    "Customer": {"columns": [{"name": "CustomerId", "type": "INTEGER"}], "foreign_keys": []},
}
SCHEMA_B = {
    "Patient": {"columns": [{"name": "PatientId", "type": "INTEGER"}], "foreign_keys": []},
    "Visit": {"columns": [{"name": "VisitId", "type": "INTEGER"}], "foreign_keys": []},
}


def _run_linker(state, tables):
    """Run the node against a mocked retriever; return (result, retriever mock)."""
    from agents.nodes.schema_linker import schema_linker_node

    mock_retriever = MagicMock()
    mock_retriever.retrieve_tables.return_value = {
        "tables": tables, "table_metadata": [], "join_hints": [], "scores": {},
    }
    mock_vector_module = MagicMock()
    mock_vector_module.get_retriever = MagicMock(return_value=mock_retriever)
    with patch.dict(sys.modules, {"vector.retriever": mock_vector_module}):
        result = asyncio.run(schema_linker_node(state))
    return result, mock_retriever


def _namespaces(retriever):
    embed_ns = retriever.embed_schema.call_args[0][1]
    retrieve_ns = retriever.retrieve_tables.call_args[0][1]
    assert embed_ns == retrieve_ns, "embedding and retrieval must use the same namespace"
    return embed_ns


def test_namespace_is_not_the_db_type_and_differs_per_connection():
    """Two SQLite databases must never share a namespace (the old namespace was 'sqlite')."""
    state_a = make_agent_state("total sales")
    state_a.update(schema=SCHEMA_A, connection_id="conn-a", tenant_id="t1")
    state_b = make_agent_state("patient visits")
    state_b.update(schema=SCHEMA_B, connection_id="conn-b", tenant_id="t1")

    _, retriever_a = _run_linker(state_a, ["Invoice"])
    _, retriever_b = _run_linker(state_b, ["Visit"])

    ns_a, ns_b = _namespaces(retriever_a), _namespaces(retriever_b)
    assert ns_a != ns_b
    assert ns_a.startswith("conn-a-") and ns_b.startswith("conn-b-")
    assert "sqlite" not in (ns_a, ns_b)
    # The full schema of the right database is what gets embedded.
    assert retriever_b.embed_schema.call_args[0][0] == SCHEMA_B


def test_local_databases_with_different_schemas_get_different_namespaces():
    """Legacy mode (no connection id): the schema hash alone keeps databases apart."""
    state_a = make_agent_state("q")
    state_a["schema"] = SCHEMA_A
    state_b = make_agent_state("q")
    state_b["schema"] = SCHEMA_B

    ns_a = _namespaces(_run_linker(state_a, ["Invoice"])[1])
    ns_b = _namespaces(_run_linker(state_b, ["Visit"])[1])
    assert ns_a.startswith("local-") and ns_b.startswith("local-")
    assert ns_a != ns_b


def test_changed_schema_changes_the_namespace():
    state = make_agent_state("q")
    state.update(schema=SCHEMA_A, connection_id="conn-a")
    before = _namespaces(_run_linker(state, ["Invoice"])[1])
    again = _namespaces(_run_linker(state, ["Invoice"])[1])

    changed = {**SCHEMA_A, "Refund": {"columns": [{"name": "Id", "type": "INTEGER"}]}}
    state2 = make_agent_state("q")
    state2.update(schema=changed, connection_id="conn-a")
    after = _namespaces(_run_linker(state2, ["Invoice"])[1])

    assert before == again, "same schema, same namespace: no re-embedding"
    assert after != before, "a new table must trigger a new embedding"


def test_tables_not_in_schema_are_dropped():
    state = make_agent_state("q")
    state["schema"] = SCHEMA_A
    result, _ = _run_linker(state, ["Invoice", "DroppedTable", "Invoice"])
    assert result["relevant_tables"] == ["Invoice"]
    assert set(result["schema"]) == {"Invoice"}


def test_only_unknown_tables_falls_back_to_full_schema():
    state = make_agent_state("q")
    state["schema"] = SCHEMA_A
    result, _ = _run_linker(state, ["Artist", "Album"])   # another database's tables
    assert result["schema"] == SCHEMA_A
    assert result["relevant_tables"] == ["Invoice", "Customer"]


def test_missing_schema_skips_retrieval():
    state = make_agent_state("q")
    state["schema"] = None
    result, retriever = _run_linker(state, ["Invoice"])
    assert result == {"relevant_tables": [], "schema": None}
    retriever.embed_schema.assert_not_called()
    retriever.retrieve_tables.assert_not_called()


def test_registry_schema_is_used_when_state_holds_a_narrowed_one():
    """Second turn on a thread: state.schema is the narrowed schema of the first turn."""
    from database import registry as registry_module

    fake_registry = MagicMock()
    fake_registry.get_schema.return_value = SCHEMA_A
    state = make_agent_state("customers")
    state.update(schema={"Invoice": SCHEMA_A["Invoice"]}, connection_id="conn-a", tenant_id="t1")

    with patch.object(registry_module, "get_registry", return_value=fake_registry):
        result, retriever = _run_linker(state, ["Customer"])

    fake_registry.get_schema.assert_called_once_with("t1", "conn-a")
    assert retriever.embed_schema.call_args[0][0] == SCHEMA_A
    assert result["relevant_tables"] == ["Customer"]
    assert set(result["schema"]) == {"Customer"}


def test_registry_failure_falls_back_to_state_schema():
    from database import registry as registry_module

    state = make_agent_state("q")
    state.update(schema=SCHEMA_A, connection_id="conn-a", tenant_id="t1")
    with patch.object(registry_module, "get_registry", side_effect=NotImplementedError):
        result, retriever = _run_linker(state, ["Invoice"])
    assert retriever.embed_schema.call_args[0][0] == SCHEMA_A
    assert result["relevant_tables"] == ["Invoice"]
