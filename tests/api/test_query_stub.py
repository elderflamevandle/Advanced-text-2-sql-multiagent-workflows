"""Query endpoints against a stub graph: envelopes, limits, SSE, thread ownership, ops."""
import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import (FakeRateLimiter, StubGraph, make_app, new_connection,
                                new_tenant, parse_sse)


def _setup(client, name="acme"):
    tenant_id, headers, _ = new_tenant(client, name)
    return tenant_id, headers, new_connection(client, headers)


def _ask(client, headers, connection_id, **extra):
    return client.post("/v1/query", headers=headers,
                       json={"connection_id": connection_id, "question": "list items", **extra})


# -- happy path and state ------------------------------------------------------

def test_query_json_happy_path(client, stub_graph, limiter):
    tenant_id, headers, connection_id = _setup(client)
    response = _ask(client, headers, connection_id)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"thread_id", "status", "answer", "sql", "sql_explanation", "rows",
                         "row_count", "truncated", "error", "usage", "pending_approval",
                         "query_id", "clarification_question", "tables_used"}
    assert body["status"] == "completed" and body["error"] is None
    assert body["rows"] == [{"id": 1, "owner": "a"}] and body["row_count"] == 1
    assert body["sql"] == StubGraph.SQL and body["answer"] == "1 row(s)."
    assert body["usage"] == {"total_tokens": 15, "estimated_cost_usd": 0.001, "calls": 1}
    assert body["pending_approval"] is None
    assert limiter.calls == [(f"tenant:{tenant_id}", 60, 60)]

    internal_thread, state = stub_graph.calls[0]
    assert internal_thread == f"{tenant_id}:{body['thread_id']}"
    assert state["user_query"] == "list items" and state["db_type"] == "sqlite"
    assert state["tenant_id"] == tenant_id and state["connection_id"] == connection_id
    # The schema is not copied into the state: the schema linker loads what it needs.
    assert state["schema"] is None
    # Per-query resets, as the Streamlit UI does.
    assert state["usage_metadata"] is None and state["retry_count"] == 0
    for field in ("generated_sql", "error_log", "approval_status", "final_answer",
                  "db_results", "execution_metadata", "query_plan", "correction_plan"):
        assert state[field] is None
    # Checkpointed state holds plain data only.
    json.dumps(state)
    assert "password" not in json.dumps(state).lower()


def test_rows_are_capped_with_row_count_and_truncated(registry, stub_graph):
    stub_graph.rows = [{"id": i, "owner": "a"} for i in range(7)]
    client = TestClient(make_app(registry=registry, graph=stub_graph, max_result_rows=3))
    _, headers, connection_id = _setup(client)
    body = _ask(client, headers, connection_id).json()
    assert len(body["rows"]) == 3 and body["row_count"] == 7 and body["truncated"] is True


def test_request_validation(client):
    _, headers, connection_id = _setup(client)
    too_long = client.post("/v1/query", headers=headers, json={
        "connection_id": connection_id, "question": "x" * 4001})
    assert too_long.status_code == 422
    assert "4000" in too_long.json()["error"]["message"]
    assert client.post("/v1/query", headers=headers, json={
        "connection_id": connection_id, "question": "  "}).status_code == 422
    assert client.post("/v1/query", headers=headers, json={
        "connection_id": connection_id, "question": "q", "extra": 1}).status_code == 422
    for bad in ("a:b", "../x", "x" * 65, "", "tenant:thread", "a b"):
        response = _ask(client, headers, connection_id, thread_id=bad)
        assert response.status_code == 422, bad
    assert client.post("/v1/threads/a:b/approval", headers=headers,
                       json={"decision": "approve"}).status_code == 422
    assert client.post("/v1/query", json={"connection_id": "x", "question": "q"}
                       ).status_code == 401


# -- SSE -----------------------------------------------------------------------

