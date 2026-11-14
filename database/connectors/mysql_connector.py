"""MySQL connector using mysql.connector.pooling.MySQLConnectionPool (optional extra)."""
from __future__ import annotations

import logging
import threading

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from database.connectors.base import BaseConnector, fetch_limited, rows_to_dicts
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


class MySQLConnector(BaseConnector):
    """MySQL connector using mysql.connector.pooling.MySQLConnectionPool.

    mysql-connector-python is an optional extra — the import lives inside
    connect() so the module can be imported on systems without the driver.

    Each connection handed out is switched to ``SESSION TRANSACTION READ ONLY``
    and given a server-side execution time limit (``max_execution_time`` on
    MySQL, ``max_statement_time`` on MariaDB) where the server supports one.
    The pool resets session state on checkout, so this is re-applied each time.
    Return connections with ``release()`` / ``close()`` or use
    ``with connector.connection() as conn:``.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 3306,
        database: str = "mysql",
        user: str = "root",
        password: str = "",
        max_execution_time_ms: int | None = 60_000,
        pool_size: int = 8,
    ):
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._password = password
        self._max_execution_time_ms = max_execution_time_ms
        self._pool_size = pool_size
        self._pool = None
        self._pool_lock = threading.Lock()

    @_connection_retry
    def connect(self) -> object:
        """Return a read-only connection from the MySQL connection pool.

        Raises ImportError (with install hint) if mysql-connector-python is not installed.
        """
        try:
            import mysql.connector.pooling  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "MySQL support requires: pip install -e '.[mysql]'"
            ) from exc

        with self._pool_lock:
            if self._pool is None:
                self._pool = mysql.connector.pooling.MySQLConnectionPool(
                    pool_name="text2sql",
                    pool_size=self._pool_size,
                    host=self._host,
                    port=self._port,
                    database=self._database,
                    user=self._user,
                    password=self._password,
                )
            pool = self._pool
        conn = pool.get_connection()
        try:
            self._prepare_session(conn)
        except Exception:
            self.release(conn, error=True)
            raise
        return conn

    def _prepare_session(self, connection: object) -> None:
        """Make the session read-only and set the server-side time limit."""
        cur = connection.cursor()
        try:
            # Required: if this fails the connection is not handed out.
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            if self._max_execution_time_ms:
                ms = int(self._max_execution_time_ms)
                try:
                    cur.execute(f"SET SESSION max_execution_time = {ms}")  # MySQL >= 5.7.8
                except Exception:
                    try:
                        cur.execute(f"SET SESSION max_statement_time = {ms / 1000:.3f}")  # MariaDB
                    except Exception:
                        logger.debug("MySQL: no server-side statement time limit available")
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def release(self, connection: object, error: bool = False) -> None:
        """Roll back any open transaction and return the connection to the pool."""
        try:
            connection.rollback()
        except Exception:
            pass
        try:
            connection.close()  # pooled connection: close() hands it back to the pool
        except Exception as exc:
            logger.warning("MySQL: could not return connection to pool: %s", exc)

    def cancel(self, connection: object) -> None:
        """Kill the statement running on ``connection`` from a side connection."""
        import mysql.connector  # noqa: PLC0415

        thread_id = int(connection.connection_id)
        side = mysql.connector.connect(
            host=self._host,
            port=self._port,
            database=self._database,
            user=self._user,
            password=self._password,
        )
        try:
            cur = side.cursor()
            cur.execute(f"KILL QUERY {thread_id}")
            cur.close()
        finally:
            side.close()

    def get_schema(self, connection: object) -> dict[str, SchemaTable]:
        """Introspect the database via INFORMATION_SCHEMA.

        Returns an empty dict if no tables are found or if the connection
        cursor returns no rows (e.g., when a mock is used in tests).
        """
        schema: dict[str, SchemaTable] = {}
        try:
            cur = connection.cursor()

            # Tables in the current database
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"
            )
            tables = [row[0] for row in cur.fetchall()]

            for table in tables:
                # Columns
                cur.execute(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = %s",
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
                    "  AND tc.table_schema = kcu.table_schema "
                    "  AND tc.table_name = kcu.table_name "
                    "WHERE tc.table_schema = DATABASE() "
                    "  AND tc.table_name = %s "
                    "  AND tc.constraint_type = 'PRIMARY KEY'",
                    (table,),
                )
                primary_keys: list[str] = [row[0] for row in cur.fetchall()]

                # Foreign keys
                cur.execute(
                    "SELECT kcu.column_name, kcu.referenced_table_name, "
                    "  kcu.referenced_column_name "
                    "FROM information_schema.key_column_usage kcu "
                    "JOIN information_schema.table_constraints tc "
                    "  ON kcu.constraint_name = tc.constraint_name "
                    "  AND kcu.table_schema = tc.table_schema "
                    "  AND kcu.table_name = tc.table_name "
                    "WHERE kcu.table_schema = DATABASE() "
                    "  AND kcu.table_name = %s "
                    "  AND tc.constraint_type = 'FOREIGN KEY'",
                    (table,),
                )
                foreign_keys: list[FKInfo] = [
                    FKInfo(
                        column=r[0],
                        references_table=r[1],
                        references_column=r[2],
                    )
                    for r in cur.fetchall()
                    if r[1] is not None  # skip rows with NULL references
                ]

                # Sample rows (max 2)
                sample_rows: list[dict] = []
                try:
                    cur.execute(f"SELECT * FROM `{table}` LIMIT 2")
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

        except Exception as exc:
            logger.warning("MySQL schema introspection partial failure: %s", exc)

        return schema

    def execute_query_limited(
        self, connection: object, sql: str, max_rows: int | None = None
    ) -> tuple[list[dict], bool]:
        """Execute SQL and return ``(rows, truncated)`` with at most max_rows rows."""
        cur = connection.cursor()
        try:
            cur.execute(sql)
            rows, truncated = fetch_limited(cur, max_rows)
            if truncated:
                # Unread rows would make the next command on this connection fail.
                try:
                    connection.consume_results()
                except Exception:
                    pass
            return rows, truncated
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
        """Return connection to the pool."""
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
        """Close every idle connection in the pool. A later connect() builds a new pool."""
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is None:
            return
        closer = getattr(pool, "_remove_connections", None)
        if closer is None:
            return
        try:
            closer()
        except Exception as exc:  # noqa: BLE001
            logger.warning("MySQL: error while closing pool: %s", type(exc).__name__)
