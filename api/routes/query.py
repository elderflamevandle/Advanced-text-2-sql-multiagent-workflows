"""Query endpoints: JSON, Server-Sent Events, and human approval of a paused run."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Header, Path, Request
from fastapi.responses import JSONResponse, StreamingResponse

from api import query_service as qs
from api.auth import Tenant
from api.deps import current_tenant, get_graph, get_rate_limiter, get_registry, get_settings
from api.errors import APIError, envelope
from api.schemas import THREAD_ID_PATTERN, ApprovalRequest, QueryRequest, QueryResponse
from api.settings import Settings

router = APIRouter(prefix="/v1", tags=["query"])
logger = logging.getLogger(__name__)

_GROQ_HEADER = Header(default=None, alias="X-Groq-Api-Key",
                      description="Optional Groq key used for this request only.")
_OPENAI_HEADER = Header(default=None, alias="X-OpenAI-Api-Key",
                        description="Optional OpenAI key used for this request only.")


def _check_question(body: QueryRequest, settings: Settings) -> None:
    if len(body.question) > settings.max_question_chars:
        raise APIError(422, "validation_error",
                       f"question must be at most {settings.max_question_chars} characters.")


async def _load_connection(registry, tenant_id: str, connection_id: str):
    """Connection info and schema for this tenant (ConnectionNotFound -> 404 via handler)."""
    def work():
        info = registry.get_info(tenant_id, connection_id)
        return info, registry.get_schema(tenant_id, connection_id)

    return await asyncio.to_thread(work)


def _initial_state(body: QueryRequest, tenant: Tenant, info, schema) -> dict:
    return qs.build_initial_state(body.question, tenant_id=tenant.id, connection_id=info.id,
                                  db_type=info.db_type, schema=schema)


@router.post("/query", response_model=QueryResponse,
             summary="Ask a question; returns when the run completes, fails or needs approval")
async def query(
    body: QueryRequest,
    request: Request,
    tenant: Tenant = Depends(current_tenant),
    settings: Settings = Depends(get_settings),
    registry=Depends(get_registry),
    rate_limiter=Depends(get_rate_limiter),
    graph=Depends(get_graph),
    groq_key: str | None = _GROQ_HEADER,
    openai_key: str | None = _OPENAI_HEADER,
):
    _check_question(body, settings)
    groq_key, openai_key = qs.clean_provider_key(groq_key), qs.clean_provider_key(openai_key)
    thread_id = body.thread_id or qs.new_thread_id()
    permit = await qs.admit(rate_limiter, request.app.state.admission, settings,
                            tenant.id, thread_id)
    try:
        async def work():
            info, schema = await _load_connection(registry, tenant.id, body.connection_id)
            return await qs.run_query(
                graph, initial_state=_initial_state(body, tenant, info, schema),
                tenant_id=tenant.id, thread_id=thread_id,
                require_approval=body.require_approval, settings=settings,
                groq_api_key=groq_key, openai_api_key=openai_key)

        result = await qs.with_timeout(work(), settings)
    finally:
        permit.release()
    return JSONResponse(result)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@router.post("/query/stream", summary="Ask a question; progress and result as SSE",
             response_class=StreamingResponse,
             responses={200: {"content": {"text/event-stream": {}},
                              "description": "`stage` events, then one `result` or `error`."}})
async def query_stream(
    body: QueryRequest,
    request: Request,
    tenant: Tenant = Depends(current_tenant),
    settings: Settings = Depends(get_settings),
    registry=Depends(get_registry),
    rate_limiter=Depends(get_rate_limiter),
    graph=Depends(get_graph),
    groq_key: str | None = _GROQ_HEADER,
    openai_key: str | None = _OPENAI_HEADER,
):
    _check_question(body, settings)
    groq_key, openai_key = qs.clean_provider_key(groq_key), qs.clean_provider_key(openai_key)
    thread_id = body.thread_id or qs.new_thread_id()
    request_id = getattr(request.state, "request_id", None)
    permit = await qs.admit(rate_limiter, request.app.state.admission, settings,
                            tenant.id, thread_id)
    try:
        # Resolved before the stream starts, so auth/limit/404 errors are plain HTTP errors.
        info, schema = await _load_connection(registry, tenant.id, body.connection_id)
        initial_state = _initial_state(body, tenant, info, schema)
    except BaseException:
        permit.release()
        raise

    queue: asyncio.Queue = asyncio.Queue()

    def on_stage(node: str) -> None:
        queue.put_nowait(("stage", {"node": node, "label": qs.NODE_LABELS.get(node, node)}))

    async def runner() -> None:
        try:
            result = await qs.with_timeout(qs.run_query(
                graph, initial_state=initial_state, tenant_id=tenant.id, thread_id=thread_id,
                require_approval=body.require_approval, settings=settings,
                groq_api_key=groq_key, openai_api_key=openai_key, on_stage=on_stage), settings)
            queue.put_nowait(("result", result))
        except APIError as exc:
            queue.put_nowait(("error", envelope(exc.error_type, exc.message, request_id)))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - details stay in the server log
            logger.exception("Streaming query failed")
            queue.put_nowait(("error", envelope(
                "internal_error", "Internal server error.", request_id)))
        finally:
            permit.release()
            queue.put_nowait(None)

    # The task owns the permit, so it is released even if the response is never iterated.
    task = asyncio.create_task(runner())

    async def events():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(),
                                                  timeout=settings.sse_heartbeat_seconds)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if item is None:
                    break
                yield _sse(*item)
        finally:
            # Client went away (or the stream ended): stop the graph run.
            if not task.done():
                task.cancel()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/threads/{thread_id}/approval", response_model=QueryResponse,
             summary="Approve, reject or edit the SQL of a run that is awaiting approval")
async def approve(
    body: ApprovalRequest,
    request: Request,
    thread_id: str = Path(pattern=THREAD_ID_PATTERN),
    tenant: Tenant = Depends(current_tenant),
    settings: Settings = Depends(get_settings),
    registry=Depends(get_registry),
    rate_limiter=Depends(get_rate_limiter),
    graph=Depends(get_graph),
    groq_key: str | None = _GROQ_HEADER,
    openai_key: str | None = _OPENAI_HEADER,
):
    sql = (body.sql or "").strip() or None
    if body.decision == "edit":
        if sql is None:
            raise APIError(422, "validation_error", "sql is required when decision is 'edit'.")
        if len(sql) > settings.max_sql_chars:
            raise APIError(422, "validation_error",
                           f"sql must be at most {settings.max_sql_chars} characters.")
    groq_key, openai_key = qs.clean_provider_key(groq_key), qs.clean_provider_key(openai_key)
    permit = await qs.admit(rate_limiter, request.app.state.admission, settings,
                            tenant.id, thread_id)
    try:
        async def work():
            snapshot = await qs.load_pending_approval(graph, tenant.id, thread_id)
            values = dict(snapshot.values)
            # The connection must still exist and still belong to this tenant.
            await asyncio.to_thread(registry.get_info, tenant.id, values.get("connection_id"))
            return await qs.resume_with_decision(
                graph, tenant_id=tenant.id, thread_id=thread_id, decision=body.decision,
                sql=sql if body.decision == "edit" else None, values=values,
                settings=settings, groq_api_key=groq_key, openai_api_key=openai_key)

        result = await qs.with_timeout(work(), settings)
    finally:
        permit.release()
    return JSONResponse(result)
