"""Tenant isolation end to end: real ConnectionRegistry, real graph, fake LLM.

Two tenants each own a SQLite database with a table of the same name but different rows.
Both fire many concurrent queries; no response may contain the other tenant's rows.
"""
import asyncio
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import FakeRateLimiter, make_app, new_tenant
from tests.api.test_query_graph import REPLIES, install_fake_llm

QUERIES_PER_TENANT = 24
SQL = "SELECT id, owner FROM items ORDER BY id"


def _make_db(path, owner, count):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, owner TEXT NOT NULL)")
    conn.executemany("INSERT INTO items (id, owner) VALUES (?, ?)",
                     [(i, owner) for i in range(1, count + 1)])
    conn.commit()
    conn.close()


@pytest.fixture
def real_registry(monkeypatch):
    from database.registry import ConnectionRegistry

    monkeypatch.setenv("T2S_ALLOW_FILE_DATABASES", "1")
    monkeypatch.delenv("T2S_FILE_DATABASE_ROOT", raising=False)
    try:
        registry = ConnectionRegistry()
        registry.list("probe")
    except NotImplementedError:
        pytest.skip("database.registry.ConnectionRegistry is still the interface stub")
    yield registry
    registry.close_all()


def test_concurrent_queries_only_ever_see_their_own_tenants_rows(
        real_registry, monkeypatch, tmp_path):
    import agents.nodes.hitl as hitl
    from graph.builder import build_graph

    replies = dict(REPLIES, sql_generator=f"SQL:\n{SQL}\nEXPLANATION:\nAll items.")
    install_fake_llm(monkeypatch, replies)
    monkeypatch.setattr(hitl, "_load_hitl_config", lambda: {"enabled": False})

    app = make_app(registry=real_registry, rate_limiter=FakeRateLimiter(),
                   graph=build_graph(), max_concurrent_queries=QUERIES_PER_TENANT)
    client = TestClient(app, raise_server_exceptions=False)

    tenants = {}
    for name, count in (("alpha", 3), ("beta", 5)):
        path = tmp_path / f"{name}.db"
        _make_db(path, name, count)
        _, headers, _ = new_tenant(client, name)
        created = client.post("/v1/connections", headers=headers, json={
            "name": "main", "db_type": "sqlite", "params": {"db_path": str(path)}})
        assert created.status_code == 201, created.text
        tenants[name] = {"headers": headers, "connection_id": created.json()["id"],
                         "count": count}

    alpha, beta = tenants["alpha"], tenants["beta"]

    # Using the other tenant's connection id is a plain 404, in both directions.
    for mine, theirs in ((alpha, beta), (beta, alpha)):
        for path in ("/v1/query", "/v1/query/stream"):
            crossed = client.post(path, headers=mine["headers"], json={
                "connection_id": theirs["connection_id"], "question": "list items"})
            assert crossed.status_code == 404
            assert crossed.json()["error"]["type"] == "not_found"
        assert client.get(f"/v1/connections/{theirs['connection_id']}/schema",
                          headers=mine["headers"]).status_code == 404

    async def fire_all():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     timeout=60) as http:
            async def ask(name, index):
                tenant = tenants[name]
                # Both tenants deliberately reuse the same client-side thread ids.
                response = await http.post("/v1/query", headers=tenant["headers"], json={
                    "connection_id": tenant["connection_id"], "question": "list items",
                    "thread_id": f"thread-{index}"})
                return name, response

            jobs = [ask(name, i) for i in range(QUERIES_PER_TENANT) for name in tenants]
            return await asyncio.gather(*jobs)

    results = asyncio.run(fire_all())
    assert len(results) == 2 * QUERIES_PER_TENANT
    for name, response in results:
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "completed", body
        assert body["row_count"] == tenants[name]["count"]
        assert {row["owner"] for row in body["rows"]} == {name}
        assert [row["id"] for row in body["rows"]] == list(range(1, tenants[name]["count"] + 1))
