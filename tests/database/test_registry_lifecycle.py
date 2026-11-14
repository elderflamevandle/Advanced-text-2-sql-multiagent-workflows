"""ConnectionRegistry live managers: caching, LRU / idle eviction, leases, schema herd."""
import sqlite3
import threading
import time

import pytest
import sqlalchemy as sa

from database.connectors.sqlite_connector import SQLiteConnector
from database.credentials import CredentialCipher, generate_key
from database.manager import DatabaseManager
from database.registry import ConnectionNotFound, ConnectionRegistry

PG = {"host": "db.example.com", "database": "d", "user": "u", "password": "pw"}


class FakeManager:
    """Stands in for DatabaseManager; records disposal."""

    def __init__(self, db_type, **params):
        self.db_type = db_type
        self.params = params
        self.disposed = 0
        self.in_use = False
        self.schema_calls = 0

    def dispose(self):
        self.disposed += 1

    def dispose_if_idle(self):
        if self.in_use:
            return False
        self.disposed += 1
        return True

    def get_schema(self):
        self.schema_calls += 1
        return {"t": {}}

    def refresh_schema(self):
        self.schema_calls += 1
        return {"t": {}, "fresh": {}}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_registry(tmp_path, clock):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    cipher = CredentialCipher(generate_key())
    registries = []

    def factory(**kwargs):
        kwargs.setdefault("manager_factory", FakeManager)
        kwargs.setdefault("host_validator", lambda h, p: None)
        kwargs.setdefault("clock", clock)
        registry = ConnectionRegistry(engine, cipher=cipher, **kwargs)
        registries.append(registry)
        return registry

    yield factory
    for registry in registries:
        registry.close_all()
    engine.dispose()


def add(registry, n, tenant="t"):
    return [registry.register(tenant, f"c{i}", "postgresql", PG, verify=False).id
            for i in range(n)]


def test_manager_is_lazy_and_cached(make_registry):
    built = []

    def factory(db_type, **params):
        built.append(db_type)
        return FakeManager(db_type, **params)

    registry = make_registry(manager_factory=factory)
    (cid,) = add(registry, 1)
    assert built == [] and registry.open_count == 0        # nothing opened by register
    first = registry.get_manager("t", cid)
    assert registry.get_manager("t", cid) is first
    assert built == ["postgresql"] and registry.open_count == 1


def test_concurrent_first_use_builds_one_manager(make_registry):
    built = []

    def slow_factory(db_type, **params):
        built.append(1)
        time.sleep(0.05)
        return FakeManager(db_type, **params)

    registry = make_registry(manager_factory=slow_factory)
    (cid,) = add(registry, 1)
    barrier = threading.Barrier(12)
    results = []

    def worker():
        barrier.wait()
        results.append(registry.get_manager("t", cid))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(built) == 1 and len({id(m) for m in results}) == 1


def test_lru_eviction_disposes_least_recently_used(make_registry, clock):
    registry = make_registry(max_open=2)
    a, b, c = add(registry, 3)
    ma = registry.get_manager("t", a)
    clock.now += 1
    mb = registry.get_manager("t", b)
    clock.now += 1
    registry.get_manager("t", a)             # a is now the most recently used
    clock.now += 1
    mc = registry.get_manager("t", c)        # over the limit: b goes
    assert (ma.disposed, mb.disposed, mc.disposed) == (0, 1, 0)
    assert registry.open_count == 2
    assert registry.get_manager("t", a) is ma
    rebuilt = registry.get_manager("t", b)   # evicted connections come back on demand
    assert rebuilt is not mb and rebuilt.disposed == 0
    assert ma.disposed == 0 and mc.disposed == 1


def test_lru_never_evicts_in_use_managers(make_registry, clock):
    registry = make_registry(max_open=1)
    a, b, c = add(registry, 3)
    ma = registry.get_manager("t", a)
    ma.in_use = True                          # a query is running on it
    clock.now += 1
    with registry.lease("t", b) as mb:        # pinned by a lease
        clock.now += 1
        mc = registry.get_manager("t", c)
        assert ma.disposed == 0 and mb.disposed == 0
        assert registry.open_count == 3       # temporarily over max_open rather than unsafe
    # once the work is finished they become evictable again
    ma.in_use = False
    clock.now += 1
    registry.get_manager("t", c)
    assert registry.open_count == 1
    assert (ma.disposed, mb.disposed, mc.disposed) == (1, 1, 0)