def test_stream_emits_stages_then_result(client):
    _, headers, connection_id = _setup(client)
    response = client.post("/v1/query/stream", headers=headers,
                           json={"connection_id": connection_id, "question": "list items"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert [e for e, _ in events] == ["stage", "stage", "stage", "stage", "result"]
    assert [d["node"] for e, d in events if e == "stage"] == [
        "gatekeeper", "sql_generator", "executor", "formatter"]
    assert events[0][1]["label"] == "Validating query"
    result = events[-1][1]
    assert result["status"] == "completed" and result["rows"] == [{"id": 1, "owner": "a"}]


def test_stream_error_event_and_heartbeat(registry):
    graph = StubGraph()
    graph.delay = 0.2
    graph.raise_error = RuntimeError("boom: postgresql://user:hunter2@host/db")
    client = TestClient(make_app(registry=registry, graph=graph, sse_heartbeat_seconds=0.05))
    _, headers, connection_id = _setup(client)
    response = client.post("/v1/query/stream", headers=headers,
                           json={"connection_id": connection_id, "question": "list items"})
    assert ": heartbeat\n\n" in response.text
    events = parse_sse(response.text)
    assert [e for e, _ in events] == ["stage", "error"]
    error = events[-1][1]["error"]
    assert error["type"] == "internal_error"
    assert error["request_id"] == response.headers["x-request-id"]
    assert "hunter2" not in response.text and "boom" not in response.text
    tenant_id = next(iter(graph.threads)).split(":")[0]
    assert client.app.state.admission.in_flight(tenant_id) == 0


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
def test_stream_stops_the_run_when_the_client_disconnects(registry, spec_version):
    graph = StubGraph()
    app = make_app(registry=registry, graph=graph, sse_heartbeat_seconds=0.05)
    tenant_id, headers, connection_id = _setup(TestClient(app))

    async def scenario():
        graph.hold = asyncio.Event()  # never set: the run would hang forever
        body = json.dumps({"connection_id": connection_id, "question": "q"}).encode()
        requests = [{"type": "http.request", "body": body, "more_body": False}]
        gone = asyncio.Event()
        sent = []

        async def receive():
            if requests:
                return requests.pop(0)
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if gone.is_set() and spec_version == "2.4":
                raise OSError("client disconnected")
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": spec_version},
            "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/v1/query/stream", "raw_path": b"/v1/query/stream", "query_string": b"",
            "root_path": "", "client": ("test", 1), "server": ("test", 80),
            "headers": [(b"content-type", b"application/json"),
                        (b"x-api-key", headers["X-API-Key"].encode())],
        }
        task = asyncio.create_task(app(scope, receive, send))
        for _ in range(200):
            if graph.started and any(b"gatekeeper" in m.get("body", b"") for m in sent):
                break
            await asyncio.sleep(0.01)
        assert graph.started == 1 and app.state.admission.in_flight(tenant_id) == 1
        gone.set()
        await asyncio.wait_for(task, timeout=5)
        for _ in range(200):
            if graph.cancelled:
                break
            await asyncio.sleep(0.01)
        assert graph.cancelled == 1
        assert app.state.admission.in_flight(tenant_id) == 0

    asyncio.run(scenario())


# -- limits ----------------------------------------------------------------------

def test_rate_limit_returns_429_with_retry_after(client, limiter, stub_graph):
    tenant_id, headers, connection_id = _setup(client)
    _, other_headers, other_connection = _setup(client, "other")
    limiter.deny_keys[f"tenant:{tenant_id}"] = 12.3
    for path, payload in (
        ("/v1/query", {"connection_id": connection_id, "question": "q"}),
        ("/v1/query/stream", {"connection_id": connection_id, "question": "q"}),
        ("/v1/threads/t1/approval", {"decision": "approve"}),
    ):
        response = client.post(path, headers=headers, json=payload)
        assert response.status_code == 429, path
        assert response.headers["retry-after"] == "13"
        assert response.json()["error"]["type"] == "rate_limited"
    assert stub_graph.calls == []  # nothing ran
    # Limits are per tenant.
    assert _ask(client, other_headers, other_connection).status_code == 200


def test_rate_limit_uses_the_configured_budget(monkeypatch, registry):
    monkeypatch.setenv("T2S_RATE_LIMIT_PER_MINUTE", "7")
    limiter = FakeRateLimiter()
    from api.main import create_app
    client = TestClient(create_app(registry=registry, rate_limiter=limiter, graph=StubGraph()))
    tenant_id, headers, connection_id = _setup(client)
    assert _ask(client, headers, connection_id).status_code == 200
    assert limiter.calls == [(f"tenant:{tenant_id}", 7, 60)]


def test_in_flight_cap_and_busy_thread(registry):
    graph = StubGraph()
    app = make_app(registry=registry, graph=graph, max_concurrent_queries=2)
    _, headers, connection_id = _setup(TestClient(app))
    _, other_headers, other_connection = _setup(TestClient(app), "other")

    async def scenario():
        graph.hold = asyncio.Event()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            def ask(hdrs, conn, **extra):
                return http.post("/v1/query", headers=hdrs, json={
                    "connection_id": conn, "question": "q", **extra})

            running = [asyncio.create_task(ask(headers, connection_id, thread_id="t1")),
                       asyncio.create_task(ask(headers, connection_id, thread_id="t2"))]
            for _ in range(200):
                if graph.started == 2:
                    break
                await asyncio.sleep(0.01)
            assert graph.started == 2

            over = await ask(headers, connection_id, thread_id="t3")
            assert over.status_code == 429
            assert over.json()["error"]["type"] == "too_many_concurrent_queries"
            assert "retry-after" in over.headers

            # Another tenant is not affected; the same thread cannot run twice at once.
            other = asyncio.create_task(ask(other_headers, other_connection, thread_id="t1"))
            for _ in range(200):
                if graph.started == 3:
                    break
                await asyncio.sleep(0.01)
            busy = await ask(other_headers, other_connection, thread_id="t1")
            assert busy.status_code == 409 and busy.json()["error"]["type"] == "thread_busy"

            graph.hold.set()
            done = await asyncio.gather(*running, other)
            assert [r.status_code for r in done] == [200, 200, 200]
            graph.hold = None
            assert (await ask(headers, connection_id, thread_id="t3")).status_code == 200

    asyncio.run(scenario())


def test_request_timeout_is_504_and_frees_the_slot(registry):
    graph = StubGraph()
    graph.delay = 5
    app = make_app(registry=registry, graph=graph, request_timeout_seconds=0.1)
    client = TestClient(app)
    tenant_id, headers, connection_id = _setup(client)
    response = _ask(client, headers, connection_id)
    assert response.status_code == 504 and response.json()["error"]["type"] == "timeout"
    assert graph.cancelled == 1 and app.state.admission.in_flight(tenant_id) == 0
    stream = client.post("/v1/query/stream", headers=headers,
                         json={"connection_id": connection_id, "question": "q"})
    assert parse_sse(stream.text)[-1][1]["error"]["type"] == "timeout"


# -- thread ownership ------------------------------------------------------------

def test_threads_are_namespaced_per_tenant(client, stub_graph):
    tenant_a, a, conn_a = _setup(client, "a")
    tenant_b, b, conn_b = _setup(client, "b")
    first = _ask(client, a, conn_a, thread_id="shared-thread", require_approval=True).json()
    assert first["status"] == "awaiting_approval" and first["thread_id"] == "shared-thread"
    assert first["pending_approval"] == {"sql": StubGraph.SQL, "explanation": "All items."}
    assert first["rows"] is None and stub_graph.executed == []

    # B cannot approve, reject or edit A's paused run, whatever id it sends.
    for payload in ({"decision": "approve"}, {"decision": "reject"},
                    {"decision": "edit", "sql": "SELECT 1"}):
        denied = client.post("/v1/threads/shared-thread/approval", headers=b, json=payload)
        assert denied.status_code == 404
        assert denied.json()["error"]["type"] == "not_found"
    assert client.post(f"/v1/threads/{tenant_a}:shared-thread/approval", headers=b,
                       json={"decision": "approve"}).status_code in (404, 422)
    assert stub_graph.executed == []

    # B "continuing" the same thread id gets its own, separate thread.
    b_result = _ask(client, b, conn_b, thread_id="shared-thread").json()
    assert b_result["status"] == "completed"
    assert set(stub_graph.threads) == {f"{tenant_a}:shared-thread", f"{tenant_b}:shared-thread"}
    assert stub_graph.threads[f"{tenant_a}:shared-thread"]["next"] == ("hitl",)
    assert stub_graph.threads[f"{tenant_a}:shared-thread"]["values"]["tenant_id"] == tenant_a

    # A can still approve its own run.
    approved = client.post("/v1/threads/shared-thread/approval", headers=a,
                           json={"decision": "approve"})
    assert approved.status_code == 200 and approved.json()["status"] == "completed"
    assert stub_graph.executed[-1] == (f"{tenant_a}:shared-thread", StubGraph.SQL)


def test_approval_preconditions(client, registry):
    _, headers, connection_id = _setup(client)
    unknown = client.post("/v1/threads/nope/approval", headers=headers,
                          json={"decision": "approve"})
    assert unknown.status_code == 404
    done = _ask(client, headers, connection_id, thread_id="t1").json()
    assert done["status"] == "completed"
    not_waiting = client.post("/v1/threads/t1/approval", headers=headers,
                              json={"decision": "approve"})
    assert not_waiting.status_code == 409
    assert not_waiting.json()["error"]["type"] == "not_awaiting_approval"

    _ask(client, headers, connection_id, thread_id="t2", require_approval=True)
    assert client.post("/v1/threads/t2/approval", headers=headers,
                       json={"decision": "edit"}).status_code == 422
    assert client.post("/v1/threads/t2/approval", headers=headers,
                       json={"decision": "maybe"}).status_code == 422
    # The connection was deleted while the run was paused: nothing can be resumed.
    client.delete(f"/v1/connections/{connection_id}", headers=headers)
    assert client.post("/v1/threads/t2/approval", headers=headers,
                       json={"decision": "approve"}).status_code == 404


# -- LLM keys ----------------------------------------------------------------------

def test_per_request_llm_keys_are_scoped_to_the_run_and_never_echoed(client, stub_graph,
                                                                    caplog):
    from llm.context import get_llm_credential

    seen = []
    original = stub_graph.astream

    def spying_astream(*args, **kwargs):
        seen.append((get_llm_credential("groq"), get_llm_credential("openai")))
        return original(*args, **kwargs)

    stub_graph.astream = spying_astream
    _, headers, connection_id = _setup(client)
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            "/v1/query", json={"connection_id": connection_id, "question": "q"},
            headers={**headers, "X-Groq-Api-Key": "gsk_request_key",
                     "X-OpenAI-Api-Key": "sk-request-key"})
        plain = _ask(client, headers, connection_id)
    assert response.status_code == 200 and plain.status_code == 200
    assert seen == [("gsk_request_key", "sk-request-key"), (None, None)]
    assert get_llm_credential("groq") is None
    logged = caplog.text + response.text
    for secret in ("gsk_request_key", "sk-request-key", headers["X-API-Key"]):
        assert secret not in logged


