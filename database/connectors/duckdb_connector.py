"""Thread-safe DuckDB connector using cursor-per-thread pattern."""
from __future__ import annotations

import threading

import duckdb
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from database.connectors.base import BaseConnector, fetch_limited, rows_to_dicts
from database.schema_utils import ColumnInfo, FKInfo, SchemaTable

RETRY_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)


def _connection_retry(func):
    """Tenacity retry decorator: 3 attempts, exponential backoff 1s/2s/4s."""
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type(RETRY_EXCEPTIONS),
        reraise=True,
    )(func)


class DuckDBConnector(BaseConnector):
    """DuckDB connector.

    One parent connection is opened lazily. File databases are opened with
    ``read_only=True`` (this also covers SQLite files, which DuckDB attaches
    through its sqlite extension), and external file/network access from SQL
    is switched off, so queries cannot write or read arbitrary files.
    ``:memory:`` databases cannot be opened read-only and stay writable.

    ``connect()`` returns a per-thread cursor (legacy behaviour). ``acquire()``
    returns a fresh cursor per unit of work, which ``release()`` closes; each
    cursor is its own DuckDB connection, so ``cancel()`` interrupts one query.
    """

    def __init__(self, db_path: str = ":memory:", allow_external_access: bool = False):
        self._db_path = db_path
        self._allow_external_access = allow_external_access
        self._parent_conn: duckdb.DuckDBPyConnection | None = None
        self._local = threading.local()
        self._conn_lock = threading.Lock()

    @property
    def _is_memory(self) -> bool:
        return self._db_path in ("", ":memory:")

    def _parent(self) -> duckdb.DuckDBPyConnection:
        if self._parent_conn is None:
            with self._conn_lock:
                if self._parent_conn is None:
                    if self._is_memory:
                        conn = duckdb.connect(":memory:")
                    else:
                        conn = duckdb.connect(self._db_path, read_only=True)
                        if not self._allow_external_access:
                            try:
                                # Cannot be re-enabled by a later SET statement.
                                conn.execute("SET enable_external_access = false")
                            except Exception:
                                conn.close()
                                raise
                    self._parent_conn = conn
        return self._parent_conn

    # ------------------------------------------------------------------
    # BaseConnector interface
    # ------------------------------------------------------------------

    def connect(self) -> duckdb.DuckDBPyConnection:
        """Return a thread-local cursor copy of the parent connection."""
        parent = self._parent()
        if not hasattr(self._local, "cursor") or self._local.cursor is None:
            self._local.cursor = parent.cursor()
        return self._local.cursor

    def acquire(self) -> duckdb.DuckDBPyConnection:
        """Return a fresh cursor for one unit of work."""
        return self._parent().cursor()

    def release(self, connection: duckdb.DuckDBPyConnection, error: bool = False) -> None:
        """Close a cursor obtained from acquire(); thread-local cursors stay open."""
        if connection is getattr(self._local, "cursor", None):
            return
        try:
            connection.close()
        except Exception:
            pass

    def cancel(self, connection: duckdb.DuckDBPyConnection) -> None:
        """Interrupt the query running on this cursor (thread-safe)."""
        connection.interrupt()

    def test_connection(self) -> bool:
        """Return True if a test query succeeds.

        Uses a tenacity retry wrapper around connect() to handle transient
        ConnectionError / TimeoutError / OSError (3 attempts total).
        """
        @_connection_retry
        def _connect_with_retry():
            return self.connect()

        try:
            cur = _connect_with_retry()
            cur.execute("SELECT 1")
            return True
        except Exception:
            return False

    def get_schema(self, connection: duckdb.DuckDBPyConnection) -> dict[str, SchemaTable]:
        """Introspect the database schema via INFORMATION_SCHEMA."""
        schema: dict[str, SchemaTable] = {}
        cur = connection

        # 1. Get all base tables in the 'main' schema
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
        )
        tables = [row[0] for row in cur.fetchall()]

        for table in tables:
            # 2. Columns
            cur.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = ?",
                [table],
            )
            columns: list[ColumnInfo] = [
                ColumnInfo(
                    name=row[0],
                    type=row[1],
                    nullable=(str(row[2]).upper() == "YES"),
                )
                for row in cur.fetchall()
            ]

            # 3. Primary keys via INFORMATION_SCHEMA
            cur.execute(
                "SELECT kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "  AND tc.table_schema = kcu.table_schema "
                "WHERE tc.table_name = ? "
                "  AND tc.constraint_type = 'PRIMARY KEY' "
                "  AND tc.table_schema = 'main'",
                [table],
            )
            primary_keys: list[str] = [row[0] for row in cur.fetchall()]

            # 4. Foreign keys — try PRAGMA first (more reliable for SQLite files
            #    opened by DuckDB), fall back to INFORMATION_SCHEMA
            foreign_keys: list[FKInfo] = []
            try:
                cur.execute(f"PRAGMA foreign_key_list('{table}')")
                fk_rows = cur.fetchall()
                # PRAGMA foreign_key_list columns: id, seq, table, from, to, ...
                foreign_keys = [
                    FKInfo(
                        column=row[3],
                        references_table=row[2],
                        references_column=row[4],
                    )
                    for row in fk_rows
                ]
            except Exception:
                pass

            if not foreign_keys:
                try:
                    cur.execute(
                        "SELECT kcu.column_name, ccu.table_name, ccu.column_name "
                        "FROM information_schema.table_constraints tc "
                        "JOIN information_schema.key_column_usage kcu "
                        "  ON tc.constraint_name = kcu.constraint_name "
                        "JOIN information_schema.constraint_column_usage ccu "
                        "  ON tc.constraint_name = ccu.constraint_name "
                        "WHERE tc.table_name = ? "
                        "  AND tc.constraint_type = 'FOREIGN KEY' "
                        "  AND tc.table_schema = 'main'",
                        [table],
                    )
                    foreign_keys = [
                        FKInfo(
                            column=row[0],
                            references_table=row[1],
                            references_column=row[2],
                        )
                        for row in cur.fetchall()
                    ]
                except Exception:
                    foreign_keys = []

            # 5. Sample rows (max 2) — use f-string; parameterized identifiers
            #    are not supported in DuckDB for table names
            sample_rows: list[dict] = []
            try:
                cur.execute(f'SELECT * FROM "{table}" LIMIT 2')
                col_names = [d[0] for d in cur.description]
                sample_rows = rows_to_dicts(col_names, cur.fetchall())
            except Exception:
                sample_rows = []

            schema[table] = SchemaTable(
                columns=columns,
                primary_keys=primary_keys,
                foreign_keys=foreign_keys,
                sample_rows=sample_rows,
            )

        return schema

    def execute_query_limited(
        self, connection: duckdb.DuckDBPyConnection, sql: str, max_rows: int | None = None
    ) -> tuple[list[dict], bool]:
        """Execute SQL and return ``(rows, truncated)`` reading at most max_rows + 1 rows."""
        connection.execute(sql)
        return fetch_limited(connection, max_rows)

    def execute_query(
        self, connection: duckdb.DuckDBPyConnection, sql: str, max_rows: int | None = None
    ) -> list[dict]:
        """Execute SQL and return rows as list of dicts."""
        return self.execute_query_limited(connection, sql, max_rows)[0]

    def close(self, connection: duckdb.DuckDBPyConnection) -> None:
        """DuckDB cursors are lightweight — let GC handle them."""
        pass

    def dispose(self) -> None:
        """Close the parent connection (releases the file handle) and forget cursors."""
        with self._conn_lock:
            parent, self._parent_conn = self._parent_conn, None
            self._local = threading.local()
        if parent is not None:
            try:
                parent.close()
            except Exception:
                pass
