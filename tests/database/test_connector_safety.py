"""Read-only enforcement, bounded fetching, value serialisation and timeouts
for the SQLite and DuckDB connectors. All databases live under tmp_path."""
import datetime
import decimal
import json
import sqlite3
import threading
import time
import uuid

import duckdb
import pytest

from database.connectors.base import (
    BaseConnector,
    QueryTimeoutError,
    fetch_limited,
    to_plain_value,
)
from database.connectors.duckdb_connector import DuckDBConnector
from database.connectors.sqlite_connector import SQLiteConnector, _read_only_uri
from database.manager import DatabaseManager

SLOW_SQLITE = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
)
SLOW_DUCKDB = "SELECT count(*) FROM range(100000000000) a, range(1000000) b"


@pytest.fixture
def sqlite_file(tmp_path):
    path = tmp_path / "sample db.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT NOT NULL);"
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id),"
        " payload BLOB);"
    )
    conn.executemany("INSERT INTO parent VALUES (?, ?)", [(i, f"n{i}") for i in range(1, 51)])
    conn.execute("INSERT INTO child VALUES (1, 1, x'00ff10')")
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def duckdb_file(tmp_path):
    path = tmp_path / "sample.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, amount DECIMAL(10,2), d DATE)")
    conn.execute("INSERT INTO t SELECT i, i * 1.5, DATE '2024-01-01' FROM range(1, 51) r(i)")
    conn.close()
    return str(path)


# ---------------------------------------------------------------------------
# Read-only enforcement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO parent VALUES (999, 'x')",
        "UPDATE parent SET name = 'x'",
        "DELETE FROM parent",
        "DROP TABLE parent",
        "CREATE TABLE evil (a)",
    ],
)
def test_sqlite_rejects_writes(sqlite_file, sql):
    connector = SQLiteConnector(db_path=sqlite_file)
    for getter in (connector.connect, connector.acquire):
        conn = getter()
        with pytest.raises(sqlite3.OperationalError):
            connector.execute_query(conn, sql)
    with connector.connection() as conn:
        assert connector.execute_query(conn, "SELECT count(*) AS n FROM parent") == [{"n": 50}]


def test_sqlite_query_only_cannot_be_bypassed_by_mode_ro(sqlite_file):
    """Even if query_only were switched off, the file handle itself is read-only."""
    connector = SQLiteConnector(db_path=sqlite_file)
    conn = connector.connect()
    conn.execute("PRAGMA query_only = OFF")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO parent VALUES (999, 'x')")


def test_sqlite_memory_is_query_only():
    connector = SQLiteConnector(db_path=":memory:")
    with pytest.raises(sqlite3.OperationalError):
        connector.execute_query(connector.connect(), "CREATE TABLE t (a)")
    assert connector.acquire() is connector.connect()  # in-memory DB: one shared connection


def test_sqlite_missing_file_is_not_created(tmp_path):
    missing = tmp_path / "nope.db"
    connector = SQLiteConnector(db_path=str(missing))
    assert connector.test_connection() is False
    assert not missing.exists()


def test_sqlite_schema_introspection_still_works(sqlite_file):
    schema = DatabaseManager(db_type="sqlite", db_path=sqlite_file).get_schema()
    assert set(schema) == {"parent", "child"}
    assert schema["parent"]["primary_keys"] == ["id"]
    assert schema["child"]["foreign_keys"][0]["references_table"] == "parent"
    assert len(schema["parent"]["sample_rows"]) == 2
    json.dumps(schema)  # blob sample values were converted to placeholders


