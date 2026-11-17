"""executor_node resolves the database per tenant through the connection registry."""
import asyncio
import logging
import random
import sqlite3

import pytest
import sqlalchemy as sa

import database.manager as manager_module
from agents.nodes.executor import executor_node
from database.credentials import CredentialCipher, generate_key
from database.registry import ConnectionRegistry, set_registry

SQL = "SELECT owner, n FROM items ORDER BY n"
ROWS = {
    "tenant-a": [("alpha", 1), ("alpha", 2), ("alpha", 3)],
    "tenant-b": [("bravo", 10), ("bravo", 20)],
}


def make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (owner TEXT, n INTEGER)")   # same table name in both
    conn.executemany("INSERT INTO items VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return str(path)


def state_for(tenant_id, connection_id, sql=SQL):
    return {
        "user_query": "list items",
        "db_type": "sqlite",
        "generated_sql": sql,
        "tenant_id": tenant_id,
        "connection_id": connection_id,
    }


@pytest.fixture
def tenants(tmp_path, monkeypatch):
    monkeypatch.setenv("T2S_ALLOW_FILE_DATABASES", "true")
    monkeypatch.setattr(manager_module, "_active_manager", None)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    registry = ConnectionRegistry(engine, cipher=CredentialCipher(generate_key()))
    ids = {
        tenant: registry.register(
            tenant, "main", "sqlite", {"db_path": make_db(tmp_path / f"{tenant}.db", rows)}
        ).id
        for tenant, rows in ROWS.items()
    }
    set_registry(registry)
    yield registry, ids
    set_registry(None)
    registry.close_all()
    engine.dispose()


def expected(tenant):
    return [{"owner": owner, "n": n} for owner, n in ROWS[tenant]]


def test_each_tenant_gets_its_own_database(tenants):
    _, ids = tenants
    for tenant in ROWS:
        result = asyncio.run(executor_node(state_for(tenant, ids[tenant])))
        assert result["error_log"] is None
        assert result["db_results"] == expected(tenant)


def test_many_interleaved_concurrent_queries_never_cross_tenants(tenants):
    registry, ids = tenants
    per_tenant = 60
    jobs = [tenant for tenant in ROWS for _ in range(per_tenant)]
    random.Random(7).shuffle(jobs)

    async def one(tenant, i):
        await asyncio.sleep(random.Random(i).random() / 200)     # force interleaving
        return tenant, await executor_node(state_for(tenant, ids[tenant]))

    async def main():
        return await asyncio.gather(*(one(tenant, i) for i, tenant in enumerate(jobs)))

    results = asyncio.run(main())
    assert len(results) == 2 * per_tenant
    for tenant, result in results:
        assert result["error_log"] is None, result["error_log"]
        assert result["db_results"] == expected(tenant)
        assert result["execution_metadata"]["row_count"] == len(ROWS[tenant])
    assert registry.open_count == 2          # one cached manager per connection, not per call


def test_concurrent_queries_with_tiny_lru_still_isolated(tenants, tmp_path):
    """Constant eviction (max_open=1) must not mix tenants up or break running queries."""
    registry, ids = tenants
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    tight = ConnectionRegistry(engine, cipher=registry._cipher, max_open=1)
    set_registry(tight)
    try:
        jobs = [tenant for tenant in ROWS for _ in range(50)]
        random.Random(3).shuffle(jobs)

        async def main():
            return await asyncio.gather(
                *(executor_node(state_for(tenant, ids[tenant])) for tenant in jobs))

        for tenant, result in zip(jobs, asyncio.run(main())):
            assert result["error_log"] is None, result["error_log"]
            assert result["db_results"] == expected(tenant)
    finally:
        set_registry(registry)
        tight.close_all()
        engine.dispose()


def test_foreign_connection_id_gives_no_connection(tenants, caplog):
    _, ids = tenants
    with caplog.at_level(logging.DEBUG):
        foreign = asyncio.run(executor_node(state_for("tenant-a", ids["tenant-b"])))
        missing = asyncio.run(executor_node(state_for("tenant-a", "f" * 32)))
    for result in (foreign, missing):
        assert result["db_results"] is None
        assert result["error_log"]["error_type"] == "no_connection"
    # a foreign id is indistinguishable from one that does not exist
    assert foreign["error_log"] == missing["error_log"]
    assert "bravo" not in str(foreign) and "tenant-b" not in str(foreign)
    assert "tenant-b" not in caplog.text


def test_foreign_ids_interleaved_with_valid_traffic(tenants):
    _, ids = tenants

    async def main():
        good = [executor_node(state_for(t, ids[t])) for t in ROWS for _ in range(25)]
        bad = [executor_node(state_for("tenant-a", ids["tenant-b"])) for _ in range(25)]
        bad += [executor_node(state_for("tenant-b", ids["tenant-a"])) for _ in range(25)]
        return await asyncio.gather(*good), await asyncio.gather(*bad)

    good, bad = asyncio.run(main())
    assert all(r["error_log"] is None and r["db_results"] for r in good)
    assert all(r["db_results"] is None and r["error_log"]["error_type"] == "no_connection"
               for r in bad)


@pytest.mark.parametrize("tenant", [None, ""])
def test_connection_id_without_tenant_gives_no_connection(tenants, tenant):
    _, ids = tenants
    result = asyncio.run(executor_node(state_for(tenant, ids["tenant-a"])))
    assert result["error_log"]["error_type"] == "no_connection"


def test_connection_id_never_falls_back_to_the_legacy_manager(tenants, mocker):
    _, ids = tenants
    legacy = mocker.MagicMock()
    manager_module.set_active_manager(legacy)
    result = asyncio.run(executor_node(state_for("tenant-a", ids["tenant-b"])))
    assert result["error_log"]["error_type"] == "no_connection"
    legacy.execute_query.assert_not_called()


def test_registry_failure_is_reported_as_no_connection_without_details(tenants, caplog):
    registry, ids = tenants
    other = ConnectionRegistry(registry._engine, cipher=CredentialCipher(generate_key()))
    set_registry(other)                      # wrong encryption key
    try:
        with caplog.at_level(logging.DEBUG):
            result = asyncio.run(executor_node(state_for("tenant-a", ids["tenant-a"])))
    finally:
        set_registry(registry)
    # sqlite has no secret to decrypt, but the ciphertext binding is still checked
    assert result["db_results"] is None
    assert result["error_log"]["error_type"] == "no_connection"
    assert "T2S_SECRET_KEY" not in str(result)


def test_legacy_path_without_connection_id_is_unchanged(tenants, mocker):
    legacy = mocker.MagicMock()
    legacy.execute_query.return_value = [{"owner": "legacy", "n": 0}]
    manager_module.set_active_manager(legacy)
    state = state_for(None, None)
    result = asyncio.run(executor_node(state))
    assert result["error_log"] is None
    assert result["db_results"] == [{"owner": "legacy", "n": 0}]
    legacy.execute_query.assert_called_once()

    state.pop("connection_id")
    state.pop("tenant_id")
    assert asyncio.run(executor_node(state))["db_results"] == [{"owner": "legacy", "n": 0}]
