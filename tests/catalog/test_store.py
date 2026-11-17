import pytest
import sqlalchemy as sa

from catalog import store as store_module
from catalog.models import BUILDING, MISSING, READY
from catalog.store import CatalogStore, get_catalog_store, set_catalog_store
from tests.catalog.conftest_build import memory_engine, sample_catalog


@pytest.fixture
def engine():
    engine = memory_engine()
    yield engine
    engine.dispose()


@pytest.fixture
def store(engine):
    return CatalogStore(engine)


def test_status_of_unknown_connection_is_missing(store):
    status = store.status("tenant-a", "nope")
    assert status.state == MISSING
    assert store.load("tenant-a", "nope") is None
    assert store.version("tenant-a", "nope") is None


def test_round_trip(store):
    original = sample_catalog()
    store.save(original)
    loaded = store.load("tenant-a", "conn-1")

    assert loaded is not None
    assert list(loaded.tables) == ["customers", "orders"]
    assert loaded.db_type == "sqlite" and loaded.schema_hash == "s1"
    assert loaded.built_at and loaded.embedder_name == "fake-embedder"
    customers = loaded.tables["customers"]
    assert [c.name for c in customers.columns] == ["id", "segment", "email"]
    assert customers.primary_keys == ["id"] and customers.row_count == 40
    assert customers.description == "People who buy things."
    assert customers.content_hash == "h1"
    segment = customers.column("segment")
    assert segment.enum_values == ["retail", "wholesale"]
    assert segment.sample_values == ["retail"]
    assert segment.distinct_count == 2 and segment.cardinality == "low"
    assert segment.null_fraction == pytest.approx(0.25)
    assert segment.description_source == "llm"
    assert customers.column("id").is_primary_key and customers.column("id").is_unique
    assert customers.column("id").nullable is False
    assert customers.column("id").enum_values is None
    email = customers.column("email")
    assert email.is_pii and email.sample_values == [] and email.enum_values is None
    assert loaded.tables["orders"].column("customer_id").is_foreign_key

    assert [(r.from_table, r.to_table, r.kind) for r in loaded.relationships] == [
        ("orders", "customers", "foreign_key"), ("orders", "customers", "inferred")]
    assert loaded.relationships[1].confidence == pytest.approx(0.4)
    assert loaded.table_vectors["customers"] == pytest.approx([0.25, -0.5, 1.0])
    assert loaded.column_vectors[("customers", "segment")] == pytest.approx([1.0, 0.0, 0.5])

    status = store.status("tenant-a", "conn-1")
    assert status.state == READY and status.progress == 1.0
    assert (status.table_count, status.column_count, status.relationship_count) == (2, 5, 2)
    assert status.described and status.embedded and status.schema_hash == "s1"


def test_pii_values_are_never_written(store, engine):
    catalog = sample_catalog()
    email = catalog.tables["customers"].column("email")
    email.sample_values = ["leak@example.org"]
    email.enum_values = ["leak@example.org"]
    store.save(catalog)
    with engine.connect() as conn:
        dump = repr(conn.execute(sa.text("SELECT * FROM t2s_catalog_columns")).all())
    assert "leak@example.org" not in dump


def test_tenant_isolation(store):
    store.save(sample_catalog())
    assert store.load("tenant-b", "conn-1") is None
    assert store.status("tenant-b", "conn-1").state == MISSING
    assert store.version("tenant-b", "conn-1") is None
    with pytest.raises(KeyError):
        store.set_description("tenant-b", "conn-1", "customers", None, "mine now")
    store.delete("tenant-b", "conn-1")

    loaded = store.load("tenant-a", "conn-1")
    assert loaded is not None and loaded.tables["customers"].description_source == "llm"
    assert store.status("tenant-b", "conn-1").state == MISSING


def test_two_tenants_with_the_same_connection_id(store):
    store.save(sample_catalog("tenant-a"))
    other = sample_catalog("tenant-b")
    del other.tables["orders"]
    other.relationships = []
    store.save(other)
    assert list(store.load("tenant-a", "conn-1").tables) == ["customers", "orders"]
    assert list(store.load("tenant-b", "conn-1").tables) == ["customers"]
    store.delete("tenant-b", "conn-1")
    assert store.load("tenant-b", "conn-1") is None
    assert store.load("tenant-a", "conn-1") is not None


