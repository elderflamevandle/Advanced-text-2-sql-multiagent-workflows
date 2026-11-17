"""Relationship graph over a catalog: bridging tables, lookup tables, join hints.

Replaces the old one-hop, forward-only expansion in ``vector/schema_graph.py``. The graph is
undirected (a join works in both directions) and covers declared foreign keys as well as
relationships inferred from naming conventions; declared ones are always preferred.
"""
from __future__ import annotations

import heapq
import re

from catalog.models import Catalog, CatalogRelationship, CatalogTable

_DECLARED = "foreign_key"
_INFERRED = "inferred"

# Column name tokens that mark a human-readable label.
_LABEL_TOKENS = frozenset({
    "name", "title", "label", "description", "desc", "code", "fullname", "displayname",
})
_TEXT_TYPE = re.compile(r"char|text|string|clob|varchar", re.I)
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _name_tokens(name: str) -> list[str]:
    return [t.lower() for t in _WORDS.findall(name or "")]


def _rel_key(rel: CatalogRelationship) -> tuple:
    a = (rel.from_table, rel.from_column)
    b = (rel.to_table, rel.to_column)
    return (a, b) if a <= b else (b, a)


def _rel_rank(rel: CatalogRelationship) -> tuple:
    """Lower is better: declared before inferred, then higher confidence."""
    return (0 if rel.kind != _INFERRED else 1, -float(rel.confidence or 0.0))


class RelationshipGraph:
    """Undirected adjacency over the relationships of one catalog."""

    def __init__(self, catalog: Catalog) -> None:
        self._tables = catalog.tables
        best: dict[tuple, CatalogRelationship] = {}
        for rel in catalog.relationships:
            if rel.from_table not in self._tables or rel.to_table not in self._tables:
                continue
            key = _rel_key(rel)
            current = best.get(key)
            if current is None or _rel_rank(rel) < _rel_rank(current):
                best[key] = rel
        self._rels: list[CatalogRelationship] = sorted(
            best.values(),
            key=lambda r: (r.from_table, r.to_table, r.from_column, r.to_column),
        )
        # _adj[a][b] = 0 when a declared relationship joins a and b, 1 when only inferred ones do
        self._adj: dict[str, dict[str, int]] = {}
        self._out: dict[str, list[CatalogRelationship]] = {}
        for rel in self._rels:
            self._out.setdefault(rel.from_table, []).append(rel)
            if rel.from_table == rel.to_table:
                continue
            cost = 1 if rel.kind == _INFERRED else 0
            for a, b in ((rel.from_table, rel.to_table), (rel.to_table, rel.from_table)):
                row = self._adj.setdefault(a, {})
                row[b] = min(row.get(b, cost), cost)

    # ------------------------------------------------------------------ bridging
    def neighbours(self, table: str) -> list[str]:
        return sorted(self._adj.get(table, {}))

    def _nearest(self, sources: set[str], targets: set[str], max_hops: int) -> list[str] | None:
        """Shortest path from any source to the nearest target.

        Cost is (hops, inferred edges on the path); ties break on table names, so the result
        does not depend on dict or set ordering.
        """
        best: dict[str, tuple[int, int]] = {s: (0, 0) for s in sources}
        parent: dict[str, str | None] = {s: None for s in sources}
        heap = [(0, 0, s) for s in sorted(sources)]
        heapq.heapify(heap)
        while heap:
            hops, inferred, node = heapq.heappop(heap)
            if best.get(node) != (hops, inferred):
                continue
            if node in targets:
                path = [node]
                while parent[path[-1]] is not None:
                    path.append(parent[path[-1]])
                return path[::-1]
            if hops >= max_hops:
                continue
            for other, extra in sorted(self._adj.get(node, {}).items()):
                cost = (hops + 1, inferred + extra)
                known = best.get(other)
                if known is None or cost < known:
                    best[other] = cost
                    parent[other] = node
                    heapq.heappush(heap, (cost[0], cost[1], other))
        return None

    def bridge(self, tables: list[str], *, max_hops: int = 4) -> list[str]:
        """Extra tables needed to connect ``tables`` (approximate Steiner tree).

        Starting from the first table, the nearest still unconnected table is attached by its
        shortest path, repeatedly. Tables that cannot be reached within ``max_hops`` start
        their own component: they are left unconnected, never an error.
        """
        selected: list[str] = []
        for name in tables:
            if name in self._tables and name not in selected:
                selected.append(name)
        if len(selected) < 2:
            return []
        chosen = set(selected)
        connected = {selected[0]}
        remaining = set(selected[1:])
        extra: list[str] = []
        while remaining:
            path = self._nearest(connected, remaining, max_hops)
            if path is None:
                # nothing reachable: the next table (input order) seeds another component
                seed = next(t for t in selected if t in remaining)
                remaining.discard(seed)
                connected.add(seed)
                continue
            for node in path:
                if node not in chosen and node not in extra:
                    extra.append(node)
                connected.add(node)
                remaining.discard(node)
        return extra

    # ------------------------------------------------------------------ lookups
    def _has_label(self, table: CatalogTable) -> bool:
        keys = set(table.primary_keys)
        for col in table.columns:
            if col.is_primary_key or col.is_foreign_key or col.name in keys:
                continue
            if col.data_type and not _TEXT_TYPE.search(col.data_type):
                continue
            tokens = _name_tokens(col.name)
            if tokens and (tokens[-1] in _LABEL_TOKENS or "".join(tokens) in _LABEL_TOKENS):
                return True
        return False

    def lookups(self, tables: list[str]) -> list[str]:
        """Tables a selected table points at that turn an id into a readable label."""
        chosen = {t for t in tables if t in self._tables}
        found: set[str] = set()
        for table in chosen:
            for rel in self._out.get(table, []):
                target = rel.to_table
                if target == table or target in chosen or target in found:
                    continue
                if self._has_label(self._tables[target]):
                    found.add(target)
        return sorted(found)

    # ------------------------------------------------------------------ join hints
    def join_hints(self, tables: list[str]) -> list[dict]:
        """One hint per relationship whose two ends are both in ``tables``."""
        chosen = {t for t in tables if t in self._tables}
        hints: list[dict] = []
        for rel in self._rels:  # already deduplicated, declared preferred
            if rel.from_table in chosen and rel.to_table in chosen:
                hints.append({
                    "from": rel.from_table,
                    "to": rel.to_table,
                    "on": rel.join_condition,
                    "kind": rel.kind,
                })
        return hints


