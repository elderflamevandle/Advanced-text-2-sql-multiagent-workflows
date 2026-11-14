"""Query and approval endpoints against the real graph (fake LLM, fake registry)."""
import sys

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from tests.api.conftest import make_app, new_connection, new_tenant, parse_sse

# langgraph.types.interrupt() inside an async node needs Python 3.11+ (on 3.10 the runnable
# config is not propagated to the node and it raises "Called get_config outside of a runnable
# context"). The per-request breakpoint used for require_approval has no such limit.
needs_node_interrupt = pytest.mark.skipif(False, reason="kept for reference")

SIMPLE_SQL = "SELECT id, owner FROM items"
COMPLEX_SQL = "SELECT a.id FROM items a JOIN items b ON a.id = b.id"

REPLIES = {
    "gatekeeper": '{"category": "sql", "intent": "list items", "response": ""}',
    "query_planner": (
        '{"select": ["id"], "from": "items", "joins": [], "where": [], "group_by": [], '
        '"order_by": [], "limit": null, "ctes": [], "complexity": "simple"}'
    ),
    "sql_generator": f"SQL:\n{SIMPLE_SQL}\nEXPLANATION:\nLists the items.",
    "formatter": "There are two items.",
    "correction_plan": (
        '{"error_category": "syntax_error", "root_cause": "typo", "correction_steps": ["fix"], '
        '"affected_clauses": [], "suggested_changes": {}, "confidence": "high"}'
    ),
    "correction_sql": f"SQL:\n{SIMPLE_SQL}\nEXPLANATION:\nFixed.",
}


