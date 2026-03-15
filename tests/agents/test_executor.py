"""Tests for agents.nodes.executor_node.

Requirements covered:
- AGENT-005: SQL execution returns results or structured error
- DB-002: Missing SQL / missing db_manager / LIMIT injection / error logging
- DB-003: Error messages include dialect prefix
"""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from tests.agents.conftest import make_agent_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _make_mock_db_manager(return_value=None, side_effect=None):
    """Create a mock DatabaseManager with a controllable execute_query method."""
    mgr = MagicMock()
    if side_effect is not None:
        mgr.execute_query.side_effect = side_effect
    else:
        mgr.execute_query.return_value = return_value if return_value is not None else []
    return mgr


# ---------------------------------------------------------------------------
# AGENT-005: Successful SELECT execution
# ---------------------------------------------------------------------------

def test_executor_select_returns_db_results():
    """AGENT-005: Successful SELECT returns db_results and execution_metadata."""
    from agents.nodes.executor import executor_node

    rows = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]
    db_mgr = _make_mock_db_manager(return_value=rows)

    state = make_agent_state()
    state["generated_sql"] = "SELECT id, name FROM users LIMIT 10"
    state["db_manager"] = db_mgr

    result = _run(executor_node(state))

    assert result["db_results"] == rows
    assert result["error_log"] is None
    assert "execution_metadata" in result
    assert result["execution_metadata"]["row_count"] == 2
    assert result["execution_metadata"]["execution_time_ms"] >= 0


def test_executor_with_empty_result_set():
    """AGENT-005: SELECT returning no rows still returns db_results=[] with metadata."""
    from agents.nodes.executor import executor_node

    db_mgr = _make_mock_db_manager(return_value=[])
    state = make_agent_state()
    state["generated_sql"] = "SELECT * FROM empty_table LIMIT 10"
    state["db_manager"] = db_mgr

    result = _run(executor_node(state))

    assert result["db_results"] == []
    assert result["error_log"] is None
    assert result["execution_metadata"]["row_count"] == 0


# ---------------------------------------------------------------------------
# AGENT-005: Blocked queries produce structured error_log
# ---------------------------------------------------------------------------

def test_executor_drop_returns_blocked_error():
    """AGENT-005: DROP SQL is blocked and returns structured error_log dict."""
    from agents.nodes.executor import executor_node

    db_mgr = _make_mock_db_manager()
    state = make_agent_state()
    state["generated_sql"] = "DROP TABLE users"
    state["db_manager"] = db_mgr
    state["db_type"] = "sqlite"

    result = _run(executor_node(state))

    assert result["db_results"] is None
    assert result["execution_metadata"] is None
    err = result["error_log"]
    assert isinstance(err, dict), f"error_log should be dict, got {type(err)}"
    assert err["error_type"] == "blocked_query"
    assert "failed_sql" in err
    assert err["failed_sql"] == "DROP TABLE users"
    assert "hint" in err
    assert "dialect" in err
    # db_manager.execute_query should NOT have been called for a blocked query
    db_mgr.execute_query.assert_not_called()


def test_executor_delete_returns_blocked_error():
    """AGENT-005: DELETE SQL is blocked and returns structured error_log dict."""
    from agents.nodes.executor import executor_node

    db_mgr = _make_mock_db_manager()
    state = make_agent_state()
    state["generated_sql"] = "DELETE FROM orders WHERE id = 1"
    state["db_manager"] = db_mgr

    result = _run(executor_node(state))

    err = result["error_log"]
    assert isinstance(err, dict)
    assert err["error_type"] == "blocked_query"


# ---------------------------------------------------------------------------
# DB-002: Missing generated_sql
# ---------------------------------------------------------------------------

def test_executor_missing_sql_returns_error():
    """DB-002: Missing generated_sql returns error_log with error_type=missing_sql."""
    from agents.nodes.executor import executor_node

    db_mgr = _make_mock_db_manager()
    state = make_agent_state()
    state["generated_sql"] = None
    state["db_manager"] = db_mgr

    result = _run(executor_node(state))

    err = result["error_log"]
    assert isinstance(err, dict)
    assert err["error_type"] == "missing_sql"


def test_executor_empty_sql_returns_error():
    """DB-002: Empty string generated_sql returns error_log."""
    from agents.nodes.executor import executor_node

    db_mgr = _make_mock_db_manager()
    state = make_agent_state()
    state["generated_sql"] = ""
    state["db_manager"] = db_mgr

    result = _run(executor_node(state))

    err = result["error_log"]
    assert isinstance(err, dict)
    assert err["error_type"] == "missing_sql"


# ---------------------------------------------------------------------------
# DB-002: Missing db_manager
# ---------------------------------------------------------------------------

def test_executor_no_db_manager_returns_error():
    """DB-002: Missing db_manager returns error_log with error_type=no_connection."""
    from agents.nodes.executor import executor_node

    state = make_agent_state(db_manager=None)
    # Override the object() default from conftest
    state["db_manager"] = None
    state["generated_sql"] = "SELECT * FROM users"

    result = _run(executor_node(state))

    err = result["error_log"]
    assert isinstance(err, dict)
    assert err["error_type"] == "no_connection"


# ---------------------------------------------------------------------------
# DB-003: Error messages include dialect prefix
# ---------------------------------------------------------------------------

