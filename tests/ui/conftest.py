"""Shared fixtures for UI tests."""
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def sample_agent_state():
    """Complete AgentState dict with all 20 fields. Used as test input for UI rendering."""
    return {
        "messages": [],
        "user_query": "Show total sales by artist",
        "resolved_query": None,
        "db_type": "sqlite",
        "db_manager": None,
        "query_type": "sql",
        "schema": {"Artist": {"columns": ["ArtistId", "Name"]}},
        "relevant_tables": ["Artist", "InvoiceLine"],
        "query_plan": "SELECT Artist.Name, SUM(InvoiceLine.UnitPrice) FROM Artist JOIN...",
        "generated_sql": "SELECT a.Name, SUM(il.UnitPrice) AS total FROM Artist a JOIN InvoiceLine il ON a.ArtistId = il.TrackId GROUP BY a.Name",
        "sql_explanation": "Joins Artist to InvoiceLine via TrackId and sums UnitPrice per artist.",
        "db_results": [{"Name": "AC/DC", "total": 4.95}, {"Name": "Accept", "total": 3.96}],
        "execution_metadata": {"rows": 2, "execution_time_ms": 12},
        "approval_status": None,
        "error_log": None,
        "correction_plan": None,
        "sql_history": [],
        "usage_metadata": [
            {"provider": "groq", "model": "llama-3.3-70b-versatile", "node_name": "sql_generator",
             "input_tokens": 200, "output_tokens": 80, "total_tokens": 280, "estimated_cost_usd": 0.0002}
        ],
        "retry_count": 0,
        "final_answer": "Found 2 results. First row: {'Name': 'AC/DC', 'total': 4.95}",
    }


@pytest.fixture
def sample_agent_state_with_error(sample_agent_state):
    """AgentState with error and retry, used for debug panel auto-expand tests."""
    state = dict(sample_agent_state)
    state.update({
        "db_results": None,
        "final_answer": "I couldn't generate a working query after 2 attempt(s).",
        "error_log": {"error_type": "syntax_error", "message": "column 'TrackId' does not exist"},
        "retry_count": 2,
        "sql_history": [
            {"sql": "SELECT * FROM Artist", "error": {"message": "no results"}, "attempt_num": 0},
        ],
        "correction_plan": {"error_category": "missing_column", "severity": "high"},
    })
    return state


@pytest.fixture
def mock_graph():
    """Mock LangGraph compiled graph for UI tests. Returns sample_agent_state on ainvoke."""
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={
        "final_answer": "Found 2 results. First row: {'Name': 'AC/DC', 'total': 4.95}",
        "generated_sql": "SELECT a.Name FROM Artist a",
        "retry_count": 0,
        "usage_metadata": [],
        "db_results": [{"Name": "AC/DC", "total": 4.95}],
    })
    return graph


@pytest.fixture
def mock_db_manager():
    """Mock DatabaseManager. get_schema() returns 3-table dict."""
    mgr = MagicMock()
    mgr.get_schema.return_value = {
        "Artist": {"columns": ["ArtistId", "Name"]},
        "Album": {"columns": ["AlbumId", "Title", "ArtistId"]},
        "Track": {"columns": ["TrackId", "Name", "AlbumId"]},
    }
    return mgr
