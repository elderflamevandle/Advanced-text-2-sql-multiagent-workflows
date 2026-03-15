"""AGENT-002 unit tests: schema_linker_node retrieval and fallback behavior."""
import asyncio
import sys
from unittest.mock import MagicMock, patch

from tests.agents.conftest import make_agent_state


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