def test_sqlite_read_only_uri_forms(tmp_path):
    uri = _read_only_uri(str(tmp_path / "a b.db"))
    assert uri.startswith("file:///") and uri.endswith("?mode=ro") and "a%20b.db" in uri
    assert _read_only_uri("file:x.db") == "file:x.db?mode=ro"
    assert _read_only_uri("file:x.db?cache=shared") == "file:x.db?cache=shared&mode=ro"
    assert _read_only_uri("file:x.db?mode=rw") == "file:x.db?mode=rw"


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (999, 1.0, DATE '2024-01-01')",
        "DELETE FROM t",
        "CREATE TABLE evil (a INTEGER)",
        "DROP TABLE t",
    ],
)
def test_duckdb_rejects_writes(duckdb_file, sql):
    connector = DuckDBConnector(db_path=duckdb_file)
    with pytest.raises(duckdb.Error):
        with connector.connection() as conn:
            connector.execute_query(conn, sql)
    with pytest.raises(duckdb.Error):
        connector.execute_query(connector.connect(), sql)
    with connector.connection() as conn:
        assert connector.execute_query(conn, "SELECT count(*) AS n FROM t") == [{"n": 50}]


def test_duckdb_blocks_external_file_access(duckdb_file, tmp_path):
    csv = tmp_path / "secret.csv"
    csv.write_text("a\n1\n")
    target = tmp_path / "out.csv"
    connector = DuckDBConnector(db_path=duckdb_file)
    with connector.connection() as conn:
        with pytest.raises(duckdb.Error):
            connector.execute_query(conn, f"SELECT * FROM read_csv('{csv}')")
        with pytest.raises(duckdb.Error):
            connector.execute_query(conn, f"COPY (SELECT 1) TO '{target}'")
        with pytest.raises(duckdb.Error):
            connector.execute_query(conn, "SET enable_external_access = true")
    assert not target.exists()


def test_duckdb_opens_sqlite_file_read_only(sqlite_file):
    """The default config points DuckDB at a SQLite file; that must keep working."""
    mgr = DatabaseManager(db_type="duckdb", db_path=sqlite_file)
    assert mgr.test_connection() is True
    assert mgr.execute_query("SELECT count(*) AS n FROM parent") == [{"n": 50}]
    assert "parent" in mgr.get_schema()
    with pytest.raises(duckdb.Error):
        mgr.execute_query("INSERT INTO parent VALUES (999, 'x')")


def test_duckdb_memory_database_still_usable():
    mgr = DatabaseManager(db_type="duckdb", db_path=":memory:")
    assert mgr.execute_query("SELECT 41 + 1 AS answer") == [{"answer": 42}]


# ---------------------------------------------------------------------------
# Bounded fetch + truncation flag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("db_type", ["sqlite", "duckdb"])
def test_manager_truncates_and_flags(db_type, sqlite_file):
    mgr = DatabaseManager(db_type=db_type, db_path=sqlite_file)

    rows = mgr.execute_query("SELECT id FROM parent ORDER BY id", max_rows=10)
    assert isinstance(rows, list) and len(rows) == 10 and rows.truncated is True
    assert rows[0] == {"id": 1}

    exact = mgr.execute_query("SELECT id FROM parent", max_rows=50)
    assert len(exact) == 50 and exact.truncated is False

    everything = mgr.execute_query("SELECT id FROM parent")
    assert len(everything) == 50 and everything.truncated is False


def test_fetch_limited_never_calls_fetchall_when_bounded(mocker):
    cur = mocker.MagicMock()
    cur.description = [("a",)]
    cur.fetchmany.return_value = [(1,), (2,), (3,)]
    rows, truncated = fetch_limited(cur, 2)
    assert rows == [{"a": 1}, {"a": 2}] and truncated is True
    cur.fetchmany.assert_called_once_with(3)
    cur.fetchall.assert_not_called()


def test_fetch_limited_without_result_set(mocker):
    cur = mocker.MagicMock()
    cur.description = None
    assert fetch_limited(cur, 5) == ([], False)


def test_base_default_execute_query_limited_slices():
    class Legacy(BaseConnector):
        released = []

        def connect(self):
            return "conn"

        def get_schema(self, connection):
            return {}

        def execute_query(self, connection, sql):
            return [{"n": i} for i in range(5)]

        def close(self, connection):
            pass

        def test_connection(self):
            return True

    legacy = Legacy()
    assert legacy.execute_query_limited("conn", "x", 3) == ([{"n": 0}, {"n": 1}, {"n": 2}], True)
    assert legacy.execute_query_limited("conn", "x", 5)[1] is False
    legacy.cancel("conn")  # default: no-op
    with legacy.connection() as conn:
        assert conn == "conn"


