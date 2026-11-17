"""Shared fakes and fixtures for the API tests. Nothing here touches the repo's files:
the metadata store is in-memory SQLite and every collaborator is injected."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from database.registry import ConnectionInfo, ConnectionNotFound, ConnectionRejected

ADMIN_KEY = "admin-test-key"
ADMIN = {"X-Admin-Key": ADMIN_KEY}
SCHEMA = {
    "items": {
        "columns": [{"name": "id", "type": "INTEGER", "nullable": False},
                    {"name": "owner", "type": "TEXT", "nullable": True}],
        "primary_keys": ["id"],
        "foreign_keys": [],
        "sample_rows": [{"id": 1, "owner": "sample"}],
    }
}


@pytest.fixture(autouse=True)
def _service_env(monkeypatch):
    from database import metadata, registry

    monkeypatch.setenv("T2S_METADATA_URL", "sqlite://")
    monkeypatch.setenv("T2S_ADMIN_API_KEY", ADMIN_KEY)
    for name in ("T2S_REDIS_URL", "T2S_CHECKPOINT_URL", "T2S_CORS_ORIGINS",
                 "T2S_RATE_LIMIT_PER_MINUTE", "T2S_MAX_CONCURRENT_QUERIES"):
        monkeypatch.delenv(name, raising=False)
    metadata.reset_engine()
    registry.set_registry(None)
    yield
    registry.set_registry(None)
    metadata.reset_engine()


class FakeManager:
    def __init__(self, rows, executed, error=None):
        self._rows, self._executed, self._error = rows, executed, error

    def execute_query(self, sql, timeout=None, max_rows=None):
        self._executed.append(sql)
        if self._error is not None:
            raise self._error
        return list(self._rows)

    def get_schema(self):
        return SCHEMA


class FakeRegistry:
    """In-memory implementation of the ConnectionRegistry contract."""

    def __init__(self):
        self._items: dict[tuple[str, str], ConnectionInfo] = {}
        self.rows = [{"id": 1, "owner": "a"}, {"id": 2, "owner": "b"}]
        self.executed: list[str] = []
        self.execute_error: Exception | None = None
        self.closed = False

    def register(self, tenant_id, name, db_type, params, *, verify=True):
        if db_type not in ("sqlite", "duckdb", "postgresql", "mysql"):
            raise ConnectionRejected("Unsupported db_type.")
        if params.get("host") == "unreachable":
            raise ConnectionRejected("Could not connect to the database.")
        public = {k: v for k, v in params.items() if k != "password"}
        info = ConnectionInfo(id=uuid.uuid4().hex, tenant_id=tenant_id, name=name,
                              db_type=db_type, params_public=public,
                              created_at=datetime.now(timezone.utc).isoformat())
        self._items[(tenant_id, info.id)] = info
        return info

    def list(self, tenant_id):
        return [i for (t, _), i in self._items.items() if t == tenant_id]

    def get_info(self, tenant_id, connection_id):
        try:
            return self._items[(tenant_id, connection_id)]
        except KeyError:
            raise ConnectionNotFound(connection_id) from None

    def delete(self, tenant_id, connection_id):
        self.get_info(tenant_id, connection_id)
        del self._items[(tenant_id, connection_id)]

    def get_manager(self, tenant_id, connection_id):
        self.get_info(tenant_id, connection_id)
        return FakeManager(self.rows, self.executed, self.execute_error)

    def get_schema(self, tenant_id, connection_id, *, refresh=False):
        self.get_info(tenant_id, connection_id)
        return SCHEMA

    def close_all(self):
        self.closed = True


class FakeRateLimiter:
    def __init__(self):
        self.calls: list[tuple[str, int, float]] = []
        self.deny_keys: dict[str, float] = {}

    async def allow(self, key, limit, window_seconds):
        self.calls.append((key, limit, window_seconds))
        if key in self.deny_keys:
            return False, self.deny_keys[key]
        return True, 0.0


class StubGraph:
    """Minimal stand-in for the compiled graph: gatekeeper -> sql_generator -> [hitl pause]
    -> executor -> formatter, with per-thread state like a checkpointer."""

    SQL = "SELECT id, owner FROM items"

    def __init__(self):
        self.threads: dict[str, dict] = {}
        self.calls: list[tuple[str, object]] = []
        self.executed: list[tuple[str, str]] = []
        self.rows = [{"id": 1, "owner": "a"}]
        self.raise_error: Exception | None = None
        self.delay = 0.0
        self.hold: asyncio.Event | None = None
        self.started = 0
        self.cancelled = 0

    async def astream(self, graph_input, config=None, stream_mode=None, interrupt_before=None):
        thread = config["configurable"]["thread_id"]
        self.calls.append((thread, graph_input))
        if graph_input is not None:
            values = dict(graph_input)
            values["usage_metadata"] = []
            entry = self.threads[thread] = {"values": values, "next": ("gatekeeper",)}
            self.started += 1
            yield {"gatekeeper": {"query_type": "sql"}}
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
                if self.hold is not None:
                    await self.hold.wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            if self.raise_error is not None:
                raise self.raise_error
            values["generated_sql"] = self.SQL
            values["sql_explanation"] = "All items."
            values["usage_metadata"].append({"total_tokens": 15, "estimated_cost_usd": 0.001})
            yield {"sql_generator": {}}
            if interrupt_before and "hitl" in interrupt_before:
                entry["next"] = ("hitl",)
                yield {"__interrupt__": ()}
                return
        else:
            entry = self.threads[thread]
            values = entry["values"]
        if values.get("approval_status") == "rejected":
            values["final_answer"] = "The query was rejected."
        else:
            self.executed.append((thread, values["generated_sql"]))
            values["db_results"] = list(self.rows)
            values["execution_metadata"] = {"row_count": len(self.rows), "truncated": False}
            values["final_answer"] = f"{len(self.rows)} row(s)."
            yield {"executor": {}}
        entry["next"] = ()
        yield {"formatter": {}}

    async def aget_state(self, config):
        entry = self.threads.get(config["configurable"]["thread_id"])
        if entry is None:
            return SimpleNamespace(values={}, next=())
        return SimpleNamespace(values=dict(entry["values"]), next=entry["next"])

    async def aupdate_state(self, config, values, as_node=None):
        entry = self.threads[config["configurable"]["thread_id"]]
        entry["values"].update(values)
        assert as_node == "hitl"
        entry["next"] = ("executor",)


def make_app(*, registry=None, rate_limiter=None, graph=None, catalog=None, memory=None,
             **settings):
    from api.main import create_app
    from api.settings import Settings

    return create_app(
        Settings(**settings),
        registry=registry if registry is not None else FakeRegistry(),
        rate_limiter=rate_limiter if rate_limiter is not None else FakeRateLimiter(),
        graph=graph if graph is not None else StubGraph(),
        catalog=catalog, memory=memory,
    )


def new_tenant(client, name="acme"):
    """Create a tenant through the admin API. Returns (tenant_id, key headers, key id)."""
    response = client.post("/v1/admin/tenants", json={"name": name}, headers=ADMIN)
    assert response.status_code == 201, response.text
    body = response.json()
    return body["tenant"]["id"], {"X-API-Key": body["api_key"]["key"]}, body["api_key"]["id"]


def new_connection(client, headers, name="main"):
    response = client.post("/v1/connections", headers=headers, json={
        "name": name, "db_type": "sqlite", "params": {"db_path": "/data/x.db"}})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln and not ln.startswith(":")]
        if not lines:
            continue
        event = next(ln[len("event: "):] for ln in lines if ln.startswith("event: "))
        data = next(ln[len("data: "):] for ln in lines if ln.startswith("data: "))
        events.append((event, json.loads(data)))
    return events


@pytest.fixture
def registry():
    return FakeRegistry()


@pytest.fixture
def limiter():
    return FakeRateLimiter()


@pytest.fixture
def stub_graph():
    return StubGraph()


@pytest.fixture
def app(registry, limiter, stub_graph):
    return make_app(registry=registry, rate_limiter=limiter, graph=stub_graph)


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _no_real_catalog_or_memory():
    """Tests run without the schema catalog and the query memory unless they inject fakes
    (agents.services.set_*_for_tests, or create_app(catalog=..., memory=...))."""
    from agents import services

    services.set_catalog_service_for_tests(None)
    services.set_memory_service_for_tests(None)
    yield
    services.reset_services_for_tests()
