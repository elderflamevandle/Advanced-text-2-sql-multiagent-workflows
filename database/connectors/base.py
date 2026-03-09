"""Abstract base class for all database dialect connectors."""
from __future__ import annotations

from abc import ABC, abstractmethod

from database.schema_utils import SchemaTable


class BaseConnector(ABC):
    """Contract all dialect connectors must implement.

    No database driver is imported here. All driver imports live inside the
    concrete connector subclasses so that optional extras (PostgreSQL, MySQL)
    do not cause ImportError on systems without those packages.
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
