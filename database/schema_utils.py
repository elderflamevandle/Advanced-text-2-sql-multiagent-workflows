"""Schema utility types and builder shared by all connectors."""
from __future__ import annotations

from typing import TypedDict


class ColumnInfo(TypedDict):
    name: str
    type: str
    nullable: bool


class FKInfo(TypedDict):
    column: str
    references_table: str
    references_column: str


class SchemaTable(TypedDict):
    columns: list[ColumnInfo]
    primary_keys: list[str]
    foreign_keys: list[FKInfo]
    sample_rows: list[dict]
