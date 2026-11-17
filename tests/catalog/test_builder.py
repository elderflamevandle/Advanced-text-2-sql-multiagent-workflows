import json
import sqlite3
import time

import pytest
import sqlalchemy as sa

from catalog import builder, describer
from catalog import profiler as profiler_module
from catalog.builder import build_catalog, schema_hash
from catalog.models import ERROR, MISSING, READY, STALE
from catalog.store import CatalogStore
from tests.catalog.conftest_build import (
    SECRET_EMAIL,
    FakeEmbedder,
    FakeLLM,
    make_registry,
    make_shop_db,
    memory_engine,
)

TENANT = "tenant-a"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("T2S_ALLOW_FILE_DATABASES", "1")
    monkeypatch.delenv("T2S_FILE_DATABASE_ROOT", raising=False)
    monkeypatch.setattr(builder, "_config", lambda: {})
    engine = memory_engine()
    registry = make_registry(engine)
    store = CatalogStore(engine)
    db_path = make_shop_db(tmp_path / "shop.db")
    info = registry.register(TENANT, "shop", "sqlite", {"db_path": db_path})

    class Env:
        pass

    e = Env()
    e.engine, e.registry, e.store, e.db_path, e.cid = engine, registry, store, db_path, info.id
    e.llm = FakeLLM()
    monkeypatch.setattr("llm.fallback.get_llm", lambda node, state=None: e.llm)
    profiled = []
    real_profile = profiler_module.profile_table

    def counting_profile(manager, db_type, table, *, options=None):
        profiled.append(table.name)
        return real_profile(manager, db_type, table, options=options)

    monkeypatch.setattr(builder, "profile_table", counting_profile)
    e.profiled = profiled
    yield e
    registry.close_all()
    engine.dispose()


def _alter(db_path, *statements):
    conn = sqlite3.connect(db_path)
    for statement in statements:
        conn.execute(statement)
    conn.commit()
    conn.close()


def test_end_to_end(env):
    embedder = FakeEmbedder()
    events = []
    status = build_catalog(TENANT, env.cid, registry=env.registry, store=env.store,
                           embedder=embedder, describe=True,
                           progress=lambda fraction, stage: events.append((fraction, stage)))

    assert status.state == READY and status.error is None
    assert (status.table_count, status.column_count) == (3, 14)
    assert status.relationship_count >= 2
    assert status.described and status.embedded
    assert status.progress == 1.0 and status.started_at and status.finished_at
    assert events[-1] == (1.0, "ready")
    assert [f for f, _ in events] == sorted(f for f, _ in events)

    catalog = env.store.load(TENANT, env.cid)
    assert catalog.db_type == "sqlite" and catalog.embedder_name == "fake-embedder"
    orders = catalog.tables["orders"]
    assert orders.row_count == 200 and orders.primary_keys == ["id"] and orders.content_hash
    assert orders.column("customer_id").is_foreign_key
    assert orders.column("status").enum_values == ["new", "paid", "returned", "shipped"]
    assert orders.column("quantity").null_fraction == pytest.approx(0.2)
    assert orders.description == "Rows of orders." and orders.description_source == "llm"
    assert orders.column("status").description == "The status of orders."
    fks = {(r.from_table, r.from_column, r.to_table, r.to_column)
           for r in catalog.relationships if r.kind == "foreign_key"}
    assert fks == {("orders", "customer_id", "customers", "id"),
                   ("orders", "product_id", "products", "id")}
    assert catalog.schema_hash == schema_hash(env.registry.get_schema(TENANT, env.cid))

    assert set(catalog.table_vectors) == {"customers", "products", "orders"}
    assert len(catalog.column_vectors) == 14
    assert len(embedder.calls) == 1                         # one batch for everything
    assert any(t.startswith("orders\nRows of orders.\ncolumns: id, customer_id")
               for t in embedder.texts)
    assert any(t.startswith("orders.status (TEXT)") and "values: new, paid" in t
               for t in embedder.texts)
    assert sorted(env.profiled) == ["customers", "orders", "products"]
    assert len(env.llm.prompts) == 3


