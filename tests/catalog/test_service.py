"""CatalogService with a fake store: caching by version, readiness, isolation, describe."""
from __future__ import annotations

import json
import threading

import pytest

import catalog.service as service_module
from catalog.models import BUILDING, ERROR, MISSING, READY, STALE, Catalog, CatalogStatus
from catalog.service import CatalogService, get_catalog_service, set_catalog_service
from tests.catalog.test_retrieval import BUSINESS, FakeEmbedder, make_catalog, with_vectors


class FakeStore:
    def __init__(self):
        self.catalogs: dict[tuple, Catalog] = {}
        self.states: dict[tuple, str] = {}
        self.versions: dict[tuple, int] = {}
        self.loads = 0
        self.descriptions = []

    def put(self, tenant, conn, catalog, state=READY):
        self.catalogs[(tenant, conn)] = catalog
        self.states[(tenant, conn)] = state
        self.versions[(tenant, conn)] = self.versions.get((tenant, conn), 0) + 1

    def load(self, tenant, conn):
        self.loads += 1
        return self.catalogs.get((tenant, conn))

    def status(self, tenant, conn):
        catalog = self.catalogs.get((tenant, conn))
        return CatalogStatus(state=self.states.get((tenant, conn), MISSING),
                             table_count=len(catalog.tables) if catalog else 0)

    def version(self, tenant, conn):
        number = self.versions.get((tenant, conn))
        return None if number is None else f"v{number}"

    def set_description(self, tenant, conn, table, column, text):
        if (tenant, conn) not in self.catalogs:
            raise KeyError(table)
        self.descriptions.append((tenant, conn, table, column, text))
        self.versions[(tenant, conn)] += 1

    def delete(self, tenant, conn):
        for mapping in (self.catalogs, self.states, self.versions):
            mapping.pop((tenant, conn), None)


QUESTION = "Which physicians have the most appointments?"


@pytest.fixture
def store():
    fake = FakeStore()
    fake.put("tenant-a", "conn-1", make_catalog(BUSINESS))
    return fake


def test_select_context_uses_the_catalog(store):
    service = CatalogService(store=store, embedder=None)
    ctx = service.select_context("tenant-a", "conn-1", QUESTION)
    assert {"physicians", "appointments"} <= set(ctx.tables)
    ctx = service.select_context("tenant-a", "conn-1", QUESTION, widen=True,
                                 must_include=("coupons",))
    assert ctx.reasons["coupons"] == "required"


def test_catalog_is_cached_until_the_version_changes(store):
    service = CatalogService(store=store, embedder=None)
    for _ in range(5):
        service.select_context("tenant-a", "conn-1", QUESTION)
    assert store.loads == 1
    smaller = make_catalog({k: BUSINESS[k] for k in list(BUSINESS)[:10]})
    store.put("tenant-a", "conn-1", smaller)
    ctx = service.select_context("tenant-a", "conn-1", "orders by customer")
    assert store.loads == 2 and "physicians" not in ctx.tables
    service.select_context("tenant-a", "conn-1", "orders by customer")
    assert store.loads == 2


def test_none_unless_ready_or_stale(store):
    service = CatalogService(store=store, embedder=None)
    for state, usable in ((MISSING, False), (BUILDING, False), (ERROR, False), (STALE, True),
                          (READY, True)):
        store.states[("tenant-a", "conn-1")] = state
        result = service.select_context("tenant-a", "conn-1", QUESTION)
        assert (result is not None) is usable, state
    store.put("tenant-a", "empty", Catalog("tenant-a", "empty", "sqlite", "h"))
    assert service.select_context("tenant-a", "empty", QUESTION) is None
    assert service.select_context("tenant-a", "never-built", QUESTION) is None
    store.states[("tenant-a", "ghost")] = READY          # status says ready, nothing stored
    assert service.select_context("tenant-a", "ghost", QUESTION) is None


def test_tenant_isolation(store):
    other = make_catalog({k: BUSINESS[k] for k in ("coupons", "order_coupons", "orders")})
    store.put("tenant-b", "conn-1", other)               # same connection id, another tenant
    service = CatalogService(store=store, embedder=None)
    mine = service.select_context("tenant-a", "conn-1", QUESTION)
    theirs = service.select_context("tenant-b", "conn-1", QUESTION)
    assert "physicians" in mine.tables
    assert set(theirs.tables) <= {"coupons", "order_coupons", "orders"}
    assert service.select_context("tenant-c", "conn-1", QUESTION) is None
    assert service.status("tenant-c", "conn-1").state == MISSING
    assert service.describe("tenant-c", "conn-1")["tables"] == []
    service.delete("tenant-b", "conn-1")
    assert service.select_context("tenant-b", "conn-1", QUESTION) is None
    assert service.select_context("tenant-a", "conn-1", QUESTION) is not None