def install_fake_llm(monkeypatch, replies):
    """Patch the LLM factory the same way tests/graph/test_graph_e2e.py does."""
    from llm.fallback import FallbackClient
    from llm.usage_tracker import UsageTracker

    def get_llm(node, state=None):
        message = AIMessage(
            content=replies[node],
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        model = GenericFakeChatModel(messages=iter([message] * 4))
        return FallbackClient(model, None, None, UsageTracker(), node_name=node, state=state)

    def no_retriever(*args, **kwargs):
        raise RuntimeError("no vector store")

    monkeypatch.setattr("llm.fallback.get_llm", get_llm)
    monkeypatch.setattr("vector.retriever.get_retriever", no_retriever)


@pytest.fixture
def pipeline(monkeypatch, registry):
    """(client, headers, connection_id, registry, replies, hitl_config) on a real graph."""
    import agents.nodes.hitl as hitl
    from graph.builder import build_graph

    replies = dict(REPLIES)
    hitl_config = {"enabled": False}
    install_fake_llm(monkeypatch, replies)
    monkeypatch.setattr(hitl, "_load_hitl_config", lambda: dict(hitl_config))
    client = TestClient(make_app(registry=registry, graph=build_graph()),
                        raise_server_exceptions=False)
    _, headers, _ = new_tenant(client)
    return client, headers, new_connection(client, headers), registry, replies, hitl_config


def _ask(client, headers, connection_id, **extra):
    response = client.post("/v1/query", headers=headers, json={
        "connection_id": connection_id, "question": "list the items", **extra})
    assert response.status_code == 200, response.text
    return response.json()


def _decide(client, headers, thread_id, **payload):
    response = client.post(f"/v1/threads/{thread_id}/approval", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_happy_path_runs_the_real_pipeline(pipeline):
    client, headers, connection_id, registry, _, _ = pipeline
    body = _ask(client, headers, connection_id)
    assert body["status"] == "completed" and body["error"] is None
    assert body["answer"] == "There are two items."
    assert body["sql"] == SIMPLE_SQL and body["sql_explanation"]
    assert body["rows"] == registry.rows and body["row_count"] == 2
    assert body["truncated"] is False
    assert body["usage"]["calls"] == 4 and body["usage"]["total_tokens"] == 60
    assert len(registry.executed) == 1

    # A second question on the same thread starts from clean per-query state.
    again = _ask(client, headers, connection_id, thread_id=body["thread_id"])
    assert again["usage"]["calls"] == 4 and again["thread_id"] == body["thread_id"]


def test_stream_event_order_on_the_real_pipeline(pipeline):
    client, headers, connection_id, _, _, _ = pipeline
    response = client.post("/v1/query/stream", headers=headers, json={
        "connection_id": connection_id, "question": "list the items"})
    events = parse_sse(response.text)
    assert [d["node"] for e, d in events if e == "stage"] == [
        "gatekeeper", "schema_linker", "query_planner", "sql_generator", "hitl",
        "executor", "formatter", "evaluator"]
    assert events[-1][0] == "result" and [e for e, _ in events].count("result") == 1
    assert events[-1][1]["status"] == "completed"
    assert events[-1][1]["rows"] == [{"id": 1, "owner": "a"}, {"id": 2, "owner": "b"}]


def test_failed_query_has_status_failed_and_an_error(pipeline):
    client, headers, connection_id, registry, _, _ = pipeline
    registry.execute_error = RuntimeError("no such table: items")
    body = _ask(client, headers, connection_id)
    assert body["status"] == "failed" and body["rows"] is None and body["row_count"] == 0
    assert body["error"]["type"] and "no such table" in body["error"]["message"]
    assert set(body["error"]) == {"type", "message"}
    assert body["answer"]  # the formatter still explains the failure
    assert len(registry.executed) >= 1


def test_require_approval_pauses_then_approve_executes(pipeline):
    client, headers, connection_id, registry, _, _ = pipeline
    paused = _ask(client, headers, connection_id, require_approval=True)
    assert paused["status"] == "awaiting_approval" and paused["rows"] is None
    assert paused["pending_approval"] == {"sql": SIMPLE_SQL,
                                          "explanation": paused["sql_explanation"]}
    assert registry.executed == []  # nothing runs before the decision

    done = _decide(client, headers, paused["thread_id"], decision="approve")
    assert done["status"] == "completed" and done["rows"] == registry.rows
    assert done["thread_id"] == paused["thread_id"] and done["pending_approval"] is None
    assert len(registry.executed) == 1 and registry.executed[0].startswith(SIMPLE_SQL)
    assert done["usage"]["calls"] == 4  # three before the pause + the formatter

    # The decision is consumed: a second approval is refused.
    replay = client.post(f"/v1/threads/{paused['thread_id']}/approval", headers=headers,
                         json={"decision": "approve"})
    assert replay.status_code == 409 and len(registry.executed) == 1


def test_reject_never_executes(pipeline):
    client, headers, connection_id, registry, _, _ = pipeline
    paused = _ask(client, headers, connection_id, require_approval=True)
    done = _decide(client, headers, paused["thread_id"], decision="reject")
    assert done["status"] == "failed" and done["error"]["type"] == "user_rejected"
    assert done["rows"] is None and registry.executed == []


def test_edit_runs_the_edited_sql_through_the_executor(pipeline):
    client, headers, connection_id, registry, _, _ = pipeline
    paused = _ask(client, headers, connection_id, require_approval=True)
    edited = "SELECT id FROM items WHERE id = 1"
    done = _decide(client, headers, paused["thread_id"], decision="edit", sql=edited)
    assert done["status"] == "completed" and done["sql"] == edited
    assert len(registry.executed) == 1 and registry.executed[0].startswith(edited)


@pytest.mark.parametrize("edited", [
    "DROP TABLE items",
    "DELETE FROM items",
    "SELECT 1; DROP TABLE items",
])
def test_edited_sql_still_passes_the_safety_scanner(pipeline, edited):
    client, headers, connection_id, registry, _, _ = pipeline
    paused = _ask(client, headers, connection_id, require_approval=True)
    done = _decide(client, headers, paused["thread_id"], decision="edit", sql=edited)
    assert done["status"] == "failed" and done["error"]["type"] == "blocked_query"
    assert done["rows"] is None and registry.executed == []


@needs_node_interrupt
@pytest.mark.parametrize("decision,status,executions", [
    ("approve", "completed", 1), ("reject", "failed", 0), ("edit", "completed", 1)])
def test_server_side_hitl_interrupt_can_be_resolved(pipeline, decision, status, executions):
    """The hitl node's own interrupt() (hitl.enabled in config) uses the same endpoint."""
    client, headers, connection_id, registry, replies, hitl_config = pipeline
    hitl_config.update(enabled=True, auto_approve_simple=True)
    replies["sql_generator"] = f"SQL:\n{COMPLEX_SQL}\nEXPLANATION:\nSelf join."

    paused = _ask(client, headers, connection_id)  # no per-request flag needed
    assert paused["status"] == "awaiting_approval"
    assert paused["pending_approval"]["sql"] == COMPLEX_SQL and registry.executed == []

    payload = {"decision": decision}
    if decision == "edit":
        payload["sql"] = SIMPLE_SQL
    done = _decide(client, headers, paused["thread_id"], **payload)
    assert done["status"] == status and len(registry.executed) == executions
    if decision == "edit":
        assert registry.executed[0].startswith(SIMPLE_SQL)
    if decision == "reject":
        assert done["error"]["type"] == "user_rejected"


@needs_node_interrupt
def test_require_approval_false_cannot_bypass_server_policy(pipeline):
    client, headers, connection_id, registry, replies, hitl_config = pipeline
    hitl_config.update(enabled=True, auto_approve_simple=True)
    replies["sql_generator"] = f"SQL:\n{COMPLEX_SQL}\nEXPLANATION:\nSelf join."
    paused = _ask(client, headers, connection_id, require_approval=False)
    assert paused["status"] == "awaiting_approval" and registry.executed == []


def test_new_question_on_a_paused_thread_replaces_the_pending_run(pipeline):
    client, headers, connection_id, registry, _, _ = pipeline
    paused = _ask(client, headers, connection_id, require_approval=True)
    fresh = _ask(client, headers, connection_id, thread_id=paused["thread_id"])
    assert fresh["status"] == "completed" and len(registry.executed) == 1
    stale = client.post(f"/v1/threads/{paused['thread_id']}/approval", headers=headers,
                        json={"decision": "approve"})
    assert stale.status_code == 409 and len(registry.executed) == 1
