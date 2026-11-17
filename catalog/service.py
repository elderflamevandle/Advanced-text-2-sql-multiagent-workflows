"""CatalogService: the only catalog entry point for graph nodes and the API."""
from __future__ import annotations

import dataclasses
import threading
from collections import OrderedDict

from catalog.models import READY, STALE, Catalog, CatalogStatus, SchemaContext

_UNSET = object()
_USABLE = (READY, STALE)


class CatalogService:
    """Read side of the catalog plus build control.

    ``store``, ``start_build``, ``embedder`` and ``llm_filter`` are injectable; left out they
    resolve lazily to ``catalog.store.get_catalog_store()``, ``catalog.jobs.start_build`` and
    ``catalog.embedding.get_embedder()``. Loaded catalogs are cached per (tenant, connection)
    and reused for as long as ``store.version(...)`` is unchanged.
    """

    def __init__(self, store=None, start_build=None, embedder=_UNSET, llm_filter=None, *,
                 cache_size: int = 32, max_tables: int = 12) -> None:
        self._store = store
        self._start_build = start_build
        self._embedder = embedder
        self.llm_filter = llm_filter
        self.max_tables = max_tables
        self._cache_size = max(1, int(cache_size))
        self._cache: OrderedDict[tuple[str, str], tuple[str, Catalog]] = OrderedDict()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ collaborators
    def _get_store(self):
        if self._store is None:
            from catalog.store import get_catalog_store

            return get_catalog_store()
        return self._store

    def _get_embedder(self, catalog: Catalog):
        if not catalog.embedder_name or not catalog.table_vectors:
            return None                      # lexical only: never load an embedding model
        if self._embedder is _UNSET:
            from catalog.embedding import get_embedder

            return get_embedder()
        return self._embedder

    # ------------------------------------------------------------------ cache
    def _forget(self, tenant_id, connection_id) -> None:
        with self._lock:
            self._cache.pop((tenant_id, connection_id), None)

    def _catalog(self, tenant_id, connection_id) -> Catalog | None:
        store = self._get_store()
        key = (tenant_id, connection_id)
        version = store.version(tenant_id, connection_id)
        if version is None:
            self._forget(tenant_id, connection_id)
            return None
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] == version:
                self._cache.move_to_end(key)
                return hit[1]
        catalog = store.load(tenant_id, connection_id)
        if catalog is None:
            self._forget(tenant_id, connection_id)
            return None
        with self._lock:
            self._cache[key] = (version, catalog)
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return catalog

    # ------------------------------------------------------------------ contract
    def start_build(self, tenant_id, connection_id, *, force=False) -> CatalogStatus:
        if self._start_build is None:
            from catalog.jobs import start_build

            return start_build(tenant_id, connection_id, force=force)
        return self._start_build(tenant_id, connection_id, force=force)

    def status(self, tenant_id, connection_id) -> CatalogStatus:
        return self._get_store().status(tenant_id, connection_id)

    def select_context(self, tenant_id, connection_id, question, *, widen=False,
                       must_include=()) -> SchemaContext | None:
        """Schema context for a question, or None when no ready/stale catalog exists."""
        if self.status(tenant_id, connection_id).state not in _USABLE:
            return None
        catalog = self._catalog(tenant_id, connection_id)
        if catalog is None or not catalog.tables:
            return None
        from catalog.retrieval import select_context

        return select_context(
            catalog, question, embedder=self._get_embedder(catalog), llm_filter=self.llm_filter,
            max_tables=self.max_tables, widen=widen, must_include=must_include)

    def describe(self, tenant_id, connection_id) -> dict:
        """JSON-safe view of the catalog for the API. PII columns carry no values."""
        status = self.status(tenant_id, connection_id)
        out: dict = {"status": dataclasses.asdict(status), "db_type": None, "built_at": None,
                     "tables": [], "relationships": []}
        catalog = self._catalog(tenant_id, connection_id) if status.state in _USABLE else None
        if catalog is None:
            return out
        out["db_type"] = catalog.db_type
        out["built_at"] = catalog.built_at
        for name in sorted(catalog.tables):
            table = catalog.tables[name]
            columns = []
            for col in table.columns:
                show = not col.is_pii
                columns.append({
                    "name": col.name,
                    "data_type": col.data_type,
                    "nullable": bool(col.nullable),
                    "is_primary_key": bool(col.is_primary_key or col.name in table.primary_keys),
                    "is_foreign_key": bool(col.is_foreign_key),
                    "is_unique": bool(col.is_unique),
                    "description": col.description,
                    "description_source": col.description_source,
                    "is_pii": bool(col.is_pii),
                    "pii_reason": col.pii_reason,
                    "distinct_count": col.distinct_count,
                    "null_fraction": col.null_fraction,
                    "cardinality": col.cardinality,
                    "sample_values": [str(v) for v in col.sample_values] if show else [],
                    "enum_values": ([str(v) for v in col.enum_values]
                                    if show and col.enum_values is not None else None),
                })
            out["tables"].append({
                "name": table.name,
                "schema_name": table.schema_name,
                "description": table.description,
                "description_source": table.description_source,
                "row_count": table.row_count,
                "primary_keys": list(table.primary_keys),
                "columns": columns,
            })
        out["relationships"] = sorted(
            ({"from_table": r.from_table, "from_column": r.from_column, "to_table": r.to_table,
              "to_column": r.to_column, "kind": r.kind, "confidence": float(r.confidence)}
             for r in catalog.relationships),
            key=lambda r: (r["from_table"], r["from_column"], r["to_table"], r["to_column"]))
        return out

    def set_description(self, tenant_id, connection_id, table, column, text) -> None:
        self._get_store().set_description(tenant_id, connection_id, table, column, text)
        self._forget(tenant_id, connection_id)

    def delete(self, tenant_id, connection_id) -> None:
        self._get_store().delete(tenant_id, connection_id)
        self._forget(tenant_id, connection_id)


_service: CatalogService | None = None
_service_lock = threading.Lock()


def get_catalog_service() -> CatalogService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = CatalogService()
    return _service


def set_catalog_service(service: CatalogService | None) -> None:
    global _service
    with _service_lock:
        _service = service