def test_pii_never_leaves_the_database(env):
    embedder = FakeEmbedder()
    build_catalog(TENANT, env.cid, registry=env.registry, store=env.store,
                  embedder=embedder, describe=True)
    catalog = env.store.load(TENANT, env.cid)
    customers = catalog.tables["customers"]
    assert customers.column("full_name").is_pii             # by name
    assert customers.column("contact").is_pii               # by value (emails)
    assert customers.column("contact").sample_values == []
    assert customers.column("segment").enum_values == ["retail", "wholesale"]

    with env.engine.connect() as conn:
        dump = "".join(
            repr(conn.execute(sa.text(f'SELECT * FROM "{name}"')).all())
            for name in sa.inspect(conn).get_table_names() if name.startswith("t2s_catalog"))
    everything = dump + json.dumps(embedder.texts) + json.dumps(env.llm.prompts)
    assert "wholesale" in dump
    for secret in (SECRET_EMAIL, "@example.org", "Person 1"):
        assert secret not in everything


def test_incremental_rebuild_only_touches_the_changed_table(env):
    embedder = FakeEmbedder()
    kwargs = dict(registry=env.registry, store=env.store, embedder=embedder, describe=True)
    first = build_catalog(TENANT, env.cid, **kwargs)
    v1 = env.store.version(TENANT, env.cid)
    del env.profiled[:]
    env.llm.prompts.clear()
    embedder.calls.clear()

    again = build_catalog(TENANT, env.cid, **kwargs)        # nothing changed
    assert again.state == READY and again.schema_hash == first.schema_hash
    assert env.profiled == [] and env.llm.prompts == [] and embedder.calls == []
    assert env.store.version(TENANT, env.cid) != v1
    assert env.store.load(TENANT, env.cid).tables["orders"].column("status").enum_values

    _alter(env.db_path, "ALTER TABLE products ADD COLUMN colour TEXT",
           "UPDATE products SET colour = 'red'")
    changed = build_catalog(TENANT, env.cid, **kwargs)
    assert changed.state == READY and changed.schema_hash != first.schema_hash
    assert changed.column_count == 15
    assert env.profiled == ["products"]
    assert len(env.llm.prompts) == 1 and "Table: products" in env.llm.prompts[0]
    assert embedder.texts and all(t.startswith("products") for t in embedder.texts)
    catalog = env.store.load(TENANT, env.cid)
    assert catalog.tables["products"].column("colour").enum_values == ["red"]
    assert catalog.tables["orders"].description == "Rows of orders."      # reused
    assert len(catalog.column_vectors) == 15 and len(catalog.table_vectors) == 3

    del env.profiled[:]
    build_catalog(TENANT, env.cid, force=True, **kwargs)
    assert sorted(env.profiled) == ["customers", "orders", "products"]


def test_schema_hash_is_structural_and_stable():
    schema = {
        "b": {"columns": [{"name": "id", "type": "INTEGER", "nullable": False}],
              "primary_keys": ["id"], "foreign_keys": [], "sample_rows": [{"id": 1}]},
        "a": {"columns": [{"name": "id", "type": "INTEGER", "nullable": False},
                          {"name": "b_id", "type": "INTEGER", "nullable": True}],
              "primary_keys": ["id"],
              "foreign_keys": [{"column": "b_id", "references_table": "b",
                                "references_column": "id"}],
              "sample_rows": []},
    }
    base = schema_hash(schema)
    reordered = {"a": dict(schema["a"]), "b": dict(schema["b"], sample_rows=[{"id": 99}])}
    assert schema_hash(reordered) == base and len(base) == 64

    def variant(change):
        copy = json.loads(json.dumps(schema))
        change(copy)
        return schema_hash(copy)

    variants = {
        variant(lambda s: s["a"]["columns"].append({"name": "x", "type": "TEXT"})),
        variant(lambda s: s["a"]["columns"][1].update(type="TEXT")),
        variant(lambda s: s["a"].update(foreign_keys=[])),
        variant(lambda s: s["b"].update(primary_keys=[])),
        variant(lambda s: s.pop("b")),
        variant(lambda s: s["a"]["columns"].reverse()),
    }
    assert base not in variants and len(variants) == 6
    assert schema_hash({}) == schema_hash(None)