# ---------------------------------------------------------------------- inference
_IRREGULAR = {
    "people": "person", "children": "child", "men": "man", "women": "woman",
    "feet": "foot", "teeth": "tooth", "mice": "mouse", "geese": "goose",
    "indices": "index", "matrices": "matrix", "vertices": "vertex", "analyses": "analysis",
    "diagnoses": "diagnosis", "statuses": "status", "addresses": "address",
}
_TABLE_PREFIXES = ("dim", "fact", "tbl", "lkp", "ref", "stg")
# Same-named key columns that say nothing about which table they belong to.
_GENERIC_KEYS = frozenset({
    "id", "key", "pk", "uuid", "guid", "code", "name", "rowid", "oid", "no", "number", "num",
})
_TYPE_FAMILIES = (
    ("int", re.compile(r"int|serial|number|numeric|decimal", re.I)),
    ("uuid", re.compile(r"uuid|guid|uniqueidentifier", re.I)),
    ("text", re.compile(r"char|text|string|clob", re.I)),
    ("float", re.compile(r"float|double|real", re.I)),
    ("time", re.compile(r"date|time", re.I)),
    ("bool", re.compile(r"bool|bit", re.I)),
    ("binary", re.compile(r"blob|binary|bytea", re.I)),
)


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def singular(word: str) -> str:
    """Normalised singular of a lower-case identifier (best effort, never raises)."""
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _type_family(data_type: str | None) -> str | None:
    if not data_type:
        return None
    for family, pattern in _TYPE_FAMILIES:
        if pattern.search(data_type):
            return family
    return None


def _compatible(a: str | None, b: str | None) -> tuple[bool, bool]:
    """(compatible, both known). Unknown types never block a match."""
    fa, fb = _type_family(a), _type_family(b)
    if fa is None or fb is None:
        return True, False
    return fa == fb, True


def _key_column(table: CatalogTable) -> tuple[str | None, bool]:
    """The single-column key of a table and whether it is a declared primary key."""
    keys = list(table.primary_keys) or [c.name for c in table.columns if c.is_primary_key]
    if len(keys) == 1:
        return keys[0], True
    return None, False


def _split_suffix(column: str) -> tuple[list[str], str] | None:
    """Split ``customer_id`` / ``customerId`` / ``customerid`` / ``customer_key`` into
    (entity tokens, suffix). None when the column does not look like a reference."""
    tokens = _name_tokens(column)
    if not tokens:
        return None
    if len(tokens) >= 2 and tokens[-1] in ("id", "key"):
        return tokens[:-1], tokens[-1]
    last = tokens[-1]
    if last.endswith("id") and len(last) >= 5:        # glued form: customerid
        return tokens[:-1] + [last[:-2]], "id"
    return None


