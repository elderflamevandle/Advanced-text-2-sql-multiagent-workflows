"""Connection CRUD through the registry, and tenant isolation of connection ids."""
from tests.api.conftest import new_connection, new_tenant


def test_connection_crud(client):
    _, headers, _ = new_tenant(client)
    created = client.post("/v1/connections", headers=headers, json={
        "name": "warehouse", "db_type": "PostgreSQL",
        "params": {"host": "db.example.com", "port": 5432, "user": "ro",
                   "password": "s3cret-value", "database": "sales"}})
    assert created.status_code == 201
    body = created.json()
    assert body["db_type"] == "postgresql" and body["name"] == "warehouse"
    assert body["params"] == {"host": "db.example.com", "port": 5432, "user": "ro",
                              "database": "sales"}
    assert "s3cret-value" not in created.text and "tenant_id" not in body

    listing = client.get("/v1/connections", headers=headers).json()["connections"]
    assert [c["id"] for c in listing] == [body["id"]]
    assert client.get(f"/v1/connections/{body['id']}", headers=headers).json() == body

    assert client.delete(f"/v1/connections/{body['id']}", headers=headers).status_code == 204
    assert client.get(f"/v1/connections/{body['id']}", headers=headers).status_code == 404
    assert client.delete(f"/v1/connections/{body['id']}", headers=headers).status_code == 404


def test_rejected_connection_is_422_with_the_registry_message(client):
    _, headers, _ = new_tenant(client)
    response = client.post("/v1/connections", headers=headers, json={
        "name": "bad", "db_type": "postgresql",
        "params": {"host": "unreachable", "password": "s3cret-value"}})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["type"] == "connection_rejected"
    assert error["message"] == "Could not connect to the database."
    assert "s3cret-value" not in response.text


def test_validation_errors_never_echo_the_request_body(client):
    _, headers, _ = new_tenant(client)
    response = client.post("/v1/connections", headers=headers, json={
        "name": "x", "db_type": "postgresql", "params": "password=s3cret-value"})
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "validation_error"
    assert response.json()["error"]["details"][0]["loc"] == ["body", "params"]
    assert "s3cret-value" not in response.text


def test_schema_endpoint_hides_sample_rows_by_default(client):
    _, headers, _ = new_tenant(client)
    connection_id = new_connection(client, headers)
    schema = client.get(f"/v1/connections/{connection_id}/schema", headers=headers).json()
    assert schema["connection_id"] == connection_id
    table = schema["tables"][0]
    assert table["name"] == "items" and table["primary_keys"] == ["id"]
    assert [c["name"] for c in table["columns"]] == ["id", "owner"]
    assert "sample_rows" not in table
    with_samples = client.get(f"/v1/connections/{connection_id}/schema",
                              headers=headers, params={"include_samples": "true"}).json()
    assert with_samples["tables"][0]["sample_rows"] == [{"id": 1, "owner": "sample"}]


def test_other_tenants_connections_are_404_never_403(client):
    _, a, _ = new_tenant(client, "a")
    _, b, _ = new_tenant(client, "b")
    connection_id = new_connection(client, a)

    assert client.get("/v1/connections", headers=b).json()["connections"] == []
    missing = client.get("/v1/connections/does-not-exist", headers=b)
    for response in (
        client.get(f"/v1/connections/{connection_id}", headers=b),
        client.get(f"/v1/connections/{connection_id}/schema", headers=b),
        client.delete(f"/v1/connections/{connection_id}", headers=b),
        client.post("/v1/query", headers=b,
                    json={"connection_id": connection_id, "question": "anything"}),
        client.post("/v1/query/stream", headers=b,
                    json={"connection_id": connection_id, "question": "anything"}),
    ):
        assert response.status_code == 404
        # Indistinguishable from an id that does not exist at all.
        assert response.json()["error"]["type"] == missing.json()["error"]["type"]
        assert response.json()["error"]["message"] == missing.json()["error"]["message"]
    # Still there for its owner.
    assert client.get(f"/v1/connections/{connection_id}", headers=a).status_code == 200