def test_idle_eviction_disposes_only_idle_managers(make_registry, clock):
    registry = make_registry(idle_ttl_seconds=100)
    a, b, c, d = add(registry, 4)
    ma, mb, mc = (registry.get_manager("t", x) for x in (a, b, c))
    mb.in_use = True
    clock.now += 60
    md = registry.get_manager("t", d)         # recently used
    with registry.lease("t", c):
        clock.now += 60                       # a, b, c are now 120 s idle; d only 60 s
        assert registry.evict_idle() == 1
        assert (ma.disposed, mb.disposed, mc.disposed, md.disposed) == (1, 0, 0, 0)
    mb.in_use = False
    clock.now += 100                          # releasing the lease refreshed c's clock
    assert registry.evict_idle() == 3
    assert (mb.disposed, mc.disposed, md.disposed) == (1, 1, 1)
    assert registry.open_count == 0
    assert registry.get_manager("t", a) is not ma


def test_idle_eviction_runs_opportunistically_on_use(make_registry, clock):
    registry = make_registry(idle_ttl_seconds=100)
    a, b = add(registry, 2)
    ma = registry.get_manager("t", a)
    clock.now += 500
    registry.get_manager("t", b)
    assert ma.disposed == 1 and registry.open_count == 1


def test_delete_disposes_and_removes(make_registry):
    registry = make_registry()
    a, b = add(registry, 2)
    ma, mb = registry.get_manager("t", a), registry.get_manager("t", b)
    registry.delete("t", a)
    assert ma.disposed == 1 and mb.disposed == 0 and registry.open_count == 1
    with pytest.raises(ConnectionNotFound):
        registry.get_manager("t", a)


def test_delete_in_another_process_is_noticed_after_revalidation(make_registry, clock):
    one = make_registry(revalidate_seconds=30)
    two = make_registry()
    (cid,) = add(one, 1)
    manager = one.get_manager("t", cid)
    two.delete("t", cid)                       # e.g. a different API worker
    assert one.get_manager("t", cid) is manager   # still inside the revalidation window
    clock.now += 31
    with pytest.raises(ConnectionNotFound):
        one.get_manager("t", cid)
    assert manager.disposed == 1 and one.open_count == 0


def test_close_all_disposes_everything(make_registry):
    registry = make_registry()
    managers = [registry.get_manager("t", cid) for cid in add(registry, 3)]
    registry.close_all()
    registry.close_all()                       # idempotent
    assert [m.disposed for m in managers] == [1, 1, 1] and registry.open_count == 0


def test_failed_build_leaves_no_cache_entry(make_registry):
    calls = []

    def factory(db_type, **params):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("driver exploded: pw")
        return FakeManager(db_type, **params)

    registry = make_registry(manager_factory=factory)
    (cid,) = add(registry, 1)
    with pytest.raises(ValueError) as err:
        registry.get_manager("t", cid)
    assert "pw" not in str(err.value)
    assert registry.open_count == 0
    assert isinstance(registry.get_manager("t", cid), FakeManager)   # retry works


def test_get_schema_cached_and_refresh(make_registry):
    registry = make_registry()
    (cid,) = add(registry, 1)
    assert registry.get_schema("t", cid) == {"t": {}}
    assert "fresh" in registry.get_schema("t", cid, refresh=True)
    with pytest.raises(ConnectionNotFound):
        registry.get_schema("other", cid)


# ---------------------------------------------------------------------------
# Real DatabaseManager
# ---------------------------------------------------------------------------

@pytest.fixture
def sqlite_path(tmp_path):
    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (owner TEXT, n INTEGER)")
    conn.execute("INSERT INTO items VALUES ('a', 1)")
    conn.commit()
    conn.close()
    return str(path)


