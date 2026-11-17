"""Builds a connection's schema catalog: structure, profile, descriptions, vectors.

The build works on an in-memory catalog and writes it with one atomic ``store.save``, so
readers keep the previous catalog until the new one is complete, and keep it for good when
the build fails. Unchanged tables (same ``content_hash``) reuse the previous profile,
descriptions and vectors unless ``force`` is set.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone

from catalog.models import (
    BUILDING,
    ERROR,
    MISSING,
    READY,
    STALE,
    Catalog,
    CatalogColumn,
    CatalogRelationship,
    CatalogStatus,
    CatalogTable,
)
from catalog.profiler import ProfileOptions, profile_table

logger = logging.getLogger(__name__)

DEFAULT_MAX_TABLES_DESCRIBED = 300
EMBED_BATCH = 256
_STATUS_INTERVAL_SECONDS = 1.0

# progress milestones
_P_SCHEMA, _P_PROFILE, _P_DESCRIBE, _P_EMBED = 0.05, 0.55, 0.85, 0.97


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(payload) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _table_structure(name: str, spec: dict) -> dict:
    spec = spec or {}
    return {
        "name": name,
        "columns": [
            [str(c.get("name")), str(c.get("type") or "").lower(), bool(c.get("nullable", True))]
            for c in spec.get("columns") or []
        ],
        "primary_keys": sorted(str(k) for k in spec.get("primary_keys") or []),
        "foreign_keys": sorted(
            [str(fk.get("column")), str(fk.get("references_table")),
             str(fk.get("references_column"))]
            for fk in spec.get("foreign_keys") or []
        ),
    }


def table_hash(name: str, spec: dict) -> str:
    """Hash of one table's structure (name, columns, types, keys, foreign keys)."""
    return _digest(_table_structure(name, spec))


def schema_hash(schema: dict) -> str:
    """Stable hash of a legacy schema dict: structure only, never sample rows."""
    schema = schema or {}
    return _digest([_table_structure(name, schema[name]) for name in sorted(schema)])


def _config() -> dict:
    try:
        from llm.config import get_section  # noqa: PLC0415
        return get_section("catalog")
    except Exception:  # noqa: BLE001 - config is optional
        return {}


def _config_int(config: dict, key: str, default: int) -> int:
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


def _convert(schema: dict, db_type: str) -> tuple[dict[str, CatalogTable],
                                                  list[CatalogRelationship]]:
    tables: dict[str, CatalogTable] = {}
    relationships: list[CatalogRelationship] = []
    for name, spec in schema.items():
        spec = spec or {}
        primary_keys = [str(k) for k in spec.get("primary_keys") or []]
        foreign_keys = spec.get("foreign_keys") or []
        fk_columns = {str(fk.get("column")) for fk in foreign_keys}
        schema_name = None
        if db_type.startswith("postgres") and "." in name:
            schema_name = name.split(".", 1)[0]
        table = CatalogTable(name=name, schema_name=schema_name, primary_keys=primary_keys,
                             content_hash=table_hash(name, spec))
        for col in spec.get("columns") or []:
            col_name = str(col.get("name"))
            table.columns.append(CatalogColumn(
                name=col_name, data_type=str(col.get("type") or ""),
                nullable=bool(col.get("nullable", True)),
                is_primary_key=col_name in primary_keys,
                is_foreign_key=col_name in fk_columns,
                is_unique=len(primary_keys) == 1 and col_name in primary_keys,
            ))
        tables[name] = table
        for fk in foreign_keys:
            if fk.get("column") and fk.get("references_table") and fk.get("references_column"):
                relationships.append(CatalogRelationship(
                    from_table=name, from_column=str(fk["column"]),
                    to_table=str(fk["references_table"]),
                    to_column=str(fk["references_column"]), kind="foreign_key",
                ))
    return tables, relationships


def _rel_key(rel: CatalogRelationship) -> tuple:
    return (rel.from_table, rel.from_column, rel.to_table, rel.to_column)


