"""Catalog endpoints, feedback, auto-build, cleanup and clarification through the API."""
import pytest
from fastapi.testclient import TestClient

from tests.agents.phase3_fakes import FakeCatalog, FakeMemory, items_context
from tests.api.conftest import make_app, new_connection, new_tenant
from tests.api.test_query_graph import REPLIES, install_fake_llm


@pytest.fixture
def catalog():
    return FakeCatalog(items_context())


@pytest.fixture
def memory():
    return FakeMemory()


@pytest.fixture
def api(catalog, memory, registry, limiter, stub_graph):
    app = make_app(registry=registry, rate_limiter=limiter, graph=stub_graph,
                   catalog=catalog, memory=memory)
    return TestClient(app, raise_server_exceptions=False)


def _real_graph_client(monkeypatch, registry, replies=None, **collaborators):
    import agents.nodes.hitl as hitl
    from graph.builder import build_graph

    install_fake_llm(monkeypatch, dict(REPLIES, **(replies or {})))
    monkeypatch.setattr(hitl, "_load_hitl_config", lambda: {"enabled": False})
    app = make_app(registry=registry, graph=build_graph(), **collaborators)
    return TestClient(app, raise_server_exceptions=False)


# -- registration and deletion -----------------------------------------------------

def test_registration_starts_a_build_and_delete_cleans_up(api, catalog, memory):
    tenant_id, headers, _ = new_tenant(api)
    connection_id = new_connection(api, headers)
    assert catalog.builds == [(tenant_id, connection_id, False)]

    assert api.delete(f"/v1/connections/{connection_id}", headers=headers).status_code == 204
    assert catalog.deleted == [(tenant_id, connection_id)]
    assert memory.deleted == [(tenant_id, connection_id)]


def test_auto_build_can_be_switched_off(api, catalog, monkeypatch):
    import api.routes.connections as connections

    monkeypatch.setattr(connections, "_auto_build_enabled", lambda: False)
    _, headers, _ = new_tenant(api)
    new_connection(api, headers)
    assert catalog.builds == []


def test_catalog_failures_never_fail_registration_or_deletion(registry, limiter, stub_graph):
    broken = FakeCatalog(error=RuntimeError("catalog down"))
    broken.delete = lambda *a: (_ for _ in ()).throw(RuntimeError("down"))
    memory = FakeMemory()
    memory.delete_connection = lambda *a: (_ for _ in ()).throw(RuntimeError("down"))
    client = TestClient(make_app(registry=registry, rate_limiter=limiter, graph=stub_graph,
                                 catalog=broken, memory=memory),
                        raise_server_exceptions=False)
    _, headers, _ = new_tenant(client)
    connection_id = new_connection(client, headers)  # asserts 201
    assert client.delete(f"/v1/connections/{connection_id}", headers=headers).status_code == 204


def test_registration_works_without_a_catalog(client):
    _, headers, _ = new_tenant(client)
    connection_id = new_connection(client, headers)
    assert client.delete(f"/v1/connections/{connection_id}", headers=headers).status_code == 204


# -- catalog endpoints ---------------------------------------------------------------

def test_catalog_build_status_tables_and_descriptions(api, catalog):
    tenant_id, headers, _ = new_tenant(api)
    connection_id = new_connection(api, headers)
    base = f"/v1/connections/{connection_id}/catalog"

    response = api.post(base, headers=headers, json={"force": True})
    assert response.status_code == 202
    assert response.json()["state"] == "building"
    assert response.json()["connection_id"] == connection_id
    assert catalog.builds[-1] == (tenant_id, connection_id, True)
    assert api.post(base, headers=headers).status_code == 202  # body is optional
    assert catalog.builds[-1] == (tenant_id, connection_id, False)

    status = api.get(base, headers=headers)
    assert status.status_code == 200
    assert status.json() == {"connection_id": connection_id, "state": "building",
                             "table_count": 1, "progress": 0.0, "error": None}

    put = api.put(f"{base}/descriptions", headers=headers,
                  json={"table": "items", "column": "owner", "description": "Login name."})
    assert put.status_code == 204
    assert api.put(f"{base}/descriptions", headers=headers,
                   json={"table": "items", "description": "One row per item."}).status_code == 204
    tables = api.get(f"{base}/tables", headers=headers)
    assert tables.status_code == 200
    table = tables.json()["tables"][0]
    assert table["description"] == "One row per item."
    assert table["columns"][1] == {"name": "owner", "description": "Login name."}

    for body in ({"table": "nope", "description": "x"},
                 {"table": "items", "column": "nope", "description": "x"}):
        missing = api.put(f"{base}/descriptions", headers=headers, json=body)
        assert missing.status_code == 404 and missing.json()["error"]["type"] == "not_found"
    assert api.put(f"{base}/descriptions", headers=headers,
                   json={"table": "items"}).status_code == 422


def test_catalog_endpoints_are_tenant_scoped(api, catalog):
    _, alice, _ = new_tenant(api, "alice")
    _, bob, _ = new_tenant(api, "bob")
    connection_id = new_connection(api, alice)
    builds = list(catalog.builds)
    base = f"/v1/connections/{connection_id}/catalog"
    assert api.post(base, headers=bob).status_code == 404
    assert api.get(base, headers=bob).status_code == 404
    assert api.get(f"{base}/tables", headers=bob).status_code == 404
    assert api.put(f"{base}/descriptions", headers=bob,
                   json={"table": "items", "description": "x"}).status_code == 404
    assert api.get("/v1/connections/unknown/catalog", headers=alice).status_code == 404
    assert api.get(base).status_code == 401
    assert catalog.builds == builds and catalog.descriptions == {}