def test_user_descriptions_survive_a_forced_rebuild(env):
    kwargs = dict(registry=env.registry, store=env.store, embedder=None, describe=True)
    build_catalog(TENANT, env.cid, **kwargs)
    env.store.set_description(TENANT, env.cid, "orders", None, "Every purchase.")
    env.store.set_description(TENANT, env.cid, "orders", "status", "Fulfilment state.")
    env.llm.prompts.clear()
    build_catalog(TENANT, env.cid, force=True, **kwargs)
    orders = env.store.load(TENANT, env.cid).tables["orders"]
    assert (orders.description, orders.description_source) == ("Every purchase.", "user")
    assert orders.column("status").description == "Fulfilment state."
    assert all("Table: orders" not in p for p in env.llm.prompts)


def test_no_embedder_and_no_descriptions(env):
    status = build_catalog(TENANT, env.cid, registry=env.registry, store=env.store,
                           embedder=None, describe=False)
    assert status.state == READY and not status.embedded and not status.described
    catalog = env.store.load(TENANT, env.cid)
    assert catalog.embedder_name is None and catalog.table_vectors == {}
    assert env.llm.prompts == []


def test_describe_follows_the_config(env, monkeypatch):
    monkeypatch.setattr(builder, "_config", lambda: {"describe_with_llm": False})
    build_catalog(TENANT, env.cid, registry=env.registry, store=env.store, embedder=None)
    assert env.llm.prompts == []
    monkeypatch.setattr(builder, "_config", lambda: {})
    build_catalog(TENANT, env.cid, registry=env.registry, store=env.store, embedder=None)
    assert len(env.llm.prompts) == 3


def test_description_limit_is_reported(env, monkeypatch):
    monkeypatch.setattr(builder, "_config", lambda: {"max_tables_described": 1})
    status = build_catalog(TENANT, env.cid, registry=env.registry, store=env.store,
                           embedder=None, describe=True)
    assert status.state == READY and status.described
    assert len(env.llm.prompts) == 1 and "Table: orders" in env.llm.prompts[0]  # most joins
    notes = env.store.build_info(TENANT, env.cid)["notes"]
    assert "2 of 3 tables have no generated description" in notes and "limit of 1" in notes


def test_llm_outage_still_builds(env):
    from llm.errors import LLMUnavailableError

    env.llm.answer = LLMUnavailableError("down")
    status = build_catalog(TENANT, env.cid, registry=env.registry, store=env.store,
                           embedder=None, describe=True)
    assert status.state == READY and not status.described
    assert "unavailable" in env.store.build_info(TENANT, env.cid)["notes"]


def test_embedder_failure_still_builds(env):
    class Broken(FakeEmbedder):
        def embed_documents(self, texts):
            raise RuntimeError("model exploded")

    status = build_catalog(TENANT, env.cid, registry=env.registry, store=env.store,
                           embedder=Broken(), describe=False)
    assert status.state == READY and not status.embedded
    assert env.store.load(TENANT, env.cid).embedder_name is None