def _add_inferred(tables: dict[str, CatalogTable],
                  relationships: list[CatalogRelationship]) -> list[CatalogRelationship]:
    try:
        from catalog.graph import infer_relationships  # noqa: PLC0415
        inferred = infer_relationships(tables, list(relationships)) or []
    except (ImportError, NotImplementedError, AttributeError):
        return relationships
    except Exception as exc:  # noqa: BLE001 - inference is a bonus
        logger.debug("build_catalog: relationship inference failed (%s)", type(exc).__name__)
        return relationships
    seen = {_rel_key(r) for r in relationships}
    merged = list(relationships)
    for rel in inferred:
        if isinstance(rel, CatalogRelationship) and _rel_key(rel) not in seen:
            seen.add(_rel_key(rel))
            merged.append(rel)
    return merged


_PROFILE_FIELDS = ("sample_values", "enum_values", "distinct_count", "null_fraction",
                   "cardinality", "is_pii", "pii_reason", "is_unique")


def _reuse(table: CatalogTable, previous: CatalogTable) -> None:
    """Copy profile and descriptions from the previous build of an unchanged table."""
    table.row_count = previous.row_count
    table.description = previous.description
    table.description_source = previous.description_source
    for column in table.columns:
        old = previous.column(column.name)
        if old is None:
            continue
        for name in _PROFILE_FIELDS:
            value = getattr(old, name)
            setattr(column, name, list(value) if isinstance(value, list) else value)
        column.description = old.description
        column.description_source = old.description_source


def table_document(table: CatalogTable) -> str:
    """Text embedded for a table: name, description and column names."""
    parts = [table.name]
    if table.description:
        parts.append(table.description)
    parts.append("columns: " + ", ".join(c.name for c in table.columns))
    return "\n".join(parts)


def column_document(table: CatalogTable, column: CatalogColumn) -> str:
    """Text embedded for a column. Values of PII columns are never included."""
    parts = [f"{table.name}.{column.name} ({column.data_type})"]
    if column.description:
        parts.append(column.description)
    if not column.is_pii:
        values = column.enum_values or column.sample_values
        if values:
            parts.append("values: " + ", ".join(str(v) for v in values[:25]))
    return "\n".join(parts)


def _safe_error(exc: Exception) -> str:
    """A message without anything the exception text might leak (DSNs, SQL, rows)."""
    name = type(exc).__name__
    if name == "ConnectionNotFound":
        return "Connection not found."
    if name == "ConnectionRejected":
        return "The connection is not allowed by the current policy."
    if name == "CredentialDecryptError":
        return "The stored credentials could not be decrypted."
    if name == "QueryTimeoutError":
        return "The database did not answer in time."
    return f"Catalog build failed ({name})."


class _Reporter:
    """Writes build progress to the store (throttled) and to the optional callback."""

    def __init__(self, store, tenant_id, connection_id, callback) -> None:
        self._store, self._tenant, self._conn = store, tenant_id, connection_id
        self._callback = callback
        self._last_write = 0.0
        self._last_value = -1.0

    def __call__(self, fraction: float, stage: str, *, force: bool = False) -> None:
        fraction = min(1.0, max(0.0, fraction))
        now = time.monotonic()
        if force or (now - self._last_write >= _STATUS_INTERVAL_SECONDS
                     and fraction != self._last_value):
            self._last_write, self._last_value = now, fraction
            try:
                self._store.set_status(self._tenant, self._conn, progress=fraction)
            except Exception:  # noqa: BLE001 - progress is informational
                logger.debug("build_catalog: progress write failed", exc_info=True)
        if self._callback is not None:
            try:
                self._callback(fraction, stage)
            except Exception:  # noqa: BLE001
                logger.debug("build_catalog: progress callback failed", exc_info=True)


def _embed(embedder, texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH):
        batch = embedder.embed_documents(texts[start:start + EMBED_BATCH])
        vectors.extend([float(x) for x in vec] for vec in batch)
    if len(vectors) != len(texts):
        raise ValueError("embedder returned a wrong number of vectors")
    return vectors


