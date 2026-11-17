"""Render the selected part of a catalog as prompt text, and as the legacy schema dict."""
from __future__ import annotations

from catalog.models import Catalog, CatalogColumn, CatalogTable

MAX_ENUM_VALUES = 12
MAX_SAMPLES = 3
MAX_VALUE_CHARS = 40
MAX_DESCRIPTION_CHARS = 240
MAX_VALUE_HINTS = 20


def _clip(text: object, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _value(text: object) -> str:
    out = _clip(text, MAX_VALUE_CHARS)
    return f"'{out}'" if ("," in out or out != out.strip() or not out) else out


def _fk_targets(catalog: Catalog) -> dict[tuple[str, str], tuple[str, bool]]:
    """(table, column) -> ("t.c", inferred). Declared relationships win over inferred."""
    targets: dict[tuple[str, str], tuple[str, bool]] = {}
    for rel in catalog.relationships:
        key = (rel.from_table, rel.from_column)
        inferred = rel.kind == "inferred"
        if key not in targets or (targets[key][1] and not inferred):
            targets[key] = (f"{rel.to_table}.{rel.to_column}", inferred)
    return targets


def _is_key(table: CatalogTable, col: CatalogColumn, fks: dict) -> bool:
    return (col.is_primary_key or col.is_foreign_key or col.name in table.primary_keys
            or (table.name, col.name) in fks)


def _column_line(table: CatalogTable, col: CatalogColumn, fks: dict, *, samples: bool,
                 descriptions: bool) -> str:
    parts = [col.name]
    if col.data_type:
        parts.append(col.data_type)
    if col.is_primary_key or col.name in table.primary_keys:
        parts.append("[PK]")
    target = fks.get((table.name, col.name))
    if target:
        parts.append(f"[FK -> {target[0]}{' (inferred)' if target[1] else ''}]")
    if col.nullable and not (col.is_primary_key or col.name in table.primary_keys):
        parts.append("[nullable]")
    if col.is_pii:
        parts.append("[sensitive]")
    line = "  " + " ".join(parts)
    extras: list[str] = []
    if descriptions and col.description:
        extras.append(_clip(col.description, MAX_DESCRIPTION_CHARS))
    if not col.is_pii:                       # PII values never reach a prompt
        if col.enum_values:
            shown = [_value(v) for v in col.enum_values[:MAX_ENUM_VALUES]]
            more = len(col.enum_values) - len(shown)
            extras.append("values: " + ", ".join(shown) + (f", … +{more} more" if more > 0 else ""))
        elif samples and col.sample_values:
            extras.append("e.g. " + ", ".join(_value(v) for v in col.sample_values[:MAX_SAMPLES]))
    return line + (" -- " + "; ".join(extras) if extras else "")


def _table_block(table: CatalogTable, fks: dict, *, samples: bool, descriptions: bool,
                 keys_only: bool = False, table_description: bool = True) -> str:
    head = f"Table {table.name}"
    if table.row_count is not None:
        head += f" ({table.row_count:,} rows)"
    if table_description and table.description:
        head += f": {_clip(table.description, MAX_DESCRIPTION_CHARS)}"
    lines = [head]
    hidden = 0
    for col in table.columns:
        if keys_only and not _is_key(table, col, fks):
            hidden += 1
            continue
        lines.append(_column_line(table, col, fks, samples=samples, descriptions=descriptions))
    if hidden:
        lines.append(f"  ... {hidden} more column{'s' if hidden != 1 else ''}")
    return "\n".join(lines)


def _tail(join_hints, value_hints) -> str:
    sections: list[str] = []
    seen: set[str] = set()
    joins: list[str] = []
    for hint in join_hints or []:
        on = hint.get("on")
        if not on or on in seen:
            continue
        seen.add(on)
        joins.append(f"  {on}" + (" (inferred)" if hint.get("kind") == "inferred" else ""))
    if joins:
        sections.append("Joins:\n" + "\n".join(joins))
    values: list[str] = []
    for hint in (value_hints or [])[:MAX_VALUE_HINTS]:
        literal = str(hint.get("value", "")).replace("'", "''")
        values.append(f"  {hint.get('table')}.{hint.get('column')} = '{literal}'")
    if values:
        sections.append("Value hints (exact stored values):\n" + "\n".join(values))
    return "\n\n".join(sections)


def render_context(catalog: Catalog, tables: list[str], join_hints, value_hints, *,
                   max_chars: int = 24000) -> str:
    """Compact text describing ``tables`` for an LLM prompt.

    ``tables`` is taken to be ordered most relevant first. When the text exceeds ``max_chars``
    detail is removed in this order: sample values, column descriptions, non-key columns of
    the least relevant tables (replaced by "... N more columns"), table descriptions, and as a
    last resort whole tables from the end.
    """
    present = [catalog.tables[t] for t in dict.fromkeys(tables) if t in catalog.tables]
    fks = _fk_targets(catalog)
    tail = _tail(join_hints, value_hints)

    def assemble(blocks: list[str], note: str = "") -> str:
        parts = list(blocks)
        if note:
            parts.append(note)
        if tail:
            parts.append(tail)
        return "\n\n".join(parts)

    text = ""
    for samples, descriptions in ((True, True), (False, True), (False, False)):
        blocks = [_table_block(t, fks, samples=samples, descriptions=descriptions)
                  for t in present]
        text = assemble(blocks)
        if len(text) <= max_chars:
            return text

    # collapse the least relevant tables to their key columns, one at a time
    opts = {"samples": False, "descriptions": False}
    total = len(text)
    for i in range(len(present) - 1, -1, -1):
        small = _table_block(present[i], fks, keys_only=True, **opts)
        total -= len(blocks[i]) - len(small)
        blocks[i] = small
        if total <= max_chars:
            return assemble(blocks)

    blocks = [_table_block(t, fks, keys_only=True, table_description=False, **opts)
              for t in present]
    text = assemble(blocks)
    while len(text) > max_chars and len(blocks) > 1:
        blocks.pop()
        omitted = len(present) - len(blocks)
        text = assemble(blocks, f"... {omitted} more table{'s' if omitted != 1 else ''} omitted")
    return text if len(text) <= max_chars else text[:max_chars]


def to_legacy_schema(catalog: Catalog, tables: list[str]) -> dict:
    """The schema dict shape of ``database.schema_utils.SchemaTable`` for ``tables``.

    Only declared foreign keys are listed (inferred joins travel as join hints), and
    ``sample_rows`` is always empty: row data never leaves the catalog through this path.
    """
    declared: dict[str, list[dict]] = {}
    for rel in catalog.relationships:
        if rel.kind == "inferred":
            continue
        fk = {"column": rel.from_column, "references_table": rel.to_table,
              "references_column": rel.to_column}
        bucket = declared.setdefault(rel.from_table, [])
        if fk not in bucket:
            bucket.append(fk)
    schema: dict = {}
    for name in dict.fromkeys(tables):
        table = catalog.tables.get(name)
        if table is None:
            continue
        schema[name] = {
            "columns": [{"name": c.name, "type": c.data_type, "nullable": bool(c.nullable)}
                        for c in table.columns],
            "primary_keys": list(table.primary_keys)
            or [c.name for c in table.columns if c.is_primary_key],
            "foreign_keys": list(declared.get(name, [])),
            "sample_rows": [],
        }
    return schema