def test_schema_fetched_once_under_concurrent_calls(make_registry, sqlite_path, monkeypatch):
    monkeypatch.setenv("T2S_ALLOW_FILE_DATABASES", "true")
    calls = []
    real_get_schema = SQLiteConnector.get_schema

    def slow_get_schema(self, connection):
        calls.append(threading.get_ident())
        time.sleep(0.1)                        # introspection is slow
        return real_get_schema(self, connection)

    monkeypatch.setattr(SQLiteConnector, "get_schema", slow_get_schema)
    registry = make_registry(manager_factory=None)
    cid = registry.register("t", "db", "sqlite", {"db_path": sqlite_path}).id

    barrier = threading.Barrier(16)
    results, errors = [], []

    def worker():
        try:
            barrier.wait()
            results.append(registry.get_schema("t", cid))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(calls) == 1
    assert len(results) == 16 and all(r is results[0] for r in results)
    assert "items" in results[0]

    registry.get_schema("t", cid)
    assert len(calls) == 1                     # cached
    registry.get_schema("t", cid, refresh=True)
    assert len(calls) == 2


def test_database_manager_dispose_waits_for_in_flight_work(sqlite_path):
    manager = DatabaseManager(db_type="sqlite", db_path=sqlite_path)
    disposed = []
    manager._connector.dispose = lambda: disposed.append(1)

    assert not manager.in_use
    with manager._busy():
        assert manager.in_use
        assert manager.dispose_if_idle() is False       # eviction is refused
        manager.dispose()                                # deletion is deferred
        assert disposed == []
    assert disposed == [1] and not manager.in_use        # ...until the work is done
    assert manager.dispose_if_idle() is True
    assert disposed == [1, 1]


def test_real_manager_not_evicted_while_query_runs_and_usable_after_dispose(
        make_registry, sqlite_path, monkeypatch):
    monkeypatch.setenv("T2S_ALLOW_FILE_DATABASES", "true")
    registry = make_registry(manager_factory=None, max_open=1)
    a = registry.register("t", "a", "sqlite", {"db_path": sqlite_path}).id
    b = registry.register("t", "b", "sqlite", {"db_path": sqlite_path}).id
    manager = registry.get_manager("t", a)
    disposals = []
    real_dispose = manager._connector.dispose
    manager._connector.dispose = lambda: (disposals.append(1), real_dispose())

    started, release = threading.Event(), threading.Event()
    real_execute = SQLiteConnector.execute_query_limited

    def blocking_execute(self, connection, sql, max_rows=None):
        started.set()
        assert release.wait(5)
        return real_execute(self, connection, sql, max_rows)

    monkeypatch.setattr(SQLiteConnector, "execute_query_limited", blocking_execute)
    out = []
    thread = threading.Thread(
        target=lambda: out.append(manager.execute_query("SELECT owner, n FROM items")))
    thread.start()
    assert started.wait(5)
    registry.get_manager("t", b)               # would evict `a`, but it is running a query
    assert disposals == [] and registry.open_count == 2
    release.set()
    thread.join(5)
    assert out == [[{"owner": "a", "n": 1}]]

    monkeypatch.setattr(SQLiteConnector, "execute_query_limited", real_execute)
    registry.get_manager("t", b)               # now idle: evicted and closed
    assert disposals == [1] and registry.open_count == 1
    # a caller still holding the evicted manager gets a correct answer (lazy reopen)
    assert manager.execute_query("SELECT n FROM items") == [{"n": 1}]


def test_connector_dispose_closes_and_reopens(sqlite_path, tmp_path):
    connector = SQLiteConnector(db_path=sqlite_path)
    shared = connector.connect()
    connector.dispose()
    connector.dispose()                         # idempotent
    with pytest.raises(sqlite3.ProgrammingError):
        shared.execute("SELECT 1")              # really closed
    assert connector.test_connection()          # reopens lazily

    duckdb = pytest.importorskip("duckdb")
    path = str(tmp_path / "d.duckdb")
    conn = duckdb.connect(path)
    conn.execute("CREATE TABLE t (n INTEGER)")
    conn.execute("INSERT INTO t VALUES (5)")
    conn.close()
    manager = DatabaseManager(db_type="duckdb", db_path=path)
    assert manager.execute_query("SELECT n FROM t") == [{"n": 5}]
    manager.dispose()
    writer = duckdb.connect(path)               # the file lock was released
    writer.close()
    assert manager.execute_query("SELECT n FROM t") == [{"n": 5}]
    manager.dispose()
