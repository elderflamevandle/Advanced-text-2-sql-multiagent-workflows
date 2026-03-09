"""
Tests for DatabaseManager (DB-001).
DuckDB and SQLite tests pass after Plan 02.
PostgreSQL, MySQL, and retry tests remain xfail until Plan 03.
"""
import pytest

# Import will succeed after database/manager.py exists (Plan 02)
try:
    from database.manager import DatabaseManager
except ImportError:
    DatabaseManager = None


@pytest.fixture
def duckdb_manager(chinook_db_path):
    return DatabaseManager(db_type="duckdb", db_path=chinook_db_path)


@pytest.fixture
def sqlite_manager(chinook_db_path):
    return DatabaseManager(db_type="sqlite", db_path=chinook_db_path)


# --------------------------------------------------------------------------
# DuckDB + SQLite tests — these pass after Plan 02
# --------------------------------------------------------------------------

def test_duckdb_connection(duckdb_manager):
    assert duckdb_manager.test_connection() is True


def test_sqlite_connection(sqlite_manager):
    assert sqlite_manager.test_connection() is True


def test_schema_introspection(sqlite_manager):
    schema = sqlite_manager.get_schema()
    assert isinstance(schema, dict)
    assert len(schema) >= 4, "Expected at least 4 tables in schema"


def test_schema_primary_keys(sqlite_manager):
    schema = sqlite_manager.get_schema()
    artist = schema.get("Artist") or schema.get("artist")
    assert artist is not None, "Artist table not in schema"
    assert len(artist["primary_keys"]) >= 1


def test_schema_foreign_keys(sqlite_manager):
    schema = sqlite_manager.get_schema()
    track = schema.get("Track") or schema.get("track")
    assert track is not None, "Track table not in schema"
    assert len(track["foreign_keys"]) >= 1


def test_schema_sample_rows(sqlite_manager):
    schema = sqlite_manager.get_schema()
    for table_name, table_info in schema.items():
        assert "sample_rows" in table_info
        assert len(table_info["sample_rows"]) <= 2


def test_schema_caching(sqlite_manager):
    schema1 = sqlite_manager.get_schema()
    schema2 = sqlite_manager.get_schema()
    assert schema1 is schema2, "get_schema() must return cached object on second call"


def test_schema_refresh(sqlite_manager):
    schema1 = sqlite_manager.get_schema()
    schema2 = sqlite_manager.refresh_schema()
    assert schema1 is not schema2, "refresh_schema() must return new object"


# --------------------------------------------------------------------------
# Plan 03 tests — remain xfail until PostgreSQL/MySQL connectors implemented
# --------------------------------------------------------------------------

def test_connection_retry(mocker):
    """Retry logic fires on transient ConnectionError (mocked)."""
    if DatabaseManager is None:
        pytest.xfail("DatabaseManager not implemented")
    call_count = {"n": 0}

    def flaky_connect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise ConnectionError("transient failure")
        return mocker.MagicMock()

    with mocker.patch("database.connectors.duckdb_connector.DuckDBConnector.connect", side_effect=flaky_connect):
        manager = DatabaseManager(db_type="duckdb", db_path=":memory:")
        manager.test_connection()
    assert call_count["n"] == 3, "Expected exactly 3 connection attempts"


def test_postgresql_mock(mock_pg_conn, mocker):
    """PostgreSQL connector returns expected schema shape (no live DB needed)."""
    if DatabaseManager is None:
        pytest.xfail("DatabaseManager not implemented")
    mock_pg_conn.cursor.return_value.fetchall.return_value = [("users",), ("orders",)]
    mocker.patch("database.connectors.postgresql_connector.PostgreSQLConnector.connect", return_value=mock_pg_conn)
    manager = DatabaseManager(db_type="postgresql", host="localhost", database="test", user="u", password="p")
    schema = manager.get_schema()
    assert isinstance(schema, dict)


def test_mysql_mock(mock_mysql_conn, mocker):
    """MySQL connector returns expected schema shape (no live DB needed)."""
    if DatabaseManager is None:
        pytest.xfail("DatabaseManager not implemented")
    mocker.patch("database.connectors.mysql_connector.MySQLConnector.connect", return_value=mock_mysql_conn)
    manager = DatabaseManager(db_type="mysql", host="localhost", database="test", user="u", password="p")
    schema = manager.get_schema()
    assert isinstance(schema, dict)
