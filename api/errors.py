"""Error envelope ``{"error": {"type", "message", "request_id"}}`` and exception handlers."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.context import request_id_var

logger = logging.getLogger(__name__)

_STATUS_TYPES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "unavailable",
    504: "timeout",
}


class APIError(Exception):
    """An error with a client-safe message. Anything else becomes a generic 500."""

    def __init__(self, status_code: int, error_type: str, message: str,
                 headers: dict[str, str] | None = None, details: list | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.message = message
        self.headers = headers
        self.details = details


def not_found(message: str = "Not found.") -> APIError:
    return APIError(404, "not_found", message)


def envelope(error_type: str, message: str, request_id: str | None = None,
             details: list | None = None) -> dict:
    body = {"type": error_type, "message": message,
            "request_id": request_id or request_id_var.get()}
    if details:
        body["details"] = details
    return {"error": body}


def error_response(status_code: int, error_type: str, message: str, *,
                   headers: dict[str, str] | None = None, details: list | None = None,
                   request_id: str | None = None) -> JSONResponse:
    return JSONResponse(envelope(error_type, message, request_id, details),
                        status_code=status_code, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    from database.registry import ConnectionNotFound, ConnectionRejected

    @app.exception_handler(APIError)
    async def _api_error(request: Request, exc: APIError):
        return error_response(exc.status_code, exc.error_type, exc.message,
                              headers=exc.headers, details=exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        error_type = _STATUS_TYPES.get(exc.status_code, "error")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return error_response(exc.status_code, error_type, message,
                              headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # Only location, message and type: pydantic's "input" would echo request data
        # (connection params may hold passwords).
        details = [
            {"loc": [str(p) for p in err.get("loc", ())], "msg": err.get("msg", ""),
             "type": err.get("type", "")}
            for err in exc.errors()
        ]
        return error_response(422, "validation_error", "Request validation failed.",
                              details=details)

    @app.exception_handler(ConnectionNotFound)
    async def _connection_not_found(request: Request, exc: ConnectionNotFound):
        # Same answer for "missing" and "owned by another tenant".
        return error_response(404, "not_found", "Connection not found.")

    @app.exception_handler(ConnectionRejected)
    async def _connection_rejected(request: Request, exc: ConnectionRejected):
        return error_response(422, "connection_rejected",
                              str(exc) or "Connection rejected.")