def test_executor_error_message_includes_dialect():
    """DB-003: Error messages are prefixed with the dialect name."""
    from agents.nodes.executor import executor_node

    db_mgr = _make_mock_db_manager(side_effect=Exception("no such table: foo"))
    state = make_agent_state()
    state["generated_sql"] = "SELECT * FROM foo LIMIT 10"
    state["db_type"] = "sqlite"
    state["db_manager"] = db_mgr

    result = _run(executor_node(state))

    err = result["error_log"]
    assert isinstance(err, dict)
    # Message should contain dialect prefix
    assert "sqlite" in err["message"].lower() or "SQLite" in err["message"]


def test_executor_dialect_in_blocked_error():
    """DB-003: Blocked query error includes dialect field."""
    from agents.nodes.executor import executor_node

    db_mgr = _make_mock_db_manager()
    state = make_agent_state()
    state["generated_sql"] = "INSERT INTO users VALUES (1, 'test')"
    state["db_type"] = "postgresql"
    state["db_manager"] = db_mgr

    result = _run(executor_node(state))

    err = result["error_log"]
    assert err["dialect"] == "postgresql"


# ---------------------------------------------------------------------------
# DB-002: LIMIT injection
# ---------------------------------------------------------------------------

def test_executor_injects_limit_when_missing():
    """DB-002: Queries without LIMIT get LIMIT 1000 appended."""
    from agents.nodes.executor import executor_node

    captured_sql = []

    def capture_sql(sql):
        captured_sql.append(sql)
        return [{"id": 1}]

    db_mgr = _make_mock_db_manager()
    db_mgr.execute_query.side_effect = capture_sql

    state = make_agent_state()
    state["generated_sql"] = "SELECT id FROM users"
    state["db_manager"] = db_mgr

    result = _run(executor_node(state))

    # The SQL passed to execute_query should have LIMIT appended
    assert len(captured_sql) == 1
    assert "LIMIT" in captured_sql[0].upper()
    assert "1000" in captured_sql[0]


def test_executor_no_limit_injection_when_limit_present():
    """DB-002: Queries already containing LIMIT are NOT given a second LIMIT."""
    from agents.nodes.executor import executor_node

    captured_sql = []

    def capture_sql(sql):
        captured_sql.append(sql)
        return [{"id": 1}]

    db_mgr = _make_mock_db_manager()
    db_mgr.execute_query.side_effect = capture_sql

    state = make_agent_state()
    state["generated_sql"] = "SELECT id FROM users LIMIT 50"
    state["db_manager"] = db_mgr

    result = _run(executor_node(state))

    assert len(captured_sql) == 1
    # Should not have duplicate LIMIT
    sql_upper = captured_sql[0].upper()
    assert sql_upper.count("LIMIT") == 1
    # The original LIMIT value should be preserved
    assert "50" in captured_sql[0]


def test_executor_limit_injection_on_cte():
    """DB-002: CTE (WITH ... SELECT) without LIMIT gets LIMIT appended."""
    from agents.nodes.executor import executor_node

    captured_sql = []

    def capture_sql(sql):
        captured_sql.append(sql)
        return []

    db_mgr = _make_mock_db_manager()
    db_mgr.execute_query.side_effect = capture_sql

    state = make_agent_state()
    state["generated_sql"] = "WITH cte AS (SELECT 1 AS n) SELECT * FROM cte"
    state["db_manager"] = db_mgr

    result = _run(executor_node(state))

    assert len(captured_sql) == 1
    assert "LIMIT" in captured_sql[0].upper()


# ---------------------------------------------------------------------------
# AGENT-005: DB execution error returns structured error_log
# ---------------------------------------------------------------------------

def test_executor_db_exception_returns_structured_error():
    """AGENT-005: Exception during execute_query returns structured error_log dict."""
    from agents.nodes.executor import executor_node

    db_mgr = _make_mock_db_manager(side_effect=Exception("table not found"))
    state = make_agent_state()
    state["generated_sql"] = "SELECT * FROM nonexistent LIMIT 10"
    state["db_type"] = "duckdb"
    state["db_manager"] = db_mgr

    result = _run(executor_node(state))

    err = result["error_log"]
    assert isinstance(err, dict)
    assert "error_type" in err
    assert "message" in err
    assert "dialect" in err
    assert err["dialect"] == "duckdb"
    assert "failed_sql" in err
    assert "hint" in err
    assert result["db_results"] is None


# ---------------------------------------------------------------------------
# AGENT-005: Timeout produces error_log with timeout error_type
# ---------------------------------------------------------------------------

def test_executor_timeout_returns_error():
    """AGENT-005: Query exceeding timeout produces error_log with error_type=timeout."""
    from agents.nodes.executor import executor_node

    def slow_query(sql):
        import time
        time.sleep(10)  # Will be interrupted by timeout
        return []

    db_mgr = _make_mock_db_manager()
    db_mgr.execute_query.side_effect = slow_query

    state = make_agent_state()
    state["generated_sql"] = "SELECT * FROM big_table LIMIT 10"
    state["db_type"] = "sqlite"
    state["db_manager"] = db_mgr

    # Patch config to use a very short timeout for the test
    with patch("agents.nodes.executor._get_timeout", return_value=0.1):
        result = _run(executor_node(state))

    err = result["error_log"]
    assert isinstance(err, dict)
    assert err["error_type"] == "timeout"
    assert "timeout" in err["message"].lower() or "timed" in err["message"].lower()
    assert result["db_results"] is None
