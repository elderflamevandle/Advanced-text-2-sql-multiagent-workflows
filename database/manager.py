"""DatabaseManager facade: factory, schema cache, refresh_schema()."""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import yaml
from dotenv import load_dotenv

from database.connectors.base import BaseConnector, QueryTimeoutError
from database.connectors.duckdb_connector import DuckDBConnector
from database.connectors.sqlite_connector import SQLiteConnector

load_dotenv(override=False)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level active manager registry
# Shared across threads; set by the sidebar on connect, read by agent nodes.
# Keeps DatabaseManager out of LangGraph AgentState (not msgpack-serializable).
# ---------------------------------------------------------------------------
_active_manager: "DatabaseManager | None" = None


def set_active_manager(mgr: "DatabaseManager") -> None:
    global _active_manager
    _active_manager = mgr


def get_active_manager() -> "DatabaseManager | None":
    return _active_manager


class QueryRows(list):
    """Rows returned by DatabaseManager.execute_query().

    A plain ``list[dict]`` with one extra attribute: ``truncated`` is True when
    the query produced more than ``max_rows`` rows and the surplus was dropped.
    """

    truncated: bool = False


def _server_timeout_ms(kwargs: dict) -> int:
    """Server-side statement timeout for PostgreSQL/MySQL, in milliseconds.

    Taken from the ``statement_timeout_ms`` kwarg, else ``database.query_timeout``
    in config/config.yaml plus a 5 s margin, so the client-side cancel fires
    first and the server limit is only the backstop.
    """
    if kwargs.get("statement_timeout_ms") is not None:
        return int(kwargs["statement_timeout_ms"])
    seconds = 60.0
    try:
        config_path = Path(__file__).parent.parent / "config" / "config.yaml"
        with open(config_path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        seconds = float(cfg.get("database", {}).get("query_timeout", 60))
    except Exception:
        pass
    return int((seconds + 5) * 1000)


class _Watchdog:
    """Cancel the query on ``connection`` if it is still running after ``timeout`` s."""

    def __init__(self, connector: BaseConnector, connection: object, timeout: float) -> None:
        self._connector = connector
        self._connection = connection
        self._lock = threading.Lock()
        self._done = False
        self.timed_out = False
        self._timer = threading.Timer(timeout, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def _fire(self) -> None:
        # The lock guarantees we never cancel a connection that has already
        # been released (and possibly handed to another query).
        with self._lock:
            if self._done:
                return
            self.timed_out = True
            try:
                self._connector.cancel(self._connection)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not cancel timed-out query: %s", exc)

    def finish(self) -> None:
        self._timer.cancel()
        with self._lock:
            self._done = True


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
            schema=kwargs.get("schema", os.getenv("DB_SCHEMA", "public")),
            statement_timeout_ms=_server_timeout_ms(kwargs),
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
            max_execution_time_ms=_server_timeout_ms(kwargs),
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
        self.db_type: str = db_type
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
            with self._connector.connection() as conn:
                self._schema_cache = self._connector.get_schema(conn)
        return self._schema_cache

    def refresh_schema(self) -> dict:
        """Clear the schema cache and return a freshly fetched dict."""
        self._schema_cache = None
        return self.get_schema()

    def execute_query(
        self,
        sql: str,
        timeout: float | None = None,
        max_rows: int | None = None,
    ) -> QueryRows:
        """Execute a read-only SQL query and return rows as a list of dicts.

        Args:
            sql: the statement to run.
            timeout: seconds before the running statement is cancelled in the
                database; ``QueryTimeoutError`` is raised in that case.
                ``None`` disables the client-side watchdog.
            max_rows: keep at most this many rows; the returned list has
                ``.truncated == True`` if the query produced more.

        The connection is always released (returned to the pool / closed),
        also when the query fails or is cancelled.
        """
        with self._connector.connection() as conn:
            watchdog = (
                _Watchdog(self._connector, conn, timeout)
                if timeout is not None and timeout > 0
                else None
            )
            try:
                rows, truncated = self._connector.execute_query_limited(conn, sql, max_rows)
            except Exception as exc:
                if watchdog is not None and watchdog.timed_out:
                    raise QueryTimeoutError(
                        f"Query cancelled after exceeding {timeout:g}s timeout"
                    ) from exc
                raise
            finally:
                if watchdog is not None:
                    watchdog.finish()
        result = QueryRows(rows)
        result.truncated = bool(truncated)
        return result
