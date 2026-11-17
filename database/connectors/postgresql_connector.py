"""PostgreSQL connector using psycopg2 ThreadedConnectionPool (optional extra)."""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Sequence

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from database.connectors.base import BaseConnector, fetch_limited, rows_to_dicts
from database.schema_utils import ColumnInfo, FKInfo, SchemaTable

logger = logging.getLogger(__name__)

RETRY_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _connection_retry(func):
    """Tenacity retry decorator: 3 attempts, exponential backoff 1s/2s/4s."""
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type(RETRY_EXCEPTIONS),
        reraise=True,
    )(func)


class PostgreSQLConnector(BaseConnector):
    """PostgreSQL connector using psycopg2 ThreadedConnectionPool.

    psycopg2 is an optional extra — the import lives inside connect() so the
    module can be imported on systems that do not have psycopg2 installed.

    Every pooled connection is a read-only autocommit session with a
    server-side ``statement_timeout``. Connections handed out by ``connect()``
    / ``acquire()`` must go back through ``release()`` (or ``close()``); use
    ``with connector.connection() as conn:`` to make that automatic.

    ``schema`` is the schema (or list of schemas) to introspect and to put on
    the session ``search_path``; it defaults to ``"public"``.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        database: str = "postgres",
        user: str = "postgres",
        password: str = "",
        schema: str | Sequence[str] = "public",
        statement_timeout_ms: int | None = 60_000,
        max_connections: int = 10,
    ):
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._password = password
        if isinstance(schema, str):
            schemas = [part.strip() for part in schema.split(",") if part.strip()]
        else:
            schemas = [str(part).strip() for part in schema]
        schemas = schemas or ["public"]
        for name in schemas:
            if not _IDENTIFIER.match(name):
                raise ValueError(f"Invalid PostgreSQL schema name: {name!r}")
        self._schemas: list[str] = schemas
        self._statement_timeout_ms = statement_timeout_ms
        self._max_connections = max_connections
        self._pool = None
        self._pool_lock = threading.Lock()

    def _session_options(self) -> str:
        """libpq ``options`` applied to every pooled connection at startup."""
        opts = ["-c default_transaction_read_only=on"]
        if self._statement_timeout_ms:
            opts.append(f"-c statement_timeout={int(self._statement_timeout_ms)}")
        opts.append(f"-c search_path={','.join(self._schemas)}")
        return " ".join(opts)

    @_connection_retry
    def connect(self) -> object:
        """Return a read-only connection from the thread-safe pool.

        Raises ImportError (with install hint) if psycopg2 is not installed.
        """
        try:
            import psycopg2.pool  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "PostgreSQL support requires: pip install -e '.[postgresql]'"
            ) from exc

        with self._pool_lock:
            if self._pool is None:
                self._pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=self._max_connections,
                    host=self._host,
                    port=self._port,
                    database=self._database,
                    user=self._user,
                    password=self._password,
                    options=self._session_options(),
                )
            pool = self._pool
        conn = pool.getconn()
        try:
            # Autocommit: no transaction is left open (or aborted) between
            # queries. readonly: the server rejects writes for this session.
            conn.set_session(readonly=True, autocommit=True)
        except Exception:
            self._discard(conn)
            raise
        return conn

    def _discard(self, connection: object) -> None:
        """Return a connection to the pool and have the pool close it."""
        if self._pool is None:
            return
        try:
            self._pool.putconn(connection, close=True)
        except Exception:
            pass

    def release(self, connection: object, error: bool = False) -> None:
        """Return the connection to the pool, clean or discarded.

        After an error the connection is rolled back first; if that fails, or
        the connection is already closed, the pool drops it instead of reusing it.
        """
        if self._pool is None:
            return
        broken = False
        if error:
            try:
                connection.rollback()
            except Exception:
                broken = True
        if getattr(connection, "closed", 0) not in (0, False, None):
            broken = True
        try:
            if broken:
                self._pool.putconn(connection, close=True)
            else:
                self._pool.putconn(connection)
        except Exception as exc:
            logger.warning("PostgreSQL: could not return connection to pool: %s", exc)

    def cancel(self, connection: object) -> None:
        """Ask the server to cancel the statement running on ``connection``."""
        connection.cancel()

    def get_schema(self, connection: object) -> dict[str, SchemaTable]:
        """Introspect the configured schema(s) via INFORMATION_SCHEMA.

        Returns an empty dict if no tables are found or if the connection
        cursor returns no rows (e.g., when a mock is used in tests).
        """
        schema: dict[str, SchemaTable] = {}
        try:
            cur = connection.cursor()
            for schema_name in self._schemas:
                self._read_schema(cur, schema_name, schema)
        except Exception as exc:
            logger.warning("PostgreSQL schema introspection partial failure: %s", exc)

        return schema

    def _read_schema(self, cur: object, schema_name: str, schema: dict[str, SchemaTable]) -> None:
        """Add the tables of one database schema to ``schema`` (keyed by table name).

        A table whose name was already taken by an earlier schema is keyed as
        ``schema_name.table`` so nothing is overwritten.
        """
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE'",
            (schema_name,),
        )
        tables = [row[0] for row in cur.fetchall()]

        for table in tables:
            # Columns
            cur.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s "
                "ORDER BY ordinal_position",
                (schema_name, table),
            )
            columns: list[ColumnInfo] = [
                ColumnInfo(name=r[0], type=r[1], nullable=(r[2] == "YES"))
                for r in cur.fetchall()
            ]

            # Primary keys
            cur.execute(
                "SELECT kcu.column_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "  AND tc.table_schema = kcu.table_schema "
                "WHERE tc.table_name = %s "
                "  AND tc.constraint_type = 'PRIMARY KEY' "
                "  AND tc.table_schema = %s",
                (table, schema_name),
            )
            primary_keys: list[str] = [row[0] for row in cur.fetchall()]

            # Foreign keys
            cur.execute(
                "SELECT kcu.column_name, ccu.table_name, ccu.column_name "
                "FROM information_schema.referential_constraints rc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON rc.constraint_name = kcu.constraint_name "
                "  AND rc.constraint_schema = kcu.constraint_schema "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON rc.unique_constraint_name = ccu.constraint_name "
                "  AND rc.unique_constraint_schema = ccu.constraint_schema "
                "WHERE kcu.table_name = %s AND kcu.table_schema = %s",
                (table, schema_name),
            )
            foreign_keys: list[FKInfo] = [
                FKInfo(
                    column=r[0],
                    references_table=r[1],
                    references_column=r[2],
                )
                for r in cur.fetchall()
            ]

            # Sample rows (max 2)
            sample_rows: list[dict] = []
            try:
                quoted = ".".join(
                    '"' + part.replace('"', '""') + '"' for part in (schema_name, table)
                )
                cur.execute(f"SELECT * FROM {quoted} LIMIT 2")
                col_names = [d[0] for d in cur.description]
                sample_rows = rows_to_dicts(col_names, cur.fetchall())
            except Exception:
                sample_rows = []

            key = table if table not in schema else f"{schema_name}.{table}"
            schema[key] = SchemaTable(
                columns=columns,
                primary_keys=primary_keys,
                foreign_keys=foreign_keys,
                sample_rows=sample_rows,
            )

    def execute_query_limited(
        self, connection: object, sql: str, max_rows: int | None = None
    ) -> tuple[list[dict], bool]:
        """Execute SQL and return ``(rows, truncated)`` with at most max_rows rows."""
        cur = connection.cursor()
        try:
            cur.execute(sql)
            return fetch_limited(cur, max_rows)
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def execute_query(
        self, connection: object, sql: str, max_rows: int | None = None
    ) -> list[dict]:
        """Execute SQL and return rows as list of dicts."""
        return self.execute_query_limited(connection, sql, max_rows)[0]

    def close(self, connection: object) -> None:
        """Return the connection back to the pool."""
        self.release(connection)

    def test_connection(self) -> bool:
        """Return True if a test query succeeds; False on any error."""
        try:
            with self.connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchall()
            return True
        except Exception:
            return False

    def dispose(self) -> None:
        """Close every connection in the pool. A later connect() builds a new pool."""
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            try:
                pool.closeall()
            except Exception as exc:  # noqa: BLE001
                logger.warning("PostgreSQL: error while closing pool: %s", type(exc).__name__)
