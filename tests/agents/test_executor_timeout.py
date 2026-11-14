"""Timeout behaviour of executor_node against real SQLite / DuckDB queries."""
import asyncio
import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

from agents.nodes.executor import executor_node
from database.manager import DatabaseManager
from tests.agents.conftest import make_agent_state

SAFE = {"safe": True, "statement_type": "SELECT", "reason": None}
SLOW = {
    "sqlite": (
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
        "SELECT count(*) AS n FROM c"
    ),
    "duckdb": "SELECT count(*) AS n FROM range(100000000000) a, range(1000000) b",
}
TIMEOUT = 0.4


@pytest.fixture
def db_file(tmp_path):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (i INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    return str(path)


def _state(sql, db_type):
    state = make_agent_state()
    state["generated_sql"] = sql
    state["db_type"] = db_type
    return state


@pytest.mark.parametrize("db_type", ["sqlite", "duckdb"])
def test_slow_query_times_out_and_is_cancelled(db_type, db_file):
    mgr = DatabaseManager(db_type=db_type, db_path=db_file)
    worker_done = threading.Event()
    real_execute = mgr.execute_query

    def tracked(*args, **kwargs):
        try:
            return real_execute(*args, **kwargs)
        finally:
            worker_done.set()

    mgr.execute_query = tracked

    async def scenario():
        """Run the node next to a ticker task to prove the loop is not blocked."""
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        tick_task = asyncio.create_task(ticker())
        start = time.monotonic()
        result = await executor_node(_state(SLOW[db_type], db_type))
        elapsed = time.monotonic() - start
        tick_task.cancel()
        return result, elapsed, ticks

    with (
        patch("database.manager.get_active_manager", return_value=mgr),
        patch("database.safety.scan_sql", return_value=SAFE),
        patch("agents.nodes.executor._get_timeout", return_value=TIMEOUT),
    ):
        result, elapsed, ticks = asyncio.run(scenario())

    # 1. structured timeout error, returned at about the timeout
    assert result["db_results"] is None and result["execution_metadata"] is None
    err = result["error_log"]
    assert err["error_type"] == "timeout"
    assert err["dialect"] == db_type
    assert "timeout" in err["message"].lower()
    assert TIMEOUT * 0.9 <= elapsed < TIMEOUT + 1.5, elapsed

    # 2. the event loop kept running while the query was executing
    assert ticks >= 5, ticks

    # 3. the query was cancelled in the database: the worker thread finishes
    assert worker_done.wait(5), "worker thread still running after timeout"

    # 4. the manager is still usable afterwards
    assert mgr.execute_query("SELECT i FROM t") == [{"i": 1}]


def test_node_returns_promptly_even_if_driver_cannot_cancel():
    """An uncancellable call must not hold the node (old ThreadPoolExecutor bug)."""
    release = threading.Event()

    class StuckManager:
        def execute_query(self, sql, timeout=None, max_rows=None):
            release.wait(20)
            return []

    try:
        with (
            patch("database.manager.get_active_manager", return_value=StuckManager()),
            patch("database.safety.scan_sql", return_value=SAFE),
            patch("agents.nodes.executor._get_timeout", return_value=0.2),
        ):
            start = time.monotonic()
            result = asyncio.run(executor_node(_state("SELECT 1", "sqlite")))
            elapsed = time.monotonic() - start
    finally:
        release.set()

    assert result["error_log"]["error_type"] == "timeout"
    assert elapsed < 1.5, elapsed


def test_manager_timeout_error_maps_to_timeout_error_type():
    """QueryTimeoutError raised by the manager is reported as a timeout, not 'unknown'."""
    from database.manager import QueryTimeoutError

    class Manager:
        def execute_query(self, sql, timeout=None, max_rows=None):
            raise QueryTimeoutError("cancelled")

    with (
        patch("database.manager.get_active_manager", return_value=Manager()),
        patch("database.safety.scan_sql", return_value=SAFE),
    ):
        result = asyncio.run(executor_node(_state("SELECT 1", "postgresql")))
    assert result["error_log"]["error_type"] == "timeout"
    assert result["error_log"]["message"].startswith("POSTGRESQL error:")


def test_fast_query_is_unaffected_by_timeout(db_file):
    mgr = DatabaseManager(db_type="sqlite", db_path=db_file)
    with (
        patch("database.manager.get_active_manager", return_value=mgr),
        patch("database.safety.scan_sql", return_value=SAFE),
        patch("agents.nodes.executor._get_timeout", return_value=5),
    ):
        result = asyncio.run(executor_node(_state("SELECT i FROM t", "sqlite")))
    assert result["db_results"] == [{"i": 1}]
    assert result["execution_metadata"]["truncated"] is False