# ---------------------------------------------------------------------------
# Value serialisation
# ---------------------------------------------------------------------------

def test_to_plain_value_conversions():
    assert to_plain_value(decimal.Decimal("1.50")) == 1.5
    assert to_plain_value(datetime.date(2024, 1, 2)) == "2024-01-02"
    assert to_plain_value(datetime.datetime(2024, 1, 2, 3, 4, 5)) == "2024-01-02T03:04:05"
    assert to_plain_value(datetime.time(3, 4, 5)) == "03:04:05"
    assert to_plain_value(b"\x00\x01\x02") == "<binary 3 bytes>"
    assert to_plain_value(memoryview(b"ab")) == "<binary 2 bytes>"
    u = uuid.uuid4()
    assert to_plain_value(u) == str(u)
    assert to_plain_value(2**70) == str(2**70)
    assert to_plain_value(True) is True and to_plain_value(None) is None
    assert to_plain_value(7) == 7 and to_plain_value("x") == "x" and to_plain_value(1.5) == 1.5
    assert to_plain_value({"a": (decimal.Decimal("2"), b"x")}) == {"a": [2.0, "<binary 1 bytes>"]}


def test_duckdb_rows_are_json_and_msgpack_friendly(duckdb_file):
    mgr = DatabaseManager(db_type="duckdb", db_path=duckdb_file)
    rows = mgr.execute_query(
        "SELECT amount, d, TIMESTAMP '2024-01-02 03:04:05' AS ts, 'ab'::BLOB AS b,"
        " [1, 2] AS l, {'k': 1.5::DECIMAL(4,2)} AS s FROM t WHERE id = 2"
    )
    assert rows == [
        {
            "amount": 3.0,
            "d": "2024-01-01",
            "ts": "2024-01-02T03:04:05",
            "b": "<binary 2 bytes>",
            "l": [1, 2],
            "s": {"k": 1.5},
        }
    ]
    json.dumps(list(rows))
    json.dumps(mgr.get_schema())


def test_sqlite_blob_becomes_placeholder(sqlite_file):
    mgr = DatabaseManager(db_type="sqlite", db_path=sqlite_file)
    assert mgr.execute_query("SELECT payload FROM child") == [{"payload": "<binary 3 bytes>"}]


# ---------------------------------------------------------------------------
# Cancellation at the manager level
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("db_type,sql", [("sqlite", SLOW_SQLITE), ("duckdb", SLOW_DUCKDB)])
def test_manager_timeout_cancels_query(db_type, sql, sqlite_file):
    mgr = DatabaseManager(db_type=db_type, db_path=sqlite_file)
    start = time.monotonic()
    with pytest.raises(QueryTimeoutError):
        mgr.execute_query(sql, timeout=0.3)
    assert time.monotonic() - start < 5
    # the connector is still usable afterwards
    assert mgr.execute_query("SELECT 1 AS one") == [{"one": 1}]


def test_watchdog_does_not_cancel_after_completion(sqlite_file, mocker):
    mgr = DatabaseManager(db_type="sqlite", db_path=sqlite_file)
    spy = mocker.spy(mgr._connector, "cancel")
    assert mgr.execute_query("SELECT 1 AS one", timeout=0.05) == [{"one": 1}]
    time.sleep(0.2)
    spy.assert_not_called()


def test_sqlite_cancel_only_hits_its_own_connection(sqlite_file):
    """acquire() gives each query its own connection, so cancel() is targeted."""
    connector = SQLiteConnector(db_path=sqlite_file)
    slow, other = connector.acquire(), connector.acquire()
    assert slow is not other
    errors = []

    def run_slow():
        try:
            connector.execute_query(slow, SLOW_SQLITE)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=run_slow)
    worker.start()
    time.sleep(0.2)
    assert connector.execute_query(other, "SELECT 1 AS one") == [{"one": 1}]
    connector.cancel(slow)
    worker.join(5)
    assert not worker.is_alive()
    assert len(errors) == 1 and "interrupt" in str(errors[0]).lower()
    connector.release(slow, error=True)
    connector.release(other)