def test_cache_is_bounded(store):
    for i in range(6):
        store.put("tenant-a", f"c{i}", make_catalog({"orders": BUSINESS["orders"]}))
    service = CatalogService(store=store, embedder=None, cache_size=3)
    for i in range(6):
        service.select_context("tenant-a", f"c{i}", "orders")
    assert len(service._cache) == 3 and store.loads == 6
    service.select_context("tenant-a", "c5", "orders")
    assert store.loads == 6
    service.select_context("tenant-a", "c0", "orders")
    assert store.loads == 7


def test_concurrent_questions_share_one_catalog(store):
    service = CatalogService(store=store, embedder=None)
    service.select_context("tenant-a", "conn-1", QUESTION)
    results, errors = [], []

    def ask():
        try:
            results.append(service.select_context("tenant-a", "conn-1", QUESTION).tables)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=ask) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(results) == 16 and all(r == results[0] for r in results)
    assert store.loads == 1


def test_embedder_and_llm_filter_are_passed_through(store):
    embedder = FakeEmbedder()
    store.put("tenant-a", "vec", with_vectors(make_catalog(BUSINESS), embedder))
    calls = []

    def llm_filter(question, candidates):
        calls.append(question)
        return [c["name"] for c in candidates]

    service = CatalogService(store=store, embedder=embedder, llm_filter=llm_filter)
    ctx = service.select_context("tenant-a", "vec", "average earnings of medics")
    assert {"employees", "physicians"} <= set(ctx.tables)
    assert embedder.queries == 1 and calls == ["average earnings of medics"]
    service.select_context("tenant-a", "conn-1", QUESTION)       # no vectors: embedder unused
    assert embedder.queries == 1


def test_default_embedder_is_not_resolved_for_lexical_catalogs(store, monkeypatch):
    import catalog.embedding as embedding

    def fail():
        raise AssertionError("embedding model must not be loaded")

    monkeypatch.setattr(embedding, "get_embedder", fail)
    service = CatalogService(store=store)
    assert service.select_context("tenant-a", "conn-1", QUESTION) is not None


def test_describe_is_json_safe_and_hides_pii_values(store):
    catalog = store.catalogs[("tenant-a", "conn-1")]
    catalog.tables["customers"].column("email").sample_values = ["leak@example.org"]
    catalog.tables["employees"].column("ssn").enum_values = ["078-05-1120"]
    service = CatalogService(store=store, embedder=None)
    info = service.describe("tenant-a", "conn-1")
    text = json.dumps(info)
    assert "leak@example.org" not in text and "078-05-1120" not in text
    assert info["status"]["state"] == READY and info["db_type"] == "postgresql"
    tables = {t["name"]: t for t in info["tables"]}
    assert len(tables) == len(BUSINESS)
    orders = tables["orders"]
    assert orders["row_count"] == 90000 and orders["primary_keys"] == ["order_id"]
    assert orders["description"].startswith("One row per order")
    status = next(c for c in orders["columns"] if c["name"] == "status")
    assert status["enum_values"] == ["Pending", "Shipped", "Delivered", "Cancelled"]
    email = next(c for c in tables["customers"]["columns"] if c["name"] == "email")
    assert email["is_pii"] and email["sample_values"] == [] and email["enum_values"] is None
    assert {"from_table": "orders", "from_column": "customer_id", "to_table": "customers",
            "to_column": "customer_id", "kind": "foreign_key",
            "confidence": 1.0} in info["relationships"]
    store.states[("tenant-a", "conn-1")] = BUILDING
    building = service.describe("tenant-a", "conn-1")
    assert building["tables"] == [] and building["status"]["state"] == BUILDING


def test_set_description_and_delete_go_to_the_store_and_drop_the_cache(store):
    service = CatalogService(store=store, embedder=None)
    service.select_context("tenant-a", "conn-1", QUESTION)
    service.set_description("tenant-a", "conn-1", "orders", None, "All orders")
    assert store.descriptions == [("tenant-a", "conn-1", "orders", None, "All orders")]
    service.select_context("tenant-a", "conn-1", QUESTION)
    assert store.loads == 2
    with pytest.raises(KeyError):
        service.set_description("tenant-b", "conn-1", "orders", None, "x")
    service.delete("tenant-a", "conn-1")
    assert service.select_context("tenant-a", "conn-1", QUESTION) is None
    assert not service._cache


def test_start_build_is_delegated(store):
    started = []

    def start_build(tenant, conn, *, force=False):
        started.append((tenant, conn, force))
        return CatalogStatus(state=BUILDING)

    service = CatalogService(store=store, start_build=start_build)
    assert service.start_build("tenant-a", "conn-1", force=True).state == BUILDING
    assert started == [("tenant-a", "conn-1", True)]


def test_singleton_can_be_replaced(store):
    custom = CatalogService(store=store)
    try:
        set_catalog_service(custom)
        assert get_catalog_service() is custom
        set_catalog_service(None)
        fresh = get_catalog_service()
        assert fresh is not custom and get_catalog_service() is fresh
    finally:
        set_catalog_service(None)
    assert service_module._service is None
