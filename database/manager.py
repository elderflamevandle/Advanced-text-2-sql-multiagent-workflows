"""DatabaseManager facade: factory, schema cache, refresh_schema()."""
from __future__ import annotations

import os
import logging

from dotenv import load_dotenv

from database.connectors.base import BaseConnector
from database.connectors.duckdb_connector import DuckDBConnector
from database.connectors.sqlite_connector import SQLiteConnector

load_dotenv(override=False)

logger = logging.getLogger(__name__)


def _get_connector(db_type: str, **kwargs) -> BaseConnector:
    """Factory: return a configured connector instance for the given db_type.

    PostgreSQL and MySQL are imported lazily so that the base package remains
    importable on systems without those optional extras installed.
    """
    db_type = db_type.lower()

    if db_type == "duckdb":
        db_path = kwargs.get("db_path", os.getenv("DB_PATH", "data/chinook.db"))
        return DuckDBConnector(db_path=db_path)

    elif db_type == "sqlite":
        db_path = kwargs.get("db_path", os.getenv("DB_PATH", "data/chinook.db"))
        return SQLiteConnector(db_path=db_path)

    elif db_type == "postgresql":
        try:
            from database.connectors.postgresql_connector import PostgreSQLConnector  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "PostgreSQL support requires: pip install -e '.[postgresql]'"
            ) from exc
        return PostgreSQLConnector(
            host=kwargs.get("host", os.getenv("DB_HOST", "localhost")),
            port=int(kwargs.get("port", os.getenv("DB_PORT", 5432))),
            database=kwargs.get("database", os.getenv("DB_NAME", "postgres")),
            user=kwargs.get("user", os.getenv("DB_USER", "postgres")),
            password=kwargs.get("password", os.getenv("DB_PASSWORD", "")),
        )

    elif db_type == "mysql":
        try:
            from database.connectors.mysql_connector import MySQLConnector  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "MySQL support requires: pip install -e '.[mysql]'"
            ) from exc
        return MySQLConnector(
            host=kwargs.get("host", os.getenv("DB_HOST", "localhost")),
            port=int(kwargs.get("port", os.getenv("DB_PORT", 3306))),
            database=kwargs.get("database", os.getenv("DB_NAME", "mysql")),
            user=kwargs.get("user", os.getenv("DB_USER", "root")),
            password=kwargs.get("password", os.getenv("DB_PASSWORD", "")),
        )

    else:
        raise ValueError(
            f"Unsupported db_type: {db_type!r}. "
            f"Supported: duckdb, sqlite, postgresql, mysql"
        )


class DatabaseManager:
    """Factory facade that wraps a dialect connector and caches the schema.

    Usage::

        mgr = DatabaseManager(db_type="sqlite", db_path="data/chinook.db")
        ok  = mgr.test_connection()       # -> True
        schema = mgr.get_schema()         # -> dict[str, SchemaTable]
        schema2 = mgr.get_schema()        # same object (cache hit)
        new_schema = mgr.refresh_schema() # new object (cache cleared)

    ``db_type`` defaults to the ``DB_TYPE`` environment variable, then "duckdb".
    """

    def __init__(self, db_type: str | None = None, **kwargs: object) -> None:
        db_type = (db_type or os.getenv("DB_TYPE", "duckdb")).lower()
        self._connector: BaseConnector = _get_connector(db_type, **kwargs)
        self._schema_cache: dict | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def test_connection(self) -> bool:
        """Delegate to the underlying connector's test_connection()."""
        return self._connector.test_connection()

    def get_schema(self) -> dict:
        """Return the schema dict, fetching and caching it on first call."""
        if self._schema_cache is None:
            conn = self._connector.connect()
            self._schema_cache = self._connector.get_schema(conn)
        return self._schema_cache

    def refresh_schema(self) -> dict:
        """Clear the schema cache and return a freshly fetched dict."""
        self._schema_cache = None
        return self.get_schema()

    def execute_query(self, sql: str) -> list[dict]:
        """Execute a read-only SQL query and return rows as list of dicts."""
        conn = self._connector.connect()
        return self._connector.execute_query(conn, sql)