# -- ops ---------------------------------------------------------------------------

def test_request_id_is_propagated_or_generated(client):
    echoed = client.get("/healthz", headers={"X-Request-ID": "req-123"})
    assert echoed.status_code == 200 and echoed.json() == {"status": "ok"}
    assert echoed.headers["x-request-id"] == "req-123"
    generated = client.get("/healthz")
    assert len(generated.headers["x-request-id"]) == 32
    # A malformed id is replaced, not reflected.
    replaced = client.get("/healthz", headers={"X-Request-ID": "bad id\twith spaces"})
    assert replaced.headers["x-request-id"] != "bad id\twith spaces"
    # Error responses carry it in the header and in the envelope.
    error = client.get("/v1/connections", headers={"X-Request-ID": "req-401"})
    assert error.headers["x-request-id"] == "req-401"
    assert error.json()["error"]["request_id"] == "req-401"
    missing = client.get("/nope", headers={"X-Request-ID": "req-404"})
    assert missing.status_code == 404
    assert missing.json()["error"] == {"type": "not_found", "message": "Not Found",
                                       "request_id": "req-404"}


def test_unexpected_exception_gets_a_generic_envelope(client, stub_graph, caplog):
    _, headers, connection_id = _setup(client)
    stub_graph.raise_error = RuntimeError("boom: postgresql://user:hunter2@host/db")
    with caplog.at_level(logging.ERROR, logger="t2s.api"):
        response = client.post("/v1/query", headers={**headers, "X-Request-ID": "req-500"},
                               json={"connection_id": connection_id, "question": "q"})
    assert response.status_code == 500
    assert response.json() == {"error": {"type": "internal_error",
                                         "message": "Internal server error.",
                                         "request_id": "req-500"}}
    assert response.headers["x-request-id"] == "req-500"
    assert "hunter2" not in response.text and "Traceback" not in response.text
    # The details are in the server log, tagged with the request id.
    record = next(r for r in caplog.records if r.name == "t2s.api")
    assert record.request_id == "req-500" and record.exc_info is not None


