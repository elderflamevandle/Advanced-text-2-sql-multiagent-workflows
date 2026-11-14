"""Request id propagation, JSON access log and the last-resort 500 handler (pure ASGI)."""
from __future__ import annotations

import json
import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.requests import ClientDisconnect

from api.context import request_id_var, tenant_id_var
from api.errors import envelope

logger = logging.getLogger("t2s.api")
access_logger = logging.getLogger("t2s.access")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _incoming_request_id(scope) -> str:
    for name, value in scope.get("headers") or ():
        if name == b"x-request-id":
            candidate = value.decode("latin-1").strip()
            if _REQUEST_ID_RE.match(candidate):
                return candidate
            break
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Outermost middleware.

    * accepts a well-formed ``X-Request-ID`` or generates one, echoes it on the response and
      binds it to the logging context;
    * writes one JSON access line per request: method, route template, status, latency and
      tenant id. Never the query string, body, question text or any key;
    * turns an unhandled exception into the standard 500 envelope. The traceback goes to the
      server log only.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope)
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        rid_token = request_id_var.set(request_id)
        tenant_token = tenant_id_var.set(None)
        started = time.perf_counter()
        status = 500
        response_started = False

        async def send_wrapper(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except ClientDisconnect:
            status = 499  # client closed the connection; nothing to send
        except Exception:  # noqa: BLE001 - last line of defence
            logger.exception("Unhandled error while serving request")
            status = 500
            if not response_started:
                body = json.dumps(envelope(
                    "internal_error", "Internal server error.", request_id)).encode()
                await send({
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode()),
                                (b"x-request-id", request_id.encode())],
                })
                await send({"type": "http.response.body", "body": body})
        finally:
            route = scope.get("route")
            tenant_id = state.get("tenant_id")
            tenant_var_token = tenant_id_var.set(tenant_id)
            access_logger.info(json.dumps({
                "event": "http_request",
                "request_id": request_id,
                "tenant_id": tenant_id,
                "method": scope.get("method"),
                "route": getattr(route, "path", None) or "unmatched",
                "status": status,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }))
            tenant_id_var.reset(tenant_var_token)
            tenant_id_var.reset(tenant_token)
            request_id_var.reset(rid_token)