def test_error_without_a_previous_catalog(env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("postgresql://admin:hunter2@db.internal/prod refused")

    monkeypatch.setattr(env.registry, "get_schema", boom)
    status = build_catalog(TENANT, env.cid, registry=env.registry, store=env.store,
                           embedder=None, describe=False)
    assert status.state == ERROR
    assert status.error == "Catalog build failed (RuntimeError)."
    assert "hunter2" not in repr(env.store.status(TENANT, env.cid))
    assert env.store.load(TENANT, env.cid) is None and status.finished_at


def test_error_keeps_the_previous_catalog(env, monkeypatch):
    kwargs = dict(registry=env.registry, store=env.store, embedder=FakeEmbedder(),
                  describe=True)
    build_catalog(TENANT, env.cid, **kwargs)
    version = env.store.version(TENANT, env.cid)
    _alter(env.db_path, "ALTER TABLE products ADD COLUMN colour TEXT")

    def boom(catalog):
        raise RuntimeError("disk full: secret-row-data")

    monkeypatch.setattr(env.store, "save", boom)
    status = build_catalog(TENANT, env.cid, **kwargs)
    assert status.state == STALE                    # old catalog in service, schema moved on
    assert status.error == "Catalog build failed (RuntimeError)."
    catalog = env.store.load(TENANT, env.cid)
    assert catalog is not None and catalog.tables["products"].column("colour") is None
    assert catalog.tables["orders"].description == "Rows of orders."
    assert len(catalog.table_vectors) == 3
    assert env.store.version(TENANT, env.cid) == version


def test_unknown_or_foreign_connection_leaves_no_trace(env):
    for tenant, cid in (("tenant-b", env.cid), (TENANT, "0" * 32)):
        status = build_catalog(tenant, cid, registry=env.registry, store=env.store,
                               embedder=None, describe=False)
        assert status.state == MISSING
        assert env.store.status(tenant, cid).state == MISSING
    with env.engine.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM t2s_catalog_header")).scalar() == 0


def test_inferred_relationships_are_merged_when_available(env, monkeypatch):
    import sys
    import types

    from catalog.models import CatalogRelationship

    fake = types.ModuleType("catalog.graph")

    def infer_relationships(tables, existing):
        return list(existing) + [CatalogRelationship("orders", "quantity", "products", "id",
                                                     kind="inferred", confidence=0.3)]

    fake.infer_relationships = infer_relationships
    monkeypatch.setitem(sys.modules, "catalog.graph", fake)
    build_catalog(TENANT, env.cid, registry=env.registry, store=env.store, embedder=None,
                  describe=False)
    rels = env.store.load(TENANT, env.cid).relationships
    assert [r.kind for r in rels].count("foreign_key") == 2          # no duplicates
    assert [r.kind for r in rels].count("inferred") == 1

    def unfinished(tables, existing):
        raise NotImplementedError

    fake.infer_relationships = unfinished
    status = build_catalog(TENANT, env.cid, registry=env.registry, store=env.store,
                           embedder=None, describe=False, force=True)
    assert status.state == READY and status.relationship_count == 2


def test_three_hundred_tables_build_quickly(tmp_path, monkeypatch):
    monkeypatch.setenv("T2S_ALLOW_FILE_DATABASES", "1")
    monkeypatch.delenv("T2S_FILE_DATABASE_ROOT", raising=False)
    monkeypatch.setattr(builder, "_config", lambda: {})
    monkeypatch.setattr(describer, "describe_tables",
                        lambda *a, **k: pytest.fail("describe must be off"))
    path = str(tmp_path / "wide.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t0 (id INTEGER PRIMARY KEY, label TEXT, kind TEXT, n INTEGER)")
    for i in range(1, 300):
        conn.execute(f"CREATE TABLE t{i} (id INTEGER PRIMARY KEY, label TEXT, kind TEXT, "
                     f"n INTEGER, parent_id INTEGER REFERENCES t{i - 1}(id))")
    for i in range(300):
        marks = "?,?,?,?" if i == 0 else "?,?,?,?,?"
        rows = [(r, f"l{r}", "ab"[r % 2], r) + (() if i == 0 else (r,)) for r in range(1, 6)]
        conn.executemany(f"INSERT INTO t{i} VALUES ({marks})", rows)
    conn.commit()
    conn.close()

    engine = memory_engine()
    registry = make_registry(engine)
    store = CatalogStore(engine)
    info = registry.register(TENANT, "wide", "sqlite", {"db_path": path})
    embedder = FakeEmbedder()
    try:
        started = time.monotonic()
        status = build_catalog(TENANT, info.id, registry=registry, store=store,
                               embedder=embedder, describe=False)
        elapsed = time.monotonic() - started
        assert status.state == READY
        assert (status.table_count, status.column_count) == (300, 4 + 299 * 5)
        assert status.relationship_count >= 299 and status.embedded
        assert elapsed < 10, f"build took {elapsed:.1f}s"
        assert len(embedder.calls) == -(-(300 + 4 + 299 * 5) // builder.EMBED_BATCH)
        catalog = store.load(TENANT, info.id)
        assert catalog.tables["t150"].column("kind").enum_values == ["a", "b"]
        assert len(catalog.table_vectors) == 300
    finally:
        registry.close_all()
        engine.dispose()
