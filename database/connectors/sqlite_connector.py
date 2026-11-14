"""SQLite connector: read-only connections and PRAGMA-based schema introspection."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from database.connectors.base import BaseConnector, fetch_limited, rows_to_dicts
from database.schema_utils import ColumnInfo, FKInfo, SchemaTable

_MEMORY_PATHS = {"", ":memory:"}


def _read_only_uri(db_path: str) -> str:
    """Build a ``file:`` URI that opens ``db_path`` read-only.

    Works for relative paths, absolute POSIX paths and Windows drive paths
    (``C:\\data\\x.db`` -> ``file:///C:/data/x.db?mode=ro``). A path that is
    already a ``file:`` URI is kept, with ``mode=ro`` added when no mode is set.
    """
    if db_path.startswith("file:"):
        if "mode=" in db_path:
            return db_path
        return f"{db_path}{'&' if '?' in db_path else '?'}mode=ro"
    return f"{Path(db_path).resolve().as_uri()}?mode=ro"


class SQLiteConnector(BaseConnector):
    """SQLite connector.

    Uses Python's built-in sqlite3 module. File databases are opened read-only
    (``mode=ro``) and every connection runs ``PRAGMA query_only = ON``, so
    write statements fail inside SQLite itself. Internal sqlite_* tables are
    filtered out of the schema.

    ``connect()`` returns one shared long-lived connection. ``acquire()`` opens
    a dedicated connection per unit of work for file databases, so cancelling a
    timed-out query never interrupts someone else's. In-memory databases exist
    only inside their connection, so they always use the shared one.
    """

    def __init__(self, db_path: str = ":memory:"):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    @property
    def _is_memory(self) -> bool:
        return self._db_path in _MEMORY_PATHS or "mode=memory" in self._db_path

    def _open(self) -> sqlite3.Connection:
        if self._db_path in _MEMORY_PATHS:
            conn = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            conn = sqlite3.connect(
                _read_only_uri(self._db_path), uri=True, check_same_thread=False
            )
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA query_only = ON")
        except Exception:
            conn.close()
            raise
        return conn

    # ------------------------------------------------------------------
    # BaseConnector interface
    # ------------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Open (or return the existing) shared read-only SQLite connection."""
        with self._lock:
            if self._conn is None:
                self._conn = self._open()
            return self._conn

    def acquire(self) -> sqlite3.Connection:
        """Return a dedicated connection (file DB) or the shared one (in-memory)."""
        if self._is_memory:
            return self.connect()
        return self._open()

    def release(self, connection: sqlite3.Connection, error: bool = False) -> None:
        """Close a dedicated connection; the shared one stays open."""
        if connection is self._conn:
            return
        try:
            connection.close()
        except Exception:
            pass

    def cancel(self, connection: sqlite3.Connection) -> None:
        """Interrupt the statement running on ``connection`` (thread-safe)."""
        connection.interrupt()

    def test_connection(self) -> bool:
        """Return True if a test query succeeds."""
        try:
            with self.connection() as conn:
                conn.execute("SELECT 1").fetchall()
            return True
        except Exception:
            return False

    def get_schema(self, connection: sqlite3.Connection) -> dict[str, SchemaTable]:
        """Introspect the SQLite schema via PRAGMA statements."""
        schema: dict[str, SchemaTable] = {}
        cur = connection.cursor()

        # 1. Get all user-defined tables (exclude sqlite_* internal tables)
        cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = [row[0] for row in cur.fetchall()]

        for table in tables:
            # 2. Columns and primary keys via PRAGMA table_info
            # Row format: (cid, name, type, notnull, dflt_value, pk)
            #   pk > 0 indicates this column is part of the primary key
            cur.execute(f"PRAGMA table_info('{table}')")
            cols_raw = cur.fetchall()
            columns: list[ColumnInfo] = [
                ColumnInfo(
                    name=row[1],
                    type=row[2] if row[2] else "TEXT",
                    nullable=(row[3] == 0),  # notnull=0 means nullable
                )
                for row in cols_raw
            ]
            primary_keys: list[str] = [row[1] for row in cols_raw if row[5] > 0]

            # 3. Foreign keys via PRAGMA foreign_key_list
            # Row format: (id, seq, table, from, to, on_update, on_delete, match)
            cur.execute(f"PRAGMA foreign_key_list('{table}')")
            fks_raw = cur.fetchall()
            foreign_keys: list[FKInfo] = [
                FKInfo(
                    column=row[3],
                    references_table=row[2],
                    references_column=row[4],
                )
                for row in fks_raw
            ]

            # 4. Sample rows (max 2)
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
        self, connection: sqlite3.Connection, sql: str, max_rows: int | None = None
    ) -> tuple[list[dict], bool]:
        """Execute SQL and return ``(rows, truncated)`` reading at most max_rows + 1 rows."""
        cur = connection.cursor()
        try:
            cur.execute(sql)
            return fetch_limited(cur, max_rows)
        finally:
            cur.close()

    def execute_query(
        self, connection: sqlite3.Connection, sql: str, max_rows: int | None = None
    ) -> list[dict]:
        """Execute SQL and return rows as list of dicts."""
        return self.execute_query_limited(connection, sql, max_rows)[0]

    def close(self, connection: sqlite3.Connection) -> None:
        """Close the SQLite connection."""
        try:
            connection.close()
        except Exception:
            pass
        if connection is self._conn:
            self._conn = None