def test_catalog_endpoints_answer_503_without_a_catalog(client):
    _, headers, _ = new_tenant(client)
    connection_id = new_connection(client, headers)
    base = f"/v1/connections/{connection_id}/catalog"
    for response in (client.post(base, headers=headers), client.get(base, headers=headers),
                     client.get(f"{base}/tables", headers=headers)):
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "catalog_unavailable"
    feedback = client.post("/v1/queries/q1/feedback", headers=headers, json={"rating": "up"})
    assert feedback.status_code == 503


# -- queries: no schema in state, query id, feedback -----------------------------------

def test_initial_state_carries_no_schema(api, stub_graph):
    _, headers, _ = new_tenant(api)
    connection_id = new_connection(api, headers)
    response = api.post("/v1/query", headers=headers,
                        json={"connection_id": connection_id, "question": "list the items"})
    assert response.status_code == 200
    initial = stub_graph.calls[0][1]
    assert initial["schema"] is None and initial["connection_id"] == connection_id
    assert initial["reselect_count"] == 0 and initial["schema_context"] is None


def test_query_uses_the_catalog_and_returns_a_query_id(monkeypatch, registry, catalog, memory):
    client = _real_graph_client(monkeypatch, registry, catalog=catalog, memory=memory)
    tenant_id, headers, _ = new_tenant(client)
    connection_id = new_connection(client, headers)
    response = client.post("/v1/query", headers=headers, json={
        "connection_id": connection_id, "question": "list the items", "thread_id": "th1"})
    body = response.json()
    assert response.status_code == 200 and body["status"] == "completed", body
    assert body["query_id"] == "q1" and body["tables_used"] == ["items"]
    assert body["clarification_question"] is None
    assert body["rows"] == registry.rows
    assert catalog.select_calls[0]["tenant_id"] == tenant_id
    assert memory.logged[0]["thread_id"] == "th1", "the tenant prefix is not stored"
    assert memory.logged[0]["tables"] == ("items",)


def test_full_query_works_without_catalog_and_memory(monkeypatch, registry):
    client = _real_graph_client(monkeypatch, registry)
    _, headers, _ = new_tenant(client)
    connection_id = new_connection(client, headers)
    body = client.post("/v1/query", headers=headers, json={
        "connection_id": connection_id, "question": "list the items"}).json()
    assert body["status"] == "completed" and body["rows"] == registry.rows
    assert body["query_id"] is None and body["tables_used"] == ["items"]


def test_feedback(monkeypatch, registry, catalog, memory, limiter):
    client = _real_graph_client(monkeypatch, registry, catalog=catalog, memory=memory,
                                rate_limiter=limiter)
    alice_id, alice, _ = new_tenant(client, "alice")
    _, bob, _ = new_tenant(client, "bob")
    connection_id = new_connection(client, alice)
    query_id = client.post("/v1/query", headers=alice, json={
        "connection_id": connection_id, "question": "list the items"}).json()["query_id"]
    url = f"/v1/queries/{query_id}/feedback"

    ok = client.post(url, headers=alice, json={
        "rating": "down", "comment": "wrong column", "corrected_sql": "SELECT id FROM items"})
    assert ok.status_code == 204
    assert memory.feedback == [{"tenant_id": alice_id, "query_id": query_id, "rating": "down",
                                "comment": "wrong column",
                                "corrected_sql": "SELECT id FROM items"}]

    other = client.post(url, headers=bob, json={"rating": "up"})
    assert other.status_code == 404 and other.json()["error"]["type"] == "not_found"
    assert client.post("/v1/queries/nope/feedback", headers=alice,
                       json={"rating": "up"}).status_code == 404
    unsafe = client.post(url, headers=alice,
                         json={"rating": "down", "corrected_sql": "DROP TABLE items"})
    assert unsafe.status_code == 422
    assert client.post(url, headers=alice, json={"rating": "meh"}).status_code == 422
    assert client.post(url, headers=alice,
                       json={"rating": "up", "comment": "x" * 2001}).status_code == 422
    assert client.post(url, json={"rating": "up"}).status_code == 401
    assert len(memory.feedback) == 1

    limiter.deny_keys[f"tenant:{alice_id}"] = 3.0
    limited = client.post(url, headers=alice, json={"rating": "up"})
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "3"


# -- clarification -------------------------------------------------------------------

CLARIFY = ('{"select": [], "from": "items", "needs_clarification": true, '
           '"clarification_question": "Do you mean open or closed items?"}')


def test_api_returns_needs_clarification(monkeypatch, registry):
    import agents.nodes.query_planner as planner

    monkeypatch.setattr(planner, "_clarification_enabled", lambda: True)
    client = _real_graph_client(monkeypatch, registry, {"query_planner": CLARIFY})
    _, headers, _ = new_tenant(client)
    connection_id = new_connection(client, headers)
    body = client.post("/v1/query", headers=headers, json={
        "connection_id": connection_id, "question": "list the items"}).json()
    assert body["status"] == "needs_clarification"
    assert body["clarification_question"] == "Do you mean open or closed items?"
    assert body["answer"] == body["clarification_question"]
    assert body["sql"] is None and body["rows"] is None and body["error"] is None
    assert registry.executed == []


def test_api_ignores_clarification_when_disabled(monkeypatch, registry):
    import agents.nodes.query_planner as planner

    monkeypatch.setattr(planner, "_clarification_enabled", lambda: False)
    client = _real_graph_client(monkeypatch, registry, {"query_planner": CLARIFY})
    _, headers, _ = new_tenant(client)
    connection_id = new_connection(client, headers)
    body = client.post("/v1/query", headers=headers, json={
        "connection_id": connection_id, "question": "list the items"}).json()
    assert body["status"] == "completed" and body["clarification_question"] is None
    assert len(registry.executed) == 1
