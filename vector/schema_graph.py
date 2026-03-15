"""FK adjacency graph and JOIN hint generation for schema retrieval (VECTOR-003)."""
from __future__ import annotations

from database.schema_utils import FKInfo, SchemaTable


class SchemaGraph:
    """Builds an FK adjacency graph and provides 1-hop expansion and JOIN hints."""

    def __init__(self, schema: dict[str, SchemaTable]) -> None:
        # _adj[table] = list of (fk_column, references_table, references_column)
        self._adj: dict[str, list[tuple[str, str, str]]] = {}
        for table_name, table_data in schema.items():
            fks = table_data.get("foreign_keys", [])
            if fks:
                self._adj[table_name] = [
                    (fk["column"], fk["references_table"], fk["references_column"])
                    for fk in fks
                ]

    def expand_tables(self, seed_tables: list[str]) -> list[str]:
        """Return seed tables plus their 1-hop FK neighbors (forward direction only)."""
        expanded: set[str] = set(seed_tables)
        for table in seed_tables:
            for _, ref_table, _ in self._adj.get(table, []):
                expanded.add(ref_table)
        return sorted(expanded)

    def generate_join_hints(self, tables: list[str]) -> list[dict]:
        """Generate JOIN hint dicts for all outgoing FKs of the given tables."""
        hints: list[dict] = []
        for table in tables:
            for fk_col, ref_table, ref_col in self._adj.get(table, []):
                hints.append({
                    "from": table,
                    "to": ref_table,
                    "on": f"{table}.{fk_col} = {ref_table}.{ref_col}",
                    "type": "INNER",
                })
        return hints
