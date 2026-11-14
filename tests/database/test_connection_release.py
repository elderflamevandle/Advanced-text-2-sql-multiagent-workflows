"""Connection acquisition/release for the pooled connectors (drivers are mocked).

psycopg2 and mysql-connector are not installed in the test environment, so
these tests check our calls against a fake driver, not real server behaviour.
"""
import sys
from unittest.mock import MagicMock

import pytest

from database.manager import DatabaseManager, QueryTimeoutError


def _make_conn(fail_on=None):
    """A fake DB-API connection whose cursors return one row, or raise for some SQL."""
    conn = MagicMock(name="conn")
    conn.closed = 0

    def make_cursor(*args, **kwargs):
        cur = MagicMock(name="cursor")
        cur.description = [("n",)]
        cur.fetchmany.return_value = [(1,)]
        cur.fetchall.return_value = [(1,)]

        def execute(sql, *params):
            if fail_on and fail_on in sql:
                raise RuntimeError("boom")

        cur.execute.side_effect = execute
        return cur

    conn.cursor.side_effect = make_cursor
    return conn


@pytest.fixture
def fake_psycopg2(monkeypatch):
    """Install a fake psycopg2 whose pool tracks checked-out connections."""
    module = MagicMock(name="psycopg2")
    pool = MagicMock(name="pool")
    pool.out = []
    pool.max = 10

    def getconn():
        if len(pool.out) >= pool.max:
            raise RuntimeError("connection pool exhausted")
        conn = _make_conn(fail_on="broken")
        pool.out.append(conn)
        return conn

    def putconn(conn, close=False):
        pool.out.remove(conn)

    pool.getconn.side_effect = getconn
    pool.putconn.side_effect = putconn
    module.pool.ThreadedConnectionPool.return_value = pool
    monkeypatch.setitem(sys.modules, "psycopg2", module)
    monkeypatch.setitem(sys.modules, "psycopg2.pool", module.pool)
    return module, pool


def _pg_manager(**kwargs):
    return DatabaseManager(
        db_type="postgresql", host="h", database="d", user="u", password="p", **kwargs
    )


def test_postgres_25_queries_return_every_connection(fake_psycopg2):
    _, pool = fake_psycopg2
    mgr = _pg_manager()
    for _ in range(25):  # more than maxconn=10: would exhaust a leaking pool
        assert mgr.execute_query("SELECT 1", max_rows=5) == [{"n": 1}]
    assert pool.getconn.call_count == 25
    assert pool.putconn.call_count == 25
    assert pool.out == []


def test_postgres_connection_returned_and_rolled_back_on_error(fake_psycopg2):
    _, pool = fake_psycopg2
    mgr = _pg_manager()
    seen = []
    original = pool.getconn.side_effect
    pool.getconn.side_effect = lambda: seen.append(original()) or seen[-1]
    for _ in range(25):
        with pytest.raises(RuntimeError, match="boom"):
            mgr.execute_query("SELECT broken")
    assert pool.getconn.call_count == pool.putconn.call_count == 25
    assert pool.out == []
    assert all(conn.rollback.call_count == 1 for conn in seen)
    assert mgr.execute_query("SELECT 1") == [{"n": 1}]  # pool still usable


def test_postgres_schema_and_test_connection_release(fake_psycopg2):
    _, pool = fake_psycopg2
    mgr = _pg_manager()
    assert mgr.test_connection() is True
    assert isinstance(mgr.get_schema(), dict)
    assert pool.getconn.call_count == pool.putconn.call_count == 2
    assert pool.out == []


def test_postgres_session_is_read_only_with_statement_timeout(fake_psycopg2):
    module, pool = fake_psycopg2
    seen = []
    original = pool.getconn.side_effect
    pool.getconn.side_effect = lambda: seen.append(original()) or seen[-1]
    mgr = _pg_manager(statement_timeout_ms=1234, schema="sales, public")
    mgr.execute_query("SELECT 1")

    kwargs = module.pool.ThreadedConnectionPool.call_args.kwargs
    assert "-c default_transaction_read_only=on" in kwargs["options"]
    assert "-c statement_timeout=1234" in kwargs["options"]
    assert "-c search_path=sales,public" in kwargs["options"]
    seen[0].set_session.assert_called_once_with(readonly=True, autocommit=True)


def test_postgres_broken_connection_is_discarded(fake_psycopg2):
    _, pool = fake_psycopg2
    mgr = _pg_manager()
    conn = mgr._connector.acquire()
    conn.rollback.side_effect = RuntimeError("connection lost")
    mgr._connector.release(conn, error=True)
    assert pool.putconn.call_args.kwargs == {"close": True}
    assert pool.out == []


def test_postgres_failed_session_setup_does_not_leak(fake_psycopg2):
    _, pool = fake_psycopg2
    original = pool.getconn.side_effect

    def getconn():
        conn = original()
        conn.set_session.side_effect = RuntimeError("cannot set read only")
        return conn

    pool.getconn.side_effect = getconn
    with pytest.raises(RuntimeError, match="read only"):
        _pg_manager().execute_query("SELECT 1")
    assert pool.out == []