def test_access_log_line_has_tenant_route_status_latency_and_no_payload(client, caplog):
    tenant_id, headers, connection_id = _setup(client)
    with caplog.at_level(logging.INFO, logger="t2s.access"):
        client.post("/v1/query", headers={**headers, "X-Request-ID": "req-log"},
                    json={"connection_id": connection_id, "question": "top secret question"})
        client.get(f"/v1/connections/{connection_id}", headers=headers)
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "t2s.access"]
    query_line = lines[0]
    assert query_line["request_id"] == "req-log" and query_line["tenant_id"] == tenant_id
    assert query_line["route"] == "/v1/query" and query_line["method"] == "POST"
    assert query_line["status"] == 200 and query_line["latency_ms"] >= 0
    assert lines[1]["route"] == "/v1/connections/{connection_id}"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "top secret question" not in text and headers["X-API-Key"] not in text


def test_readiness(client, app, monkeypatch):
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready", "checks": {
        "metadata_db": "ok", "checkpointer": "ok", "registry": "ok"}}

    app.state.graph = None
    not_ready = client.get("/readyz")
    assert not_ready.status_code == 503
    assert not_ready.json()["checks"]["checkpointer"] == "unavailable"
    _, headers, _ = new_tenant(client)
    assert client.post("/v1/query", headers=headers, json={
        "connection_id": "x", "question": "q"}).status_code == 503

    def broken():
        raise RuntimeError("db down: postgresql://user:hunter2@host/meta")

    monkeypatch.setattr(app.state.auth_store, "ping", broken)
    down = client.get("/readyz")
    assert down.status_code == 503 and down.json()["checks"]["metadata_db"] == "unavailable"
    assert "hunter2" not in down.text
    assert client.get("/healthz").status_code == 200


