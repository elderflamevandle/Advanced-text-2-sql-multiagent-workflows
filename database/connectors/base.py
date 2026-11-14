"""Abstract base class for all database dialect connectors.

Besides the abstract contract, this module provides driver-independent helpers
shared by every connector: bounded fetching, value normalisation for rows that
end up in LangGraph state, and the ``QueryTimeoutError`` raised on cancellation.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from database.schema_utils import SchemaTable

_INT64_MIN = -(2**63)
_UINT64_MAX = 2**64 - 1


class QueryTimeoutError(TimeoutError):
    """A query exceeded its time budget and was cancelled."""


def to_plain_value(value: Any) -> Any:
    """Convert a driver value into something JSON/msgpack can store.

    Decimal -> float, date/time/datetime -> ISO string, timedelta/UUID -> str,
    bytes -> ``"<binary N bytes>"`` placeholder, containers are converted
    recursively. Integers outside the 64-bit range become strings.
    """
    if value is None or isinstance(value, (str, float, bool)):
        return value
    if isinstance(value, int):
        return value if _INT64_MIN <= value <= _UINT64_MAX else str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (_dt.timedelta, uuid.UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary {len(value)} bytes>"
    if isinstance(value, dict):
        return {str(k): to_plain_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_plain_value(v) for v in value]
    return str(value)


def rows_to_dicts(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[dict]:
    """Zip column names with row tuples, normalising every value."""
    return [
        {col: to_plain_value(val) for col, val in zip(columns, row)}
        for row in rows
    ]


def fetch_limited(cursor: Any, max_rows: int | None) -> tuple[list[dict], bool]:
    """Read at most ``max_rows`` rows from an executed DB-API cursor.

    One extra row is requested so truncation can be reported without reading
    the whole result. Returns ``(rows, truncated)``. Statements that produce no
    result set return ``([], False)``.
    """
    description = cursor.description
    if not description:
        return [], False
    columns = [d[0] for d in description]
    if max_rows is None:
        return rows_to_dicts(columns, cursor.fetchall()), False
    raw = cursor.fetchmany(max_rows + 1)
    truncated = len(raw) > max_rows
    return rows_to_dicts(columns, raw[:max_rows]), truncated


class BaseConnector(ABC):
    """Contract all dialect connectors must implement.

    No database driver is imported here. All driver imports live inside the
    concrete connector subclasses so that optional extras (PostgreSQL, MySQL)
    do not cause ImportError on systems without those packages.

    Subclasses must implement the five abstract methods. ``acquire``,
    ``release``, ``connection``, ``cancel`` and ``execute_query_limited`` have
    working defaults so older subclasses keep functioning.
    """

    @abstractmethod
    def connect(self) -> object:
        """Return a live connection or cursor ready for queries."""

    @abstractmethod
    def get_schema(self, connection: object) -> dict[str, SchemaTable]:
        """Fetch schema: tables -> {columns, primary_keys, foreign_keys, sample_rows}."""

    @abstractmethod
    def execute_query(self, connection: object, sql: str) -> list[dict]:
        """Execute a read-only SQL query. Return rows as list of dicts."""

    @abstractmethod
    def close(self, connection: object) -> None:
        """Release/close the connection."""

    @abstractmethod
    def test_connection(self) -> bool:
        """Return True if a connection can be established, False otherwise."""

    # ------------------------------------------------------------------
    # Non-abstract lifecycle hooks
    # ------------------------------------------------------------------

    def acquire(self) -> object:
        """Check out a connection for one unit of work. Pair with release()."""
        return self.connect()

    def release(self, connection: object, error: bool = False) -> None:
        """Give back a connection obtained from acquire().

        ``error`` is True when the unit of work raised. The default does
        nothing, which suits connectors whose connect() returns a shared
        long-lived connection; pooled connectors override it.
        """

    @contextmanager
    def connection(self) -> Iterator[object]:
        """Context manager: acquire a connection and always release it."""
        conn = self.acquire()
        try:
            yield conn
        except BaseException:
            self.release(conn, error=True)
            raise
        else:
            self.release(conn, error=False)

    def dispose(self) -> None:
        """Close every pooled or shared connection held by this connector.

        Safe to call more than once. The connector stays usable: the next
        acquire()/connect() lazily opens new resources. The default is a no-op
        for connectors that hold nothing between units of work.
        """

    def cancel(self, connection: object) -> None:
        """Abort the statement currently running on ``connection``.

        Called from a different thread than the one executing the query.
        The default is a no-op for drivers without a cancel facility.
        """

    def execute_query_limited(
        self, connection: object, sql: str, max_rows: int | None = None
    ) -> tuple[list[dict], bool]:
        """Execute ``sql`` and return ``(rows, truncated)`` with at most ``max_rows`` rows.

        The default runs execute_query() and slices the result; the bundled
        connectors override it to avoid reading more than ``max_rows + 1`` rows.
        """
        rows = self.execute_query(connection, sql)
        if max_rows is not None and len(rows) > max_rows:
            return rows[:max_rows], True
        return rows, False
