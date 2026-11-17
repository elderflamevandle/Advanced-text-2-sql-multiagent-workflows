"""API keys, tenant auth and the admin API."""
import hashlib

import sqlalchemy as sa
from fastapi.testclient import TestClient

from tests.api.conftest import ADMIN, make_app, new_tenant


def _error(response):
    return response.json()["error"]


def test_missing_and_invalid_keys_get_the_same_generic_401(client):
    new_tenant(client)
    missing = client.get("/v1/connections")
    invalid = client.get("/v1/connections", headers={"X-API-Key": "t2s_" + "x" * 43})
    malformed = client.get("/v1/connections", headers={"X-API-Key": "nope"})
    for response in (missing, invalid, malformed):
        assert response.status_code == 401
        assert _error(response)["type"] == "unauthorized"
    assert _error(missing)["message"] == _error(invalid)["message"] == \
        _error(malformed)["message"]


def test_valid_key_authenticates_and_updates_last_used(client):
    tenant_id, headers, key_id = new_tenant(client)
    assert client.get("/v1/connections", headers=headers).status_code == 200
    tenants = client.get("/v1/admin/tenants", headers=ADMIN).json()["tenants"]
    entry = next(t for t in tenants if t["id"] == tenant_id)
    assert entry["keys"][0]["id"] == key_id
    assert entry["keys"][0]["last_used_at"] is not None


def test_key_is_shown_once_and_stored_only_as_a_hash(client):
    from api.auth import api_keys_table
    from database.metadata import get_engine

    created = client.post("/v1/admin/tenants", json={"name": "acme"}, headers=ADMIN).json()
    key = created["api_key"]["key"]
    assert key.startswith("t2s_") and len(key) >= 4 + 43
    assert created["api_key"]["prefix"] == key[:12]

    listing = client.get("/v1/admin/tenants", headers=ADMIN)
    assert key not in listing.text
    assert "key" not in listing.json()["tenants"][0]["keys"][0]

    with get_engine().connect() as conn:
        rows = conn.execute(sa.select(api_keys_table)).all()
    assert len(rows) == 1
    assert rows[0].key_hash == hashlib.sha256(key.encode()).hexdigest()
    assert all(key not in str(value) for value in rows[0])


def test_additional_key_and_revocation(client):
    tenant_id, first, first_id = new_tenant(client)
    second = client.post(f"/v1/admin/tenants/{tenant_id}/keys", headers=ADMIN)
    assert second.status_code == 201
    second_headers = {"X-API-Key": second.json()["key"]}
    assert client.get("/v1/connections", headers=second_headers).status_code == 200

    assert client.delete(f"/v1/admin/keys/{first_id}", headers=ADMIN).status_code == 204
    assert client.get("/v1/connections", headers=first).status_code == 401
    assert client.get("/v1/connections", headers=second_headers).status_code == 200
    # Revoking again is a no-op; an unknown id is 404.
    assert client.delete(f"/v1/admin/keys/{first_id}", headers=ADMIN).status_code == 204
    assert client.delete("/v1/admin/keys/unknown", headers=ADMIN).status_code == 404
    assert client.post("/v1/admin/tenants/unknown/keys", headers=ADMIN).status_code == 404


def test_admin_requires_the_admin_key(client):
    _, tenant_headers, _ = new_tenant(client)
    assert client.get("/v1/admin/tenants").status_code == 401
    assert client.get("/v1/admin/tenants", headers={"X-Admin-Key": "wrong"}).status_code == 401
    # A tenant key is not an admin key.
    assert client.get("/v1/admin/tenants", headers={
        "X-Admin-Key": tenant_headers["X-API-Key"]}).status_code == 401
    assert client.post("/v1/admin/tenants", json={"name": ""}, headers=ADMIN).status_code == 422


def test_admin_routes_are_404_when_the_admin_key_is_unset(monkeypatch):
    monkeypatch.delenv("T2S_ADMIN_API_KEY")
    client = TestClient(make_app())
    calls = [
        client.post("/v1/admin/tenants", json={"name": "x"}, headers=ADMIN),
        client.get("/v1/admin/tenants", headers=ADMIN),
        client.post("/v1/admin/tenants/abc/keys", headers=ADMIN),
        client.delete("/v1/admin/keys/abc", headers=ADMIN),
        client.get("/v1/admin/tenants", headers={"X-Admin-Key": ""}),
    ]
    for response in calls:
        assert response.status_code == 404
        assert _error(response)["type"] == "not_found"


def test_empty_admin_key_env_also_disables_admin(monkeypatch):
    monkeypatch.setenv("T2S_ADMIN_API_KEY", "  ")
    client = TestClient(make_app())
    assert client.get("/v1/admin/tenants", headers={"X-Admin-Key": "  "}).status_code == 404
