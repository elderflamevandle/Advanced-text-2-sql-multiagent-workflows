"""Mapping between the project's db_type names and sqlglot dialect names."""
from __future__ import annotations

_SQLGLOT_DIALECTS: dict[str, str] = {
    "sqlite": "sqlite",
    "duckdb": "duckdb",
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
}


def to_sqlglot_dialect(db_type: str | None) -> str | None:
    """Return the sqlglot dialect for a db_type, or None for unknown (generic SQL)."""
    if not db_type:
        return None
    return _SQLGLOT_DIALECTS.get(db_type.lower())
