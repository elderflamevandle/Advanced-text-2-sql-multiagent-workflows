"""PostgreSQL connector using psycopg2 ThreadedConnectionPool (optional extra)."""
from __future__ import annotations

import logging

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from database.connectors.base import BaseConnector
from database.schema_utils import ColumnInfo, FKInfo, SchemaTable

logger = logging.getLogger(__name__)

RETRY_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)


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
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        database: str = "postgres",
        user: str = "postgres",
        password: str = "",
    ):
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._password = password
        self._pool = None

    @_connection_retry
    def connect(self) -> object:
        """Return a connection from the thread-safe pool.

        Raises ImportError (with install hint) if psycopg2 is not installed.
        """
        try:
            import psycopg2.pool  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "PostgreSQL support requires: pip install -e '.[postgresql]'"
            ) from exc

        if self._pool is None:
            self._pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                host=self._host,
                port=self._port,
                database=self._database,
                user=self._user,
                password=self._password,
            )
        return self._pool.getconn()

    def get_schema(self, connection: object) -> dict[str, SchemaTable]:
        """Introspect the public schema via INFORMATION_SCHEMA.

        Returns an empty dict if no tables are found or if the connection
        cursor returns no rows (e.g., when a mock is used in tests).
        """
        schema: dict[str, SchemaTable] = {}
        try:
            cur = connection.cursor()

            # Tables in the public schema
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
            tables = [row[0] for row in cur.fetchall()]

            for table in tables:
                # Columns
                cur.execute(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s",
                    (table,),
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
                    "WHERE tc.table_name = %s "
                    "  AND tc.constraint_type = 'PRIMARY KEY' "
                    "  AND tc.table_schema = 'public'",
                    (table,),
                )
                primary_keys: list[str] = [row[0] for row in cur.fetchall()]

                # Foreign keys
                cur.execute(
                    "SELECT kcu.column_name, ccu.table_name, ccu.column_name "
                    "FROM information_schema.referential_constraints rc "
                    "JOIN information_schema.key_column_usage kcu "
                    "  ON rc.constraint_name = kcu.constraint_name "
                    "JOIN information_schema.constraint_column_usage ccu "
                    "  ON rc.unique_constraint_name = ccu.constraint_name "
                    "WHERE kcu.table_name = %s AND kcu.table_schema = 'public'",
                    (table,),
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
                    cur.execute(f'SELECT * FROM "{table}" LIMIT 2')
                    col_names = [d[0] for d in cur.description]
                    sample_rows = [
                        dict(zip(col_names, row)) for row in cur.fetchall()
                    ]
                except Exception:
                    sample_rows = []

                schema[table] = SchemaTable(
                    columns=columns,
                    primary_keys=primary_keys,
                    foreign_keys=foreign_keys,
                    sample_rows=sample_rows,
                )

        except Exception as exc:
            logger.warning("PostgreSQL schema introspection partial failure: %s", exc)

        return schema

    def execute_query(self, connection: object, sql: str) -> list[dict]:
        """Execute SQL and return rows as list of dicts."""
        cur = connection.cursor()
        cur.execute(sql)
        col_names = [d[0] for d in cur.description]
        return [dict(zip(col_names, row)) for row in cur.fetchall()]

    def close(self, connection: object) -> None:
        """Return the connection back to the pool."""
        if self._pool is not None:
            try:
                self._pool.putconn(connection)
            except Exception:
                pass

    def test_connection(self) -> bool:
        """Return True if a test query succeeds; False on any error."""
        try:
            conn = self.connect()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            self.close(conn)
            return True
        except Exception:
            return False