def test_postgres_timeout_cancels_and_releases(fake_psycopg2):
    import threading

    _, pool = fake_psycopg2
    cancelled = threading.Event()
    original = pool.getconn.side_effect

    def getconn():
        conn = original()
        conn.cancel.side_effect = cancelled.set

        def make_cursor(*a, **k):
            cur = MagicMock()

            def execute(sql):
                if not cancelled.wait(5):
                    raise AssertionError("cancel() was never called")
                raise RuntimeError("canceling statement due to user request")

            cur.execute.side_effect = execute
            return cur

        conn.cursor.side_effect = make_cursor
        return conn

    pool.getconn.side_effect = getconn
    with pytest.raises(QueryTimeoutError):
        _pg_manager().execute_query("SELECT pg_sleep(60)", timeout=0.1)
    assert pool.getconn.call_count == pool.putconn.call_count == 1


def test_postgres_schema_names_are_validated():
    from database.connectors.postgresql_connector import PostgreSQLConnector

    assert PostgreSQLConnector()._schemas == ["public"]
    assert PostgreSQLConnector(schema=["a", "b_2"])._schemas == ["a", "b_2"]
    with pytest.raises(ValueError):
        PostgreSQLConnector(schema="public; DROP")


def test_postgres_get_schema_uses_configured_schema_and_keeps_shape():
    from database.connectors.postgresql_connector import PostgreSQLConnector

    cur = MagicMock()
    cur.description = [("id",)]
    results = iter(
        [
            [("orders",)],                     # tables
            [("id", "integer", "NO")],         # columns
            [("id",)],                         # primary keys
            [],                                # foreign keys
            [(1,), (2,)],                      # sample rows
        ]
    )
    cur.fetchall.side_effect = lambda: next(results)
    conn = MagicMock()
    conn.cursor.return_value = cur

    schema = PostgreSQLConnector(schema="sales").get_schema(conn)

    assert schema == {
        "orders": {
            "columns": [{"name": "id", "type": "integer", "nullable": False}],
            "primary_keys": ["id"],
            "foreign_keys": [],
            "sample_rows": [{"id": 1}, {"id": 2}],
        }
    }
    first_call = cur.execute.call_args_list[0]
    assert first_call.args[1] == ("sales",)
    assert cur.execute.call_args_list[-1].args[0] == 'SELECT * FROM "sales"."orders" LIMIT 2'


# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_mysql(monkeypatch):
    connector_mod = MagicMock(name="mysql.connector")
    root = MagicMock(name="mysql")
    root.connector = connector_mod
    pool = MagicMock(name="pool")
    pool.out = []
    pool.all = []

    def get_connection():
        if len(pool.out) >= 5:
            raise RuntimeError("pool exhausted")
        conn = _make_conn(fail_on="broken")
        conn.connection_id = 77
        conn.close.side_effect = lambda: pool.out.remove(conn)
        pool.out.append(conn)
        pool.all.append(conn)
        return conn

    pool.get_connection.side_effect = get_connection
    connector_mod.pooling.MySQLConnectionPool.return_value = pool
    monkeypatch.setitem(sys.modules, "mysql", root)
    monkeypatch.setitem(sys.modules, "mysql.connector", connector_mod)
    monkeypatch.setitem(sys.modules, "mysql.connector.pooling", connector_mod.pooling)
    return connector_mod, pool


def _mysql_manager(**kwargs):
    return DatabaseManager(
        db_type="mysql", host="h", database="d", user="u", password="p", **kwargs
    )


def test_mysql_25_queries_return_every_connection(fake_mysql):
    _, pool = fake_mysql
    mgr = _mysql_manager()
    for i in range(25):
        if i % 2:
            with pytest.raises(RuntimeError, match="boom"):
                mgr.execute_query("SELECT broken")
        else:
            assert mgr.execute_query("SELECT 1", max_rows=5) == [{"n": 1}]
    assert pool.get_connection.call_count == 25
    assert pool.out == []
    assert all(c.close.call_count == 1 and c.rollback.call_count == 1 for c in pool.all)


def test_mysql_session_is_read_only_with_time_limit(fake_mysql):
    _, pool = fake_mysql
    mgr = _mysql_manager(statement_timeout_ms=4321)
    executed = []
    original = pool.get_connection.side_effect

    def get_connection():
        conn = original()

        def make_cursor(*a, **k):
            cur = MagicMock()
            cur.execute.side_effect = lambda sql, *p: executed.append(sql)
            return cur

        conn.cursor.side_effect = make_cursor
        return conn

    pool.get_connection.side_effect = get_connection
    conn = mgr._connector.acquire()
    mgr._connector.release(conn)
    assert executed == [
        "SET SESSION TRANSACTION READ ONLY",
        "SET SESSION max_execution_time = 4321",
    ]


def test_mysql_read_only_setup_failure_does_not_hand_out_connection(fake_mysql):
    _, pool = fake_mysql
    original = pool.get_connection.side_effect

    def get_connection():
        conn = original()
        cur = MagicMock()
        cur.execute.side_effect = RuntimeError("READ ONLY not supported")
        conn.cursor.side_effect = None
        conn.cursor.return_value = cur
        return conn

    pool.get_connection.side_effect = get_connection
    with pytest.raises(RuntimeError, match="READ ONLY"):
        _mysql_manager().execute_query("SELECT 1")
    assert pool.out == []


def test_mysql_cancel_kills_query_on_side_connection(fake_mysql):
    connector_mod, _ = fake_mysql
    mgr = _mysql_manager()
    conn = mgr._connector.acquire()
    mgr._connector.cancel(conn)
    side = connector_mod.connect.return_value
    side.cursor.return_value.execute.assert_called_once_with("KILL QUERY 77")
    side.close.assert_called_once()
    mgr._connector.release(conn)