def test_user_descriptions_survive_save(store):
    store.save(sample_catalog())
    store.set_description("tenant-a", "conn-1", "customers", None, "Our buyers.")
    store.set_description("tenant-a", "conn-1", "customers", "segment", "Sales channel.")

    loaded = store.load("tenant-a", "conn-1")
    assert loaded.tables["customers"].description == "Our buyers."
    assert loaded.tables["customers"].description_source == "user"
    assert loaded.tables["customers"].column("segment").description == "Sales channel."

    rebuilt = sample_catalog()            # a rebuild knows nothing about the user texts
    rebuilt.tables["customers"].description = "Generated again."
    store.save(rebuilt)
    assert rebuilt.tables["customers"].description == "Our buyers."   # applied on save
    loaded = store.load("tenant-a", "conn-1")
    assert loaded.tables["customers"].description == "Our buyers."
    assert loaded.tables["customers"].description_source == "user"
    segment = loaded.tables["customers"].column("segment")
    assert (segment.description, segment.description_source) == ("Sales channel.", "user")
    assert loaded.tables["orders"].description is None


def test_set_description_unknown_targets(store):
    with pytest.raises(KeyError):
        store.set_description("tenant-a", "conn-1", "customers", None, "x")
    store.save(sample_catalog())
    with pytest.raises(KeyError):
        store.set_description("tenant-a", "conn-1", "nope", None, "x")
    with pytest.raises(KeyError):
        store.set_description("tenant-a", "conn-1", "customers", "nope", "x")


def test_clearing_a_user_description(store):
    store.save(sample_catalog())
    store.set_description("tenant-a", "conn-1", "orders", None, "Purchases.")
    store.set_description("tenant-a", "conn-1", "orders", None, "")
    store.save(sample_catalog())
    assert store.load("tenant-a", "conn-1").tables["orders"].description is None


def test_version_changes_with_the_catalog(store):
    store.save(sample_catalog())
    v1 = store.version("tenant-a", "conn-1")
    assert v1
    store.set_status("tenant-a", "conn-1", progress=0.5)
    assert store.version("tenant-a", "conn-1") == v1      # status is not content
    store.set_description("tenant-a", "conn-1", "orders", None, "Purchases.")
    v2 = store.version("tenant-a", "conn-1")
    store.save(sample_catalog())
    v3 = store.version("tenant-a", "conn-1")
    assert len({v1, v2, v3}) == 3


def test_save_replaces_everything(store):
    store.save(sample_catalog())
    smaller = sample_catalog()
    del smaller.tables["orders"]
    smaller.relationships = []
    smaller.table_vectors = {}
    smaller.column_vectors = {}
    smaller.embedder_name = None
    store.save(smaller)
    loaded = store.load("tenant-a", "conn-1")
    assert list(loaded.tables) == ["customers"]
    assert loaded.relationships == [] and loaded.table_vectors == {}
    status = store.status("tenant-a", "conn-1")
    assert status.table_count == 1 and not status.embedded


def test_failed_save_keeps_the_previous_catalog(store):
    store.save(sample_catalog())
    version = store.version("tenant-a", "conn-1")
    broken = sample_catalog()
    broken.tables["orders"].columns.append(broken.tables["orders"].columns[0])  # duplicate key
    with pytest.raises(Exception):
        store.save(broken)
    loaded = store.load("tenant-a", "conn-1")
    assert list(loaded.tables) == ["customers", "orders"]
    assert len(loaded.tables["orders"].columns) == 2
    assert loaded.table_vectors and len(loaded.relationships) == 2
    assert store.version("tenant-a", "conn-1") == version


def test_status_while_building_keeps_the_catalog_readable(store):
    store.set_status("tenant-a", "conn-1", state=BUILDING, progress=0.3, started_at="t0")
    status = store.status("tenant-a", "conn-1")
    assert (status.state, status.progress, status.started_at) == (BUILDING, 0.3, "t0")
    assert store.load("tenant-a", "conn-1") is None        # nothing built yet

    store.save(sample_catalog())
    store.set_status("tenant-a", "conn-1", state=BUILDING, progress=0.1)
    assert store.load("tenant-a", "conn-1") is not None    # old catalog during a rebuild
    with pytest.raises(TypeError):
        store.set_status("tenant-a", "conn-1", bogus=1)


def test_delete_removes_user_descriptions_too(store):
    store.save(sample_catalog())
    store.set_description("tenant-a", "conn-1", "orders", None, "Purchases.")
    store.delete("tenant-a", "conn-1")
    assert store.status("tenant-a", "conn-1").state == MISSING
    store.save(sample_catalog())
    assert store.load("tenant-a", "conn-1").tables["orders"].description is None


def test_singleton(monkeypatch, engine):
    monkeypatch.setattr(store_module, "_store", None)
    custom = CatalogStore(engine)
    set_catalog_store(custom)
    assert get_catalog_store() is custom
    set_catalog_store(None)
    monkeypatch.setenv("T2S_METADATA_URL", "sqlite://")
    from database import metadata
    metadata.reset_engine()
    try:
        assert isinstance(get_catalog_store(), CatalogStore)
        assert get_catalog_store() is get_catalog_store()
    finally:
        set_catalog_store(None)
        metadata.reset_engine()
