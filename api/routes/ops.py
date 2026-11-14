"""Liveness and readiness."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["ops"])
logger = logging.getLogger(__name__)


@router.get("/healthz", summary="Liveness (no dependencies)")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness (metadata database, graph/checkpointer)")
async def readyz(request: Request):
    state = request.app.state
    checks: dict[str, str] = {}
    try:
        await asyncio.wait_for(asyncio.to_thread(state.auth_store.ping), timeout=5)
        checks["metadata_db"] = "ok"
    except Exception:  # noqa: BLE001 - details go to the log, not to the client
        logger.warning("Readiness: metadata database check failed", exc_info=True)
        checks["metadata_db"] = "unavailable"
    graph = getattr(state, "graph", None)
    checks["checkpointer"] = "ok" if graph is not None else "unavailable"
    checks["registry"] = "ok" if getattr(state, "registry", None) is not None else "unavailable"
    ready = all(value == "ok" for value in checks.values())
    return JSONResponse({"status": "ready" if ready else "not_ready", "checks": checks},
                        status_code=200 if ready else 503)
