import threading
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from catalog import builder, jobs
from catalog.models import BUILDING, ERROR, MISSING, READY, CatalogStatus
from catalog.store import CatalogStore, header_table, set_catalog_store
from database.registry import set_registry
from tests.catalog.conftest_build import (
    make_registry,
    make_shop_db,
    memory_engine,
    sample_catalog,
)

TENANT = "tenant-a"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("T2S_ALLOW_FILE_DATABASES", "1")
    monkeypatch.delenv("T2S_FILE_DATABASE_ROOT", raising=False)
    config = {"describe_with_llm": False}
    monkeypatch.setattr(builder, "_config", lambda: config)
    monkeypatch.setattr(jobs, "_config", lambda: config)
    monkeypatch.setattr("catalog.embedding.get_embedder", lambda: None)
    engine = memory_engine()
    registry = make_registry(engine)
    store = CatalogStore(engine)
    set_registry(registry)
    set_catalog_store(store)
    info = registry.register(TENANT, "shop", "sqlite",
                             {"db_path": make_shop_db(tmp_path / "shop.db")})

    class Env:
        pass

    e = Env()
    e.engine, e.registry, e.store, e.cid, e.config = engine, registry, store, info.id, config
    yield e
    jobs.shutdown()
    set_registry(None)
    set_catalog_store(None)
    registry.close_all()
    engine.dispose()


def test_background_build_and_wait(env):
    status = jobs.start_build(TENANT, env.cid)
    assert status.state in (BUILDING, READY) and status.started_at
    final = jobs.wait_for_build(TENANT, env.cid, timeout=30)
    assert final.state == READY and final.table_count == 3
    assert env.store.load(TENANT, env.cid) is not None
    assert not jobs.is_running(TENANT, env.cid)
    assert jobs.wait_for_build(TENANT, env.cid, timeout=1).state == READY   # nothing running


def test_single_flight(env, monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    calls = []

    def slow_build(tenant_id, connection_id, *, force=False, **kwargs):
        calls.append((tenant_id, connection_id, force))
        entered.set()
        assert release.wait(20)
        env.store.set_status(tenant_id, connection_id, state=READY, progress=1.0)
        return env.store.status(tenant_id, connection_id)

    monkeypatch.setattr(builder, "build_catalog", slow_build)
    first = jobs.start_build(TENANT, env.cid, force=True)
    assert first.state == BUILDING
    assert entered.wait(10)
    second = jobs.start_build(TENANT, env.cid)
    third = jobs.start_build(TENANT, env.cid)
    assert second.state == BUILDING and third.state == BUILDING
    assert jobs.is_running(TENANT, env.cid)
    assert jobs.wait_for_build(TENANT, env.cid, timeout=0.05).state == BUILDING   # times out
    release.set()
    assert jobs.wait_for_build(TENANT, env.cid, timeout=20).state == READY
    assert calls == [(TENANT, env.cid, True)]

    jobs.start_build(TENANT, env.cid)                   # a finished build can be repeated
    jobs.wait_for_build(TENANT, env.cid, timeout=20)
    assert len(calls) == 2


def test_failure_is_recorded_and_does_not_escape(env, monkeypatch):
    def boom(tenant_id, connection_id, **kwargs):
        raise RuntimeError("password=hunter2")

    monkeypatch.setattr(builder, "build_catalog", boom)
    assert jobs.start_build(TENANT, env.cid).state == BUILDING
    status = jobs.wait_for_build(TENANT, env.cid, timeout=20)
    assert status.state == ERROR
    assert status.error == "Catalog build failed (RuntimeError)."
    assert not jobs.is_running(TENANT, env.cid)


def test_build_error_inside_the_builder_is_recorded(env, monkeypatch):
    def broken_schema(*args, **kwargs):
        raise RuntimeError("no route to host 10.0.0.5")

    monkeypatch.setattr(env.registry, "get_schema", broken_schema)
    jobs.start_build(TENANT, env.cid)
    status = jobs.wait_for_build(TENANT, env.cid, timeout=20)
    assert status.state == ERROR and "10.0.0.5" not in status.error


def test_unknown_and_foreign_connections(env, monkeypatch):
    monkeypatch.setattr(builder, "build_catalog",
                        lambda *a, **k: pytest.fail("must not start"))
    assert jobs.start_build("tenant-b", env.cid).state == MISSING
    assert jobs.start_build(TENANT, "f" * 32).state == MISSING
    assert env.store.status("tenant-b", env.cid).state == MISSING
    assert jobs.wait_for_build("tenant-b", env.cid, timeout=1).state == MISSING


def _age_header(env, seconds):
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
        timespec="seconds")
    with env.engine.begin() as conn:
        conn.execute(sa.update(header_table).values(updated_at=stamp, started_at=stamp))


def test_stale_building_row_is_marked_failed(env):
    env.store.set_status(TENANT, env.cid, state=BUILDING, progress=0.4)
    assert jobs.reconcile(TENANT, env.cid).state == BUILDING        # fresh heartbeat
    _age_header(env, 3600)
    status = jobs.reconcile(TENANT, env.cid)
    assert status.state == ERROR and "interrupted" in status.error


def test_stale_row_with_an_older_catalog_goes_back_to_ready(env):
    env.store.save(sample_catalog(TENANT, env.cid))
    env.store.set_status(TENANT, env.cid, state=BUILDING, progress=0.4)
    _age_header(env, 3600)
    status = jobs.reconcile(TENANT, env.cid)
    assert status.state == READY and "interrupted" in status.error
    assert env.store.load(TENANT, env.cid) is not None


def test_start_build_respects_a_live_build_elsewhere_and_replaces_a_dead_one(env, monkeypatch):
    calls = []

    def fake_build(tenant_id, connection_id, **kwargs):
        calls.append(connection_id)
        env.store.set_status(tenant_id, connection_id, state=READY, progress=1.0)
        return CatalogStatus(state=READY)

    monkeypatch.setattr(builder, "build_catalog", fake_build)
    env.store.set_status(TENANT, env.cid, state=BUILDING, progress=0.2)   # another process
    assert jobs.start_build(TENANT, env.cid).state == BUILDING
    assert calls == [] and not jobs.is_running(TENANT, env.cid)

    _age_header(env, 3600)                                                # it died
    jobs.start_build(TENANT, env.cid)
    assert jobs.wait_for_build(TENANT, env.cid, timeout=20).state == READY
    assert calls == [env.cid]


def test_worker_count_comes_from_the_config(env):
    env.config["build_workers"] = 1
    jobs.shutdown()
    assert jobs._get_executor()._max_workers == 1
