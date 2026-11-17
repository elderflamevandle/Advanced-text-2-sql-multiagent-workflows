"""Runs the LangGraph pipeline for one tenant request and shapes the result.

Thread ownership: the LangGraph thread id is ``f"{tenant_id}:{thread_id}"``. Tenant ids are
server-generated hex and client thread ids match ``[A-Za-z0-9][A-Za-z0-9_-]{0,63}``, so the
colon cannot be forged and a tenant can only ever address checkpoints under its own prefix.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import decimal
import logging
import math
import re
import uuid
from contextlib import contextmanager
from typing import Any, Callable

from api.errors import APIError
from api.limits import Permit, QueryAdmission
from api.schemas import THREAD_ID_PATTERN
from api.settings import Settings

logger = logging.getLogger(__name__)

HITL_NODE = "hitl"
_THREAD_ID_RE = re.compile(THREAD_ID_PATTERN)

NODE_LABELS = {
    "gatekeeper": "Validating query",
    "schema_linker": "Linking schema",
    "query_planner": "Planning query",
    "sql_generator": "Generating SQL",
    "hitl": "Approval check",
    "executor": "Executing SQL",
    "correction_plan_node": "Diagnosing error",
    "correction_sql_node": "Correcting SQL",
    "formatter": "Formatting answer",
    "evaluator": "Evaluating quality",
}


# -- thread ids ---------------------------------------------------------------

def new_thread_id() -> str:
    return uuid.uuid4().hex


def validate_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str) or not _THREAD_ID_RE.match(thread_id):
        raise APIError(422, "validation_error",
                       "thread_id must be 1-64 characters of letters, digits, '_' or '-'.")
    return thread_id


def internal_thread_id(tenant_id: str, thread_id: str) -> str:
    return f"{tenant_id}:{validate_thread_id(thread_id)}"


def graph_config(tenant_id: str, thread_id: str) -> dict:
    return {"configurable": {"thread_id": internal_thread_id(tenant_id, thread_id)}}


# -- admission (rate limit, in-flight cap) ------------------------------------

async def check_rate_limit(rate_limiter, settings: Settings, tenant_id: str) -> None:
    """Per-tenant request rate (shared by queries, approvals and feedback)."""
    allowed, retry_after = await rate_limiter.allow(
        f"tenant:{tenant_id}", settings.rate_limit_per_minute, 60)
    if not allowed:
        seconds = max(1, math.ceil(float(retry_after or 0)))
        raise APIError(429, "rate_limited", "Rate limit exceeded. Retry later.",
                       headers={"Retry-After": str(seconds)})


async def admit(rate_limiter, admission: QueryAdmission, settings: Settings,
                tenant_id: str, thread_id: str) -> Permit:
    """Apply the per-tenant limits. Returns a permit the caller must release."""
    await check_rate_limit(rate_limiter, settings, tenant_id)
    permit, reason = admission.try_acquire(tenant_id, internal_thread_id(tenant_id, thread_id))
    if permit is None:
        if reason == "thread_busy":
            raise APIError(409, "thread_busy", "Another request is running on this thread.")
        raise APIError(429, "too_many_concurrent_queries",
                       "Too many queries are running for this tenant. Retry shortly.",
                       headers={"Retry-After": "1"})
    return permit


# -- LLM credentials ----------------------------------------------------------

_MAX_PROVIDER_KEY_LENGTH = 512


def clean_provider_key(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > _MAX_PROVIDER_KEY_LENGTH or not value.isprintable():
        raise APIError(422, "validation_error", "Invalid LLM provider key header.")
    return value


@contextmanager
def llm_credentials(groq_api_key: str | None, openai_api_key: str | None):
    """Request-scoped provider keys via llm.context (package A); never logged or echoed."""
    if not (groq_api_key or openai_api_key):
        yield
        return
    try:
        from llm.context import use_llm_credentials
    except ImportError:
        logger.warning("Per-request LLM keys were supplied but llm.context is not available; "
                       "using the server's provider configuration")
        yield
        return
    with use_llm_credentials(groq_api_key=groq_api_key, openai_api_key=openai_api_key):
        yield


# -- state --------------------------------------------------------------------

def build_initial_state(question: str, *, tenant_id: str, connection_id: str,
                        db_type: str, schema: dict | None = None) -> dict:
    """Initial AgentState for a new query. Identifiers and plain data only: no credentials,
    no manager objects (state is checkpointed).

    ``schema`` stays None for registered connections: the schema linker loads the tables it
    selects (catalog or registry), so the whole schema is not copied into every checkpoint.
    """
    return {
        "messages": [],
        "user_query": question,
        "db_type": (db_type or "").lower(),
        "schema": schema,
        "tenant_id": tenant_id,
        "connection_id": connection_id,
        "retry_count": 0,
        # Per-query resets (same list as streamlit_app/components/chat.py) so nothing leaks
        # from an earlier query on the same thread.
        "usage_metadata": None,  # reducer field: None resets the accumulated list
        "resolved_query": None,
        "query_type": None,
        "relevant_tables": None,
        "schema_context": None,
        "join_hints": None,
        "examples": None,
        "lessons": None,
        "query_id": None,
        "clarification_question": None,
        "reselect_count": 0,
        "required_tables": None,
        "query_plan": None,
        "generated_sql": None,
        "sql_explanation": None,
        "execution_metadata": None,
        "error_log": None,
        "sql_history": [],
        "correction_plan": None,
        "approval_status": None,
        "final_answer": None,
        "db_results": None,
    }


# -- result shaping -----------------------------------------------------------

def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, decimal.Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(v) for v in value]
    return str(value)


def _error_info(error_log: Any) -> dict:
    if isinstance(error_log, dict):
        return {"type": str(error_log.get("error_type") or "query_failed"),
                "message": str(error_log.get("message") or "The query failed.")}
    return {"type": "query_failed", "message": str(error_log)}


def build_result(snapshot, thread_id: str, max_rows: int) -> dict:
    """Shape a LangGraph state snapshot into the public query response."""
    values = dict(getattr(snapshot, "values", None) or {})
    pending_nodes = tuple(getattr(snapshot, "next", None) or ())
    usage_entries = values.get("usage_metadata") or []
    result: dict[str, Any] = {
        "thread_id": thread_id,
        "status": "completed",
        "answer": values.get("final_answer"),
        "sql": values.get("generated_sql"),
        "sql_explanation": values.get("sql_explanation"),
        "rows": None,
        "row_count": 0,
        "truncated": False,
        "error": None,
        "usage": {
            "total_tokens": int(sum((e.get("total_tokens") or 0) for e in usage_entries)),
            "estimated_cost_usd": round(
                float(sum((e.get("estimated_cost_usd") or 0.0) for e in usage_entries)), 6),
            "calls": len(usage_entries),
        },
        "pending_approval": None,
        "query_id": values.get("query_id"),
        "clarification_question": None,
        "tables_used": (list(values.get("relevant_tables") or [])
                        if values.get("generated_sql") else []),
    }
    if HITL_NODE in pending_nodes:
        result["status"] = "awaiting_approval"
        result["answer"] = None
        result["pending_approval"] = {"sql": values.get("generated_sql"),
                                      "explanation": values.get("sql_explanation")}
        return result
    if pending_nodes:  # stopped somewhere unexpected: never report it as a success
        result["status"] = "failed"
        result["error"] = {"type": "incomplete_run", "message": "The run did not finish."}
        return result
    if values.get("error_log"):
        result["status"] = "failed"
        result["error"] = _error_info(values["error_log"])
        return result

    if values.get("query_type") == "clarification" and values.get("clarification_question"):
        # The planner asked instead of guessing: no SQL was generated or run. The client
        # answers with a new query on the same thread_id.
        result["status"] = "needs_clarification"
        result["clarification_question"] = values["clarification_question"]
        result["answer"] = values.get("final_answer") or values["clarification_question"]
        result["sql"] = result["sql_explanation"] = None
        return result

    rows = values.get("db_results")
    if rows is not None:
        rows = list(rows)
        meta = values.get("execution_metadata") or {}
        result["row_count"] = int(meta.get("row_count") or len(rows))
        result["truncated"] = bool(meta.get("truncated")) or len(rows) > max_rows
        result["rows"] = _json_safe(rows[:max_rows])
    return result


# -- graph execution ----------------------------------------------------------

async def _drive(graph, graph_input, config: dict, on_stage: Callable[[str], None] | None,
                 interrupt_before: list[str] | None):
    from langgraph.errors import GraphInterrupt

    kwargs = {"interrupt_before": interrupt_before} if interrupt_before else {}
    try:
        async for chunk in graph.astream(graph_input, config=config, stream_mode="updates",
                                         **kwargs):
            for node_name in chunk or ():
                if not str(node_name).startswith("__") and on_stage is not None:
                    on_stage(node_name)
    except GraphInterrupt:
        pass  # older LangGraph raises instead of emitting an "__interrupt__" chunk
    return await graph.aget_state(config)


async def run_query(graph, *, initial_state: dict, tenant_id: str, thread_id: str,
                    require_approval: bool | None, settings: Settings,
                    groq_api_key: str | None = None, openai_api_key: str | None = None,
                    on_stage: Callable[[str], None] | None = None) -> dict:
    """Run a new query on the tenant's thread and return the public result dict."""
    config = graph_config(tenant_id, thread_id)
    # Per-request approval: a static breakpoint in front of the hitl node, independent of
    # the global hitl.enabled switch that the node itself reads from config.yaml.
    interrupt_before = [HITL_NODE] if require_approval else None
    with llm_credentials(groq_api_key, openai_api_key):
        snapshot = await _drive(graph, initial_state, config, on_stage, interrupt_before)
    return build_result(snapshot, thread_id, settings.max_result_rows)