def build_catalog(tenant_id, connection_id, *, registry=None, store=None, embedder=...,
                  describe=None, force=False, progress=None,
                  profile_options: ProfileOptions | None = None) -> CatalogStatus:
    """Build (or incrementally rebuild) the catalog of one connection. Never raises.

    ``progress(fraction, stage)`` is optional. ``embedder=...`` means the process-wide
    embedder, ``None`` disables vectors. ``describe=None`` follows the config key
    ``catalog.describe_with_llm`` (default true).
    """
    if store is None:
        from catalog.store import get_catalog_store  # noqa: PLC0415
        store = get_catalog_store()
    try:
        if registry is None:
            from database.registry import get_registry  # noqa: PLC0415
            registry = get_registry()
        info = registry.get_info(tenant_id, connection_id)
    except LookupError:
        # Unknown or foreign connection: leave no trace, look exactly like "missing".
        return CatalogStatus(state=MISSING)
    except Exception as exc:  # noqa: BLE001
        logger.warning("build_catalog: cannot resolve connection (%s)", type(exc).__name__)
        return CatalogStatus(state=ERROR, error=_safe_error(exc))

    previous = None
    new_hash = None
    try:
        report = _Reporter(store, tenant_id, connection_id, progress)
        store.set_status(tenant_id, connection_id, state=BUILDING, progress=0.0, error=None,
                         started_at=_now(), finished_at=None)
        config = _config()
        db_type = info.db_type
        previous = store.load(tenant_id, connection_id)

        schema = registry.get_schema(tenant_id, connection_id, refresh=True) or {}
        new_hash = schema_hash(schema)
        tables, relationships = _convert(schema, db_type)
        relationships = _add_inferred(tables, relationships)
        catalog = Catalog(tenant_id=tenant_id, connection_id=connection_id, db_type=db_type,
                          schema_hash=new_hash, tables=tables, relationships=relationships)
        report(_P_SCHEMA, "schema", force=True)

        # ---- reuse unchanged tables
        reused: set[str] = set()
        if previous is not None and not force:
            for name, table in tables.items():
                old = previous.tables.get(name)
                if old is not None and old.content_hash and \
                        old.content_hash == table.content_hash:
                    _reuse(table, old)
                    reused.add(name)
        # user descriptions always carry over, also to changed tables and forced builds
        if previous is not None:
            for name, table in tables.items():
                old = previous.tables.get(name)
                if old is None:
                    continue
                if old.description_source == "user":
                    table.description, table.description_source = old.description, "user"
                for column in table.columns:
                    old_col = old.column(column.name)
                    if old_col is not None and old_col.description_source == "user":
                        column.description = old_col.description
                        column.description_source = "user"

        # ---- profile
        to_profile = [t for name, t in tables.items() if name not in reused]
        if to_profile:
            options = profile_options or ProfileOptions()
            with registry.lease(tenant_id, connection_id) as manager:
                for index, table in enumerate(to_profile, start=1):
                    profile_table(manager, db_type, table, options=options)
                    report(_P_SCHEMA + (_P_PROFILE - _P_SCHEMA) * index / len(to_profile),
                           "profile")
        report(_P_PROFILE, "profile", force=True)

        # ---- describe
        notes: list[str] = []
        if describe is None:
            describe = bool(config.get("describe_with_llm", True))
        if describe:
            limit = max(0, _config_int(config, "max_tables_described",
                                       DEFAULT_MAX_TABLES_DESCRIBED))
            pending = [name for name, t in tables.items() if not t.description]
            if pending:
                degree: dict[str, int] = {}
                for rel in relationships:
                    degree[rel.from_table] = degree.get(rel.from_table, 0) + 1
                    degree[rel.to_table] = degree.get(rel.to_table, 0) + 1
                order = {name: i for i, name in enumerate(pending)}
                pending.sort(key=lambda n: (-degree.get(n, 0), order[n]))
                chosen = pending[:limit]
                done_count = [0]

                def on_table(_name: str) -> None:
                    done_count[0] += 1
                    report(_P_PROFILE + (_P_DESCRIBE - _P_PROFILE)
                           * done_count[0] / max(1, len(chosen)), "describe")

                from catalog.describer import describe_tables  # noqa: PLC0415
                described = describe_tables(catalog, chosen, on_table=on_table) if chosen else 0
                left = len(pending) - described
                if left > 0:
                    over = len(pending) - len(chosen)
                    reason = (f"{over} over the limit of {limit} tables per build"
                              if over > 0 and described == len(chosen)
                              else "the language model was unavailable or gave no usable answer"
                              if over <= 0 else
                              f"limit of {limit} tables per build, and the language model "
                              "did not answer for all of them")
                    notes.append(f"{left} of {len(tables)} tables have no generated "
                                 f"description ({reason}).")
        report(_P_DESCRIBE, "describe", force=True)

        # ---- embed
        if embedder is ...:
            from catalog.embedding import get_embedder  # noqa: PLC0415
            embedder = get_embedder()
        if embedder is not None:
            try:
                _add_vectors(catalog, previous, reused, embedder,
                             embed_columns=bool(config.get("embed_columns", True)))
            except Exception as exc:  # noqa: BLE001 - lexical search still works
                logger.warning("build_catalog: embedding failed (%s); continuing without "
                               "vectors", type(exc).__name__)
                catalog.table_vectors, catalog.column_vectors = {}, {}
                catalog.embedder_name = None
                notes.append("Embedding failed: lexical search only.")
        report(_P_EMBED, "embed", force=True)

        # ---- publish (atomic; also sets state=ready and the counts)
        store.save(catalog)
        store.set_status(tenant_id, connection_id, notes=" ".join(notes) or None)
        if progress is not None:
            report(1.0, "ready")
        logger.info("Catalog built: connection=%s tables=%d reused=%d", connection_id,
                    len(tables), len(reused))
        return store.status(tenant_id, connection_id)
    except Exception as exc:  # noqa: BLE001 - a build must not take the caller down
        logger.warning("build_catalog: connection=%s failed (%s)", connection_id,
                       type(exc).__name__)
        logger.debug("build_catalog failure detail", exc_info=True)
        return record_failure(store, tenant_id, connection_id, _safe_error(exc),
                              has_catalog=previous is not None,
                              stale=previous is not None and new_hash is not None
                              and new_hash != previous.schema_hash)


