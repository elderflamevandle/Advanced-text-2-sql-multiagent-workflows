"""MySQL connector using mysql.connector.pooling.MySQLConnectionPool (optional extra)."""
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


class MySQLConnector(BaseConnector):
    """MySQL connector using mysql.connector.pooling.MySQLConnectionPool.

    mysql-connector-python is an optional extra — the import lives inside
    connect() so the module can be imported on systems without the driver.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 3306,
        database: str = "mysql",
        user: str = "root",
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
        """Return a connection from the MySQL connection pool.

        Raises ImportError (with install hint) if mysql-connector-python is not installed.
        """
        try:
            import mysql.connector.pooling  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "MySQL support requires: pip install -e '.[mysql]'"
            ) from exc

        if self._pool is None:
            self._pool = mysql.connector.pooling.MySQLConnectionPool(
                pool_name="text2sql",
                pool_size=5,
                host=self._host,
                port=self._port,
                database=self._database,
                user=self._user,
                password=self._password,
            )
        return self._pool.get_connection()

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
            logger.warning("MySQL schema introspection partial failure: %s", exc)

        return schema

    def execute_query(self, connection: object, sql: str) -> list[dict]:
        """Execute SQL and return rows as list of dicts."""
        cur = connection.cursor()
        cur.execute(sql)
        col_names = [d[0] for d in cur.description]
        return [dict(zip(col_names, row)) for row in cur.fetchall()]

    def close(self, connection: object) -> None:
        """Return connection to the pool by closing it."""
        try:
            connection.close()
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