async def load_pending_approval(graph, tenant_id: str, thread_id: str):
    """Return the snapshot of a thread that is paused for approval, or raise 404 / 409."""
    snapshot = await graph.aget_state(graph_config(tenant_id, thread_id))
    values = getattr(snapshot, "values", None) or {}
    if not values or values.get("tenant_id") != tenant_id:
        raise APIError(404, "not_found", "Thread not found.")
    if HITL_NODE not in tuple(getattr(snapshot, "next", None) or ()):
        raise APIError(409, "not_awaiting_approval", "This thread is not awaiting approval.")
    return snapshot


def approval_update(decision: str, sql: str | None, values: dict) -> dict:
    """State written on behalf of the hitl node for a human decision."""
    if decision == "approve":
        return {"approval_status": "approved"}
    if decision == "edit":
        # The edited statement replaces generated_sql and takes the normal route to the
        # executor node, which runs the safety scanner and the row limit. It is never
        # executed from the API layer.
        return {"approval_status": "approved", "generated_sql": sql}
    return {
        "approval_status": "rejected",
        "error_log": {
            "error_type": "user_rejected",
            "message": "Query rejected by user during HITL review.",
            "dialect": values.get("db_type", "unknown"),
            "failed_sql": values.get("generated_sql"),
            "hint": "The SQL was reviewed and rejected. Please rephrase your question.",
        },
    }


