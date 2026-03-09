"""SQLite connector with PRAGMA-based schema introspection and FK enforcement."""
from __future__ import annotations

import sqlite3

from database.connectors.base import BaseConnector
from database.schema_utils import ColumnInfo, FKInfo, SchemaTable


class SQLiteConnector(BaseConnector):
    """SQLite connector.

    Uses Python's built-in sqlite3 module. PRAGMA foreign_keys = ON is
    enforced on every connection. Internal sqlite_* tables are filtered out
    of the schema.
    """

    def __init__(self, db_path: str = ":memory:"):
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # BaseConnector interface
    # ------------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Open (or return existing) SQLite connection with FK enforcement."""
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.commit()
        return self._conn

    def test_connection(self) -> bool:
        """Return True if a test query succeeds."""
        try:
            conn = self.connect()
            conn.execute("SELECT 1")
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

    def execute_query(self, connection: sqlite3.Connection, sql: str) -> list[dict]:
        """Execute SQL and return rows as list of dicts."""
        cur = connection.cursor()
        cur.execute(sql)
        col_names = [d[0] for d in cur.description]
        return [dict(zip(col_names, row)) for row in cur.fetchall()]

    def close(self, connection: sqlite3.Connection) -> None:
        """Close the SQLite connection."""
        try:
            connection.close()
        except Exception:
            pass
        self._conn = None