def test_cors_is_off_by_default_and_configurable(registry):
    preflight = {"Origin": "https://app.example.com", "Access-Control-Request-Method": "POST"}
    default = TestClient(make_app(registry=registry))
    assert "access-control-allow-origin" not in default.get(
        "/healthz", headers={"Origin": "https://app.example.com"}).headers
    assert default.options("/v1/query", headers=preflight).status_code == 405
    enabled = TestClient(make_app(registry=registry, cors_origins="https://app.example.com"))
    allowed = enabled.options("/v1/query", headers=preflight)
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://app.example.com"


def test_openapi_docs_are_served(client):
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/v1/query", "/v1/query/stream", "/v1/threads/{thread_id}/approval",
            "/v1/connections", "/v1/admin/tenants", "/healthz", "/readyz"} <= set(paths)


def test_lifespan_builds_missing_collaborators_and_cleans_up(registry, limiter):
    """Without an injected graph the lifespan opens the checkpointer and builds the graph."""
    pytest.importorskip("graph.checkpointing")
    from api.main import create_app

    app = create_app(registry=registry, rate_limiter=limiter)
    assert app.state.graph is None
    with TestClient(app) as client:
        assert app.state.graph is not None
        assert client.get("/readyz").status_code == 200
        _, headers, _ = new_tenant(client)
        assert client.get("/v1/connections", headers=headers).status_code == 200
    assert app.state.graph is None
    assert registry.closed is False  # an injected registry is left to its owner