async def resume_with_decision(graph, *, tenant_id: str, thread_id: str, decision: str,
                               sql: str | None, values: dict, settings: Settings,
                               groq_api_key: str | None = None,
                               openai_api_key: str | None = None,
                               on_stage: Callable[[str], None] | None = None) -> dict:
    """Record the decision as the hitl node's output and continue the paused run.

    ``aupdate_state(..., as_node="hitl")`` completes the hitl step with the decision, so the
    graph's own ``route_after_hitl`` picks executor (approve/edit) or formatter (reject).
    This works for both kinds of pause (the per-request breakpoint and the node's own
    ``interrupt()``) and does not depend on the global hitl.enabled switch.
    """
    config = graph_config(tenant_id, thread_id)
    await graph.aupdate_state(config, approval_update(decision, sql, values), as_node=HITL_NODE)
    with llm_credentials(groq_api_key, openai_api_key):
        snapshot = await _drive(graph, None, config, on_stage, None)
    return build_result(snapshot, thread_id, settings.max_result_rows)


async def with_timeout(coro, settings: Settings):
    try:
        return await asyncio.wait_for(coro, timeout=settings.request_timeout_seconds)
    except asyncio.TimeoutError:
        raise APIError(504, "timeout", "The request took too long and was cancelled.") from None
