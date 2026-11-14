"""Shared fixtures for UI tests."""
import os
import sys
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock

# `streamlit run streamlit_app/app.py` puts the script's directory on sys.path, and app.py
# relies on that (`from components.sidebar import ...`). Mirror it for the tests.
_STREAMLIT_APP_DIR = str(Path(__file__).resolve().parents[2] / "streamlit_app")
if _STREAMLIT_APP_DIR not in sys.path:
    sys.path.insert(0, _STREAMLIT_APP_DIR)


@pytest.fixture
def sample_agent_state():
    """Complete AgentState dict. Used as test input for UI rendering."""
    return {
        "messages": [],
        "user_query": "Show total sales by artist",
        "resolved_query": None,
        "db_type": "sqlite",
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


_ENV_VARS = (
    "T2S_ALLOW_FILE_DATABASES", "T2S_ALLOW_PRIVATE_HOSTS", "T2S_METADATA_URL",
    "T2S_SECRET_KEY", "T2S_FILE_DATABASE_ROOT",
    "GROQ_API_KEY", "OPENAI_API_KEY", "PINECONE_API_KEY",
)


@pytest.fixture(autouse=True)
def _isolated_service_env():
    """Keep UI tests from leaking process state into the rest of the suite.

    Importing streamlit_app.app sets T2S_* defaults in os.environ, and connecting builds
    the process-wide registry. The metadata store is forced in-memory so no test can
    create the default .t2s/metadata.db inside the repository.
    """
    from database import metadata, registry

    saved = {name: os.environ.get(name) for name in _ENV_VARS}
    os.environ["T2S_METADATA_URL"] = "sqlite://"
    os.environ.pop("T2S_SECRET_KEY", None)
    registry.set_registry(None)
    metadata.reset_engine()
    yield
    registry.set_registry(None)
    metadata.reset_engine()
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture
def demo_db(tmp_path):
    """Factory for small SQLite files: demo_db("a", ["Artist", "Album"]) -> path."""
    import sqlite3

    def make(name: str, tables: list[str]) -> str:
        path = tmp_path / f"{name}.db"
        conn = sqlite3.connect(path)
        for table in tables:
            conn.execute(f"CREATE TABLE {table} (Id INTEGER PRIMARY KEY, Name TEXT)")
            conn.execute(f"INSERT INTO {table} (Name) VALUES ('{name}-{table}')")
        conn.commit()
        conn.close()
        return str(path)

    return make


@pytest.fixture
def file_registry(monkeypatch):
    """A real ConnectionRegistry on an in-memory metadata store, file databases allowed."""
    from database import registry

    monkeypatch.setenv("T2S_ALLOW_FILE_DATABASES", "true")
    reg = registry.get_registry()
    yield reg
    reg.close_all()
