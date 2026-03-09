"""Thread-safe DuckDB connector using cursor-per-thread pattern."""
from __future__ import annotations

import threading

import duckdb
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from database.connectors.base import BaseConnector
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

    Uses a single parent connection with per-thread cursors via threading.local
    to avoid cursor contention across threads. The parent connection is opened
    lazily on first use.
    """

    def __init__(self, db_path: str = ":memory:"):
        self._db_path = db_path
        self._parent_conn: duckdb.DuckDBPyConnection | None = None
        self._local = threading.local()
        self._conn_lock = threading.Lock()

    # ------------------------------------------------------------------
    # BaseConnector interface
    # ------------------------------------------------------------------

    def connect(self) -> duckdb.DuckDBPyConnection:
        """Return a thread-local cursor copy of the parent connection."""
        if self._parent_conn is None:
            with self._conn_lock:
                if self._parent_conn is None:
                    self._parent_conn = duckdb.connect(self._db_path)
        if not hasattr(self._local, "cursor") or self._local.cursor is None:
            self._local.cursor = self._parent_conn.cursor()
        return self._local.cursor

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
                sample_rows = [dict(zip(col_names, row)) for row in cur.fetchall()]
            except Exception:
                sample_rows = []

            schema[table] = SchemaTable(
                columns=columns,
                primary_keys=primary_keys,
                foreign_keys=foreign_keys,
                sample_rows=sample_rows,
            )

        return schema

    def execute_query(self, connection: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
        """Execute SQL and return rows as list of dicts."""
        connection.execute(sql)
        col_names = [d[0] for d in connection.description]
        return [dict(zip(col_names, row)) for row in connection.fetchall()]

    def close(self, connection: duckdb.DuckDBPyConnection) -> None:
        """DuckDB cursors are lightweight — let GC handle them."""
        pass
