"""DatabaseManager facade: factory, schema cache, refresh_schema()."""
from __future__ import annotations

import os

from dotenv import load_dotenv

from database.connectors.base import BaseConnector
from database.connectors.duckdb_connector import DuckDBConnector
from database.connectors.sqlite_connector import SQLiteConnector

load_dotenv(override=False)

_CONNECTOR_REGISTRY: dict[str, type[BaseConnector]] = {
    "duckdb": DuckDBConnector,
    "sqlite": SQLiteConnector,
    # "postgresql" and "mysql" added in Plan 03 via lazy/conditional import
    # to avoid ImportError for users who haven't installed those optional extras.
}


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
        if db_type not in _CONNECTOR_REGISTRY:
            raise ValueError(
                f"Unsupported db_type: {db_type!r}. "
                f"Supported: {list(_CONNECTOR_REGISTRY)}"
            )
        connector_cls = _CONNECTOR_REGISTRY[db_type]
        self._connector: BaseConnector = connector_cls(**kwargs)  # type: ignore[arg-type]
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