def record_failure(store, tenant_id, connection_id, message: str, *,
                   has_catalog: bool | None = None, stale: bool = False) -> CatalogStatus:
    """Record a failed build. A previously built catalog stays in service.

    With an earlier catalog the state goes back to ready (or stale when the schema is
    known to have changed) and ``error`` says why the rebuild failed; otherwise the state
    is error.
    """
    try:
        if has_catalog is None:
            has_catalog = bool(store.build_info(tenant_id, connection_id).get("built_at"))
        state = (STALE if stale else READY) if has_catalog else ERROR
        store.set_status(tenant_id, connection_id, state=state, error=message,
                         finished_at=_now(), progress=1.0 if has_catalog else 0.0)
        return store.status(tenant_id, connection_id)
    except Exception:  # noqa: BLE001
        logger.warning("build_catalog: could not record the failure", exc_info=True)
        return CatalogStatus(state=ERROR, error=message)


def _add_vectors(catalog: Catalog, previous: Catalog | None, reused: set[str], embedder,
                 *, embed_columns: bool) -> None:
    name = getattr(embedder, "name", None) or type(embedder).__name__
    same_embedder = previous is not None and previous.embedder_name == name
    table_keys: list[str] = []
    column_keys: list[tuple[str, str]] = []
    texts: list[str] = []
    column_texts: list[str] = []
    for table_name, table in catalog.tables.items():
        old = previous.tables.get(table_name) if same_embedder and table_name in reused \
            else None
        doc = table_document(table)
        vector = previous.table_vectors.get(table_name) if old is not None else None
        if vector is not None and table_document(old) == doc:
            catalog.table_vectors[table_name] = vector
        else:
            table_keys.append(table_name)
            texts.append(doc)
        if not embed_columns:
            continue
        for column in table.columns:
            key = (table_name, column.name)
            doc = column_document(table, column)
            old_col = old.column(column.name) if old is not None else None
            vector = previous.column_vectors.get(key) if old_col is not None else None
            if vector is not None and column_document(old, old_col) == doc:
                catalog.column_vectors[key] = vector
            else:
                column_keys.append(key)
                column_texts.append(doc)
    vectors = _embed(embedder, texts + column_texts) if texts or column_texts else []
    for key, vector in zip(table_keys, vectors[:len(table_keys)]):
        catalog.table_vectors[key] = vector
    for key, vector in zip(column_keys, vectors[len(table_keys):]):
        catalog.column_vectors[key] = vector
    catalog.embedder_name = name
