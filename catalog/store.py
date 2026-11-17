"""Persistence of schema catalogs in the shared metadata database.

Every statement filters on tenant_id AND connection_id. A connection's catalog is replaced
in one transaction, so readers see either the previous catalog or the new one. Descriptions
written by users live in their own table: they survive rebuilds and always win over
generated text.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import numpy as np
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from catalog.models import (
    BUILDING,
    MISSING,
    READY,
    Catalog,
    CatalogColumn,
    CatalogRelationship,
    CatalogStatus,
    CatalogTable,
)

logger = logging.getLogger(__name__)

_metadata = sa.MetaData()
_KEY = dict(nullable=False)

header_table = sa.Table(
    "t2s_catalog_header",
    _metadata,
    sa.Column("tenant_id", sa.String(255), primary_key=True),
    sa.Column("connection_id", sa.String(64), primary_key=True),
    sa.Column("db_type", sa.String(32)),
    sa.Column("schema_hash", sa.String(64)),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("progress", sa.Float, nullable=False, default=0.0),
    sa.Column("described", sa.Boolean, nullable=False, default=False),
    sa.Column("embedded", sa.Boolean, nullable=False, default=False),
    sa.Column("error", sa.Text),
    sa.Column("notes", sa.Text),
    sa.Column("started_at", sa.String(40)),
    sa.Column("finished_at", sa.String(40)),
    sa.Column("updated_at", sa.String(40)),
    sa.Column("built_at", sa.String(40)),
    sa.Column("embedder_name", sa.String(255)),
    sa.Column("version", sa.String(32)),
    sa.Column("table_count", sa.Integer, nullable=False, default=0),
    sa.Column("column_count", sa.Integer, nullable=False, default=0),
    sa.Column("relationship_count", sa.Integer, nullable=False, default=0),
)

tables_table = sa.Table(
    "t2s_catalog_tables",
    _metadata,
    sa.Column("tenant_id", sa.String(255), primary_key=True),
    sa.Column("connection_id", sa.String(64), primary_key=True),
    sa.Column("name", sa.String(255), primary_key=True),
    sa.Column("position", sa.Integer, **_KEY),
    sa.Column("schema_name", sa.String(255)),
    sa.Column("primary_keys", sa.Text),
    sa.Column("row_count", sa.BigInteger),
    sa.Column("description", sa.Text),
    sa.Column("description_source", sa.String(16)),
    sa.Column("content_hash", sa.String(64)),
)

columns_table = sa.Table(
    "t2s_catalog_columns",
    _metadata,
    sa.Column("tenant_id", sa.String(255), primary_key=True),
    sa.Column("connection_id", sa.String(64), primary_key=True),
    sa.Column("table_name", sa.String(255), primary_key=True),
    sa.Column("name", sa.String(255), primary_key=True),
    sa.Column("position", sa.Integer, **_KEY),
    sa.Column("data_type", sa.String(255)),
    sa.Column("nullable", sa.Boolean),
    sa.Column("is_primary_key", sa.Boolean),
    sa.Column("is_foreign_key", sa.Boolean),
    sa.Column("is_unique", sa.Boolean),
    sa.Column("description", sa.Text),
    sa.Column("description_source", sa.String(16)),
    sa.Column("sample_values", sa.Text),
    sa.Column("enum_values", sa.Text),
    sa.Column("distinct_count", sa.BigInteger),
    sa.Column("null_fraction", sa.Float),
    sa.Column("cardinality", sa.String(16)),
    sa.Column("is_pii", sa.Boolean),
    sa.Column("pii_reason", sa.String(255)),
)

relationships_table = sa.Table(
    "t2s_catalog_relationships",
    _metadata,
    sa.Column("tenant_id", sa.String(255), primary_key=True),
    sa.Column("connection_id", sa.String(64), primary_key=True),
    sa.Column("position", sa.Integer, primary_key=True),
    sa.Column("from_table", sa.String(255), **_KEY),
    sa.Column("from_column", sa.String(255), **_KEY),
    sa.Column("to_table", sa.String(255), **_KEY),
    sa.Column("to_column", sa.String(255), **_KEY),
    sa.Column("kind", sa.String(32), **_KEY),
    sa.Column("confidence", sa.Float, **_KEY),
)

vectors_table = sa.Table(
    "t2s_catalog_vectors",
    _metadata,
    sa.Column("tenant_id", sa.String(255), primary_key=True),
    sa.Column("connection_id", sa.String(64), primary_key=True),
    sa.Column("kind", sa.String(8), primary_key=True),          # table | column
    sa.Column("table_name", sa.String(255), primary_key=True),
    sa.Column("column_name", sa.String(255), primary_key=True),  # "" for a table vector
    sa.Column("dim", sa.Integer, **_KEY),
    sa.Column("vector", sa.LargeBinary, **_KEY),                # float32, little endian
)

descriptions_table = sa.Table(
    "t2s_catalog_descriptions",
    _metadata,
    sa.Column("tenant_id", sa.String(255), primary_key=True),
    sa.Column("connection_id", sa.String(64), primary_key=True),
    sa.Column("table_name", sa.String(255), primary_key=True),
    sa.Column("column_name", sa.String(255), primary_key=True),  # "" for the table itself
    sa.Column("text", sa.Text, **_KEY),
    sa.Column("updated_at", sa.String(40)),
)

_CONTENT_TABLES = (tables_table, columns_table, relationships_table, vectors_table)
_STATUS_FIELDS = frozenset({
    "state", "schema_hash", "progress", "described", "embedded", "error", "notes",
    "started_at", "finished_at", "db_type",
})
_CHUNK = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_version() -> str:
    return uuid.uuid4().hex


def _dumps(value) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _loads(text, default):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def _pack(vector) -> bytes:
    return np.asarray(vector, dtype="<f4").tobytes()


def _unpack(blob) -> list[float]:
    return np.frombuffer(bytes(blob), dtype="<f4").tolist()


def _scope(table: sa.Table, tenant_id, connection_id):
    return sa.and_(table.c.tenant_id == tenant_id, table.c.connection_id == connection_id)


def _valid(tenant_id, connection_id) -> bool:
    return (isinstance(tenant_id, str) and bool(tenant_id)
            and isinstance(connection_id, str) and bool(connection_id))


class CatalogStore:
    """Thread-safe, synchronous catalog storage."""

    def __init__(self, engine: Engine | None = None) -> None:
        if engine is None:
            from database.metadata import get_engine  # noqa: PLC0415
            engine = get_engine()
        self._engine = engine
        # An in-memory SQLite engine shares one connection between threads: serialise.
        self._serial = threading.RLock() if isinstance(engine.pool, StaticPool) else None
        _metadata.create_all(engine)

    @contextmanager
    def _guard(self):
        if self._serial is None:
            yield
        else:
            with self._serial:
                yield

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _header(conn, tenant_id, connection_id):
        h = header_table
        return conn.execute(sa.select(h).where(_scope(h, tenant_id, connection_id))).first()

    @staticmethod
    def _overrides(conn, tenant_id, connection_id) -> dict[tuple[str, str], str]:
        d = descriptions_table
        rows = conn.execute(
            sa.select(d.c.table_name, d.c.column_name, d.c.text)
            .where(_scope(d, tenant_id, connection_id))
        ).all()
        return {(r.table_name, r.column_name): r.text for r in rows}

    @staticmethod
    def _upsert_header(conn, tenant_id, connection_id, values: dict) -> None:
        h = header_table
        values = dict(values, updated_at=_now())
        updated = conn.execute(
            sa.update(h).where(_scope(h, tenant_id, connection_id)).values(**values)
        )
        if updated.rowcount == 0:
            row = {"state": MISSING, "progress": 0.0, "described": False, "embedded": False,
                   "table_count": 0, "column_count": 0, "relationship_count": 0}
            row.update(values, tenant_id=tenant_id, connection_id=connection_id)
            conn.execute(sa.insert(h).values(**row))

    @staticmethod
    def _insert(conn, table: sa.Table, rows: list[dict]) -> None:
        for start in range(0, len(rows), _CHUNK):
            conn.execute(sa.insert(table), rows[start:start + _CHUNK])

    @staticmethod
    def apply_overrides(catalog: Catalog, overrides: dict[tuple[str, str], str]) -> None:
        """Put user-written descriptions on top of whatever the catalog holds."""
        for (table_name, column_name), text in overrides.items():
            table = catalog.tables.get(table_name)
            if table is None:
                continue
            target = table.column(column_name) if column_name else table
            if target is not None:
                target.description = text
                target.description_source = "user"

    # ------------------------------------------------------------------ writes
    def save(self, catalog: Catalog) -> None:
        """Replace this connection's catalog atomically and mark it ready."""
        tenant_id, connection_id = catalog.tenant_id, catalog.connection_id
        if not _valid(tenant_id, connection_id):
            raise ValueError("tenant_id and connection_id are required.")
        key = {"tenant_id": tenant_id, "connection_id": connection_id}
        with self._guard(), self._engine.begin() as conn:
            self.apply_overrides(catalog, self._overrides(conn, tenant_id, connection_id))
            table_rows, column_rows, vector_rows = [], [], []
            described = False
            for t_pos, (name, table) in enumerate(catalog.tables.items()):
                described = described or bool(table.description)
                table_rows.append(dict(
                    key, name=name, position=t_pos, schema_name=table.schema_name,
                    primary_keys=_dumps(list(table.primary_keys)), row_count=table.row_count,
                    description=table.description,
                    description_source=table.description_source,
                    content_hash=table.content_hash,
                ))
                for c_pos, col in enumerate(table.columns):
                    pii = bool(col.is_pii)
                    described = described or bool(col.description)
                    column_rows.append(dict(
                        key, table_name=name, name=col.name, position=c_pos,
                        data_type=col.data_type, nullable=bool(col.nullable),
                        is_primary_key=bool(col.is_primary_key),
                        is_foreign_key=bool(col.is_foreign_key), is_unique=bool(col.is_unique),
                        description=col.description,
                        description_source=col.description_source,
                        # Defence in depth: values of PII columns are never written.
                        sample_values=_dumps([] if pii else [str(v) for v in col.sample_values]),
                        enum_values=None if pii or col.enum_values is None
                        else _dumps([str(v) for v in col.enum_values]),
                        distinct_count=col.distinct_count, null_fraction=col.null_fraction,
                        cardinality=col.cardinality, is_pii=pii, pii_reason=col.pii_reason,
                    ))
            rel_rows = [
                dict(key, position=pos, from_table=r.from_table, from_column=r.from_column,
                     to_table=r.to_table, to_column=r.to_column, kind=r.kind,
                     confidence=float(r.confidence))
                for pos, r in enumerate(catalog.relationships)
            ]
            for name, vec in catalog.table_vectors.items():
                vector_rows.append(dict(key, kind="table", table_name=name, column_name="",
                                        dim=len(vec), vector=_pack(vec)))
            for (t_name, c_name), vec in catalog.column_vectors.items():
                vector_rows.append(dict(key, kind="column", table_name=t_name,
                                        column_name=c_name, dim=len(vec), vector=_pack(vec)))

            for table in _CONTENT_TABLES:
                conn.execute(sa.delete(table).where(_scope(table, tenant_id, connection_id)))
            self._insert(conn, tables_table, table_rows)
            self._insert(conn, columns_table, column_rows)
            self._insert(conn, relationships_table, rel_rows)
            self._insert(conn, vectors_table, vector_rows)
            now = _now()
            built_at = catalog.built_at or now
            catalog.built_at = built_at
            self._upsert_header(conn, tenant_id, connection_id, dict(
                db_type=catalog.db_type, schema_hash=catalog.schema_hash, state=READY,
                progress=1.0, described=described, embedded=bool(vector_rows), error=None,
                finished_at=now, built_at=built_at, embedder_name=catalog.embedder_name,
                version=_new_version(), table_count=len(table_rows),
                column_count=len(column_rows), relationship_count=len(rel_rows),
            ))

    def set_status(self, tenant_id, connection_id, **fields) -> None:
        """Update build status fields (state, progress, error, started_at, ...)."""
        unknown = set(fields) - _STATUS_FIELDS
        if unknown:
            raise TypeError(f"Unknown status fields: {sorted(unknown)}")
        if not _valid(tenant_id, connection_id):
            raise ValueError("tenant_id and connection_id are required.")
        if "progress" in fields:
            fields["progress"] = min(1.0, max(0.0, float(fields["progress"] or 0.0)))
        if fields.get("error"):
            fields["error"] = str(fields["error"])[:500]
        with self._guard(), self._engine.begin() as conn:
            self._upsert_header(conn, tenant_id, connection_id, fields)

    def set_description(self, tenant_id, connection_id, table, column, text) -> None:
        """Store a user description (column=None for the table). Empty text removes it.

        Raises KeyError when the catalog, table or column does not exist for this tenant.
        """
        column_key = column or ""
        text = (text or "").strip()
        t, c, d = tables_table, columns_table, descriptions_table
        with self._guard(), self._engine.begin() as conn:
            header = self._header(conn, tenant_id, connection_id) \
                if _valid(tenant_id, connection_id) else None
            if header is None or header.built_at is None:
                raise KeyError("Catalog not found.")
            if column:
                target = c
                where = sa.and_(_scope(c, tenant_id, connection_id),
                                c.c.table_name == table, c.c.name == column)
            else:
                target = t
                where = sa.and_(_scope(t, tenant_id, connection_id), t.c.name == table)
            if conn.execute(sa.select(sa.func.count()).select_from(target).where(where)
                            ).scalar_one() == 0:
                raise KeyError("Unknown table or column.")
            d_where = sa.and_(_scope(d, tenant_id, connection_id),
                              d.c.table_name == table, d.c.column_name == column_key)
            conn.execute(sa.delete(d).where(d_where))
            if text:
                conn.execute(sa.insert(d).values(
                    tenant_id=tenant_id, connection_id=connection_id, table_name=table,
                    column_name=column_key, text=text, updated_at=_now()))
            conn.execute(sa.update(target).where(where).values(
                description=text or None, description_source="user" if text else None))
            values = {"version": _new_version()}
            if text:
                values["described"] = True
            self._upsert_header(conn, tenant_id, connection_id, values)

    def delete(self, tenant_id, connection_id) -> None:
        """Remove the catalog, its status and its user descriptions. Unknown: no-op."""
        if not _valid(tenant_id, connection_id):
            return
        with self._guard(), self._engine.begin() as conn:
            for table in (*_CONTENT_TABLES, descriptions_table, header_table):
                conn.execute(sa.delete(table).where(_scope(table, tenant_id, connection_id)))

    # ------------------------------------------------------------------ reads
    def load(self, tenant_id, connection_id) -> Catalog | None:
        """Return the last successfully built catalog, or None."""
        if not _valid(tenant_id, connection_id):
            return None
        t, c, r, v = tables_table, columns_table, relationships_table, vectors_table
        with self._guard(), self._engine.connect() as conn:
            header = self._header(conn, tenant_id, connection_id)
            if header is None or header.built_at is None:
                return None
            table_rows = conn.execute(
                sa.select(t).where(_scope(t, tenant_id, connection_id)).order_by(t.c.position)
            ).all()
            column_rows = conn.execute(
                sa.select(c).where(_scope(c, tenant_id, connection_id))
                .order_by(c.c.table_name, c.c.position)
            ).all()
            rel_rows = conn.execute(
                sa.select(r).where(_scope(r, tenant_id, connection_id)).order_by(r.c.position)
            ).all()
            vector_rows = conn.execute(
                sa.select(v).where(_scope(v, tenant_id, connection_id))
            ).all()
            overrides = self._overrides(conn, tenant_id, connection_id)

        catalog = Catalog(
            tenant_id=tenant_id, connection_id=connection_id, db_type=header.db_type or "",
            schema_hash=header.schema_hash or "", built_at=header.built_at,
            embedder_name=header.embedder_name,
        )
        for row in table_rows:
            catalog.tables[row.name] = CatalogTable(
                name=row.name, schema_name=row.schema_name,
                primary_keys=_loads(row.primary_keys, []), row_count=row.row_count,
                description=row.description, description_source=row.description_source,
                content_hash=row.content_hash or "",
            )
        for row in column_rows:
            table = catalog.tables.get(row.table_name)
            if table is None:
                continue
            pii = bool(row.is_pii)
            table.columns.append(CatalogColumn(
                name=row.name, data_type=row.data_type or "", nullable=bool(row.nullable),
                is_primary_key=bool(row.is_primary_key),
                is_foreign_key=bool(row.is_foreign_key), is_unique=bool(row.is_unique),
                description=row.description, description_source=row.description_source,
                sample_values=[] if pii else _loads(row.sample_values, []),
                enum_values=None if pii else _loads(row.enum_values, None),
                distinct_count=row.distinct_count, null_fraction=row.null_fraction,
                cardinality=row.cardinality, is_pii=pii, pii_reason=row.pii_reason,
            ))
        catalog.relationships = [
            CatalogRelationship(from_table=row.from_table, from_column=row.from_column,
                                to_table=row.to_table, to_column=row.to_column,
                                kind=row.kind, confidence=row.confidence)
            for row in rel_rows
        ]
        for row in vector_rows:
            if row.kind == "table":
                catalog.table_vectors[row.table_name] = _unpack(row.vector)
            else:
                catalog.column_vectors[(row.table_name, row.column_name)] = _unpack(row.vector)
        self.apply_overrides(catalog, overrides)
        return catalog

    def status(self, tenant_id, connection_id) -> CatalogStatus:
        if not _valid(tenant_id, connection_id):
            return CatalogStatus(state=MISSING)
        with self._guard(), self._engine.connect() as conn:
            header = self._header(conn, tenant_id, connection_id)
        if header is None:
            return CatalogStatus(state=MISSING)
        return CatalogStatus(
            state=header.state or MISSING, schema_hash=header.schema_hash,
            table_count=header.table_count or 0, column_count=header.column_count or 0,
            relationship_count=header.relationship_count or 0,
            progress=float(header.progress or 0.0), described=bool(header.described),
            embedded=bool(header.embedded), error=header.error,
            started_at=header.started_at, finished_at=header.finished_at,
        )

    def version(self, tenant_id, connection_id) -> str | None:
        if not _valid(tenant_id, connection_id):
            return None
        h = header_table
        with self._guard(), self._engine.connect() as conn:
            return conn.execute(
                sa.select(h.c.version).where(_scope(h, tenant_id, connection_id))
            ).scalar_one_or_none()

    # ------------------------------------------------------------------ extras
    def build_info(self, tenant_id, connection_id) -> dict:
        """Details that CatalogStatus has no field for: notes, heartbeat, built_at.

        ``notes`` says what a build skipped (for example tables left without descriptions).
        ``updated_at`` is touched by every status write and serves as the build heartbeat.
        """
        if not _valid(tenant_id, connection_id):
            return {}
        with self._guard(), self._engine.connect() as conn:
            header = self._header(conn, tenant_id, connection_id)
        if header is None:
            return {}
        return {"notes": header.notes, "updated_at": header.updated_at,
                "built_at": header.built_at, "embedder_name": header.embedder_name,
                "db_type": header.db_type, "building": header.state == BUILDING}


_store: CatalogStore | None = None
_store_lock = threading.Lock()


def get_catalog_store() -> CatalogStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = CatalogStore()
    return _store


def set_catalog_store(store: CatalogStore | None) -> None:
    global _store
    with _store_lock:
        _store = store
