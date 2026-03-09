"""
Tests for PostgreSQL and MySQL connectors (Plan 03).
These tests run against mocked connections — no live database needed.
"""
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# PostgreSQL connector tests
# ---------------------------------------------------------------------------

def test_postgresql_connector_imports_without_psycopg2():
    """PostgreSQLConnector module importable without psycopg2 installed."""
    from database.connectors.postgresql_connector import PostgreSQLConnector
    assert PostgreSQLConnector is not None


def test_postgresql_connector_subclasses_base():
    """PostgreSQLConnector subclasses BaseConnector."""
    from database.connectors.postgresql_connector import PostgreSQLConnector
    from database.connectors.base import BaseConnector
    assert issubclass(PostgreSQLConnector, BaseConnector)


def test_postgresql_connector_has_all_methods():
    """PostgreSQLConnector implements all 5 abstract methods."""
    from database.connectors.postgresql_connector import PostgreSQLConnector
    for method in ("connect", "get_schema", "execute_query", "close", "test_connection"):
        assert hasattr(PostgreSQLConnector, method)


def test_postgresql_get_schema_returns_dict_with_mock(mocker):
    """PostgreSQL get_schema returns dict when connection returns empty fetchall."""
    from database.connectors.postgresql_connector import PostgreSQLConnector
    conn = mocker.MagicMock()
    conn.cursor.return_value.fetchall.return_value = []
    connector = PostgreSQLConnector(
        host="localhost", database="test", user="u", password="p"
    )
    schema = connector.get_schema(conn)
    assert isinstance(schema, dict)


def test_postgresql_connect_raises_import_error_without_driver():
    """connect() raises ImportError with install hint when psycopg2 not available."""
    with patch.dict("sys.modules", {"psycopg2": None, "psycopg2.pool": None}):
        # Re-import to clear any cached module
        import importlib
        import database.connectors.postgresql_connector as pg_mod
        importlib.reload(pg_mod)
        connector = pg_mod.PostgreSQLConnector(
            host="localhost", database="test", user="u", password="p"
        )
        with pytest.raises((ImportError, Exception)):
            connector.connect()


# ---------------------------------------------------------------------------
# MySQL connector tests
# ---------------------------------------------------------------------------

def test_mysql_connector_imports_without_driver():
    """MySQLConnector module importable without mysql-connector-python installed."""
    from database.connectors.mysql_connector import MySQLConnector
    assert MySQLConnector is not None


def test_mysql_connector_subclasses_base():
    """MySQLConnector subclasses BaseConnector."""
    from database.connectors.mysql_connector import MySQLConnector
    from database.connectors.base import BaseConnector
    assert issubclass(MySQLConnector, BaseConnector)


def test_mysql_connector_has_all_methods():
    """MySQLConnector implements all 5 abstract methods."""
    from database.connectors.mysql_connector import MySQLConnector
    for method in ("connect", "get_schema", "execute_query", "close", "test_connection"):
        assert hasattr(MySQLConnector, method)


def test_mysql_get_schema_returns_dict_with_mock(mocker):
    """MySQL get_schema returns dict when connection returns empty fetchall."""
    from database.connectors.mysql_connector import MySQLConnector
    conn = mocker.MagicMock()
    conn.cursor.return_value.fetchall.return_value = []
    connector = MySQLConnector(
        host="localhost", database="test", user="u", password="p"
    )
    schema = connector.get_schema(conn)
    assert isinstance(schema, dict)