def infer_relationships(
    tables: dict[str, CatalogTable], existing: list[CatalogRelationship]
) -> list[CatalogRelationship]:
    """Naming-convention joins for databases without declared foreign keys.

    Two rules, both tuned for precision (an ambiguous match is skipped, never guessed):

    1. ``<x>_id`` / ``<x>id`` / ``<x>_key`` pointing at the one table named x (singular or
       plural, case and underscores ignored) whose key is ``id``, ``<x>_id`` or the same name.
    2. a column sharing its name with the single-column primary key of exactly one other table.
    """
    taken: set[tuple[str, str]] = set()      # columns that already take part in a relationship
    pairs: set[tuple] = set()
    for rel in existing:
        taken.add((rel.from_table, rel.from_column))
        pairs.add(_rel_key(rel))

    by_entity: dict[str, list[str]] = {}
    by_stripped: dict[str, list[str]] = {}
    for name in tables:
        by_entity.setdefault(singular(_norm(name)), []).append(name)
        tokens = _name_tokens(name)
        if len(tokens) >= 2 and tokens[0] in _TABLE_PREFIXES:
            by_stripped.setdefault(singular("".join(tokens[1:])), []).append(name)

    pk_owner: dict[str, list[str]] = {}       # normalised pk column name -> tables keyed by it
    for name, table in tables.items():
        key, declared = _key_column(table)
        if key and declared:
            pk_owner.setdefault(_norm(key), []).append(name)

    def resolve(entity: str) -> tuple[str | None, bool]:
        exact = by_entity.get(entity, [])
        if len(exact) == 1:
            return exact[0], True
        if exact:
            return None, False                 # ambiguous
        stripped = by_stripped.get(entity, [])
        if len(stripped) == 1:
            return stripped[0], False
        return None, False

    def target_key(target: CatalogTable, entity: str, column: str) -> tuple[str | None, bool]:
        accepted = {"id", entity + "id", entity + "key", _norm(column),
                    singular(_norm(target.name)) + "id"}
        key, declared = _key_column(target)
        if key:
            return (key, declared) if _norm(key) in accepted else (None, False)
        if target.primary_keys:
            return None, False                 # composite key: not a simple reference
        named = [c.name for c in target.columns if _norm(c.name) in accepted]
        return (named[0], False) if len(named) == 1 else (None, False)

    found: list[CatalogRelationship] = []

    def add(table: str, column: str, to_table: str, to_column: str, confidence: float) -> None:
        rel = CatalogRelationship(table, column, to_table, to_column, kind=_INFERRED,
                                  confidence=round(max(0.5, min(0.9, confidence)), 2))
        if _rel_key(rel) in pairs:
            return
        pairs.add(_rel_key(rel))
        taken.add((table, column))
        found.append(rel)

    for table_name in sorted(tables):
        table = tables[table_name]
        own_key, _ = _key_column(table)
        for col in table.columns:
            if (table_name, col.name) in taken:
                continue
            # ---- rule 1: <x>_id -> table x
            split = _split_suffix(col.name)
            matched = False
            if split:
                tokens, _suffix = split
                for start in range(len(tokens)):
                    entity = singular("".join(tokens[start:]))
                    if len(entity) < 2:
                        continue
                    target_name, exact = resolve(entity)
                    if target_name is None:
                        continue
                    target = tables[target_name]
                    to_col, declared = target_key(target, entity, col.name)
                    if to_col is None:
                        break
                    if target_name == table_name and (to_col == col.name or start == 0):
                        break                  # the key itself, or <table>_id inside its table
                    ok, known = _compatible(col.data_type, target.column(to_col).data_type
                                            if target.column(to_col) else None)
                    if not ok:
                        break
                    confidence = 0.9 if known else 0.85
                    if start > 0:
                        confidence = 0.6       # role prefix: billing_address_id -> addresses
                    if not exact:
                        confidence = min(confidence, 0.7)
                    if not declared:
                        confidence = min(confidence, 0.55)
                    add(table_name, col.name, target_name, to_col, confidence)
                    matched = True
                    break
            if matched or (table_name, col.name) in taken:
                continue
            # ---- rule 2: same name as exactly one other table's single-column primary key
            norm = _norm(col.name)
            if norm in _GENERIC_KEYS or col.name == own_key:
                continue
            owners = [t for t in pk_owner.get(norm, []) if t != table_name]
            if len(owners) != 1:
                continue
            target = tables[owners[0]]
            to_col, _ = _key_column(target)
            target_col = target.column(to_col)
            ok, known = _compatible(col.data_type, target_col.data_type if target_col else None)
            if ok:
                add(table_name, col.name, owners[0], to_col, 0.8 if known else 0.7)
    return found
