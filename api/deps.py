"""FastAPI dependencies: settings, collaborators from ``app.state``, tenant and admin auth."""
from __future__ import annotations

import asyncio

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader

from api.auth import AuthStore, Tenant, constant_time_equals
from api.context import tenant_id_var
from api.errors import APIError
from api.settings import Settings

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False,
                               description="Tenant API key.")
_admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False,
                                 scheme_name="AdminKey",
                                 description="Value of T2S_ADMIN_API_KEY.")

_UNAUTHORIZED = "Missing or invalid API key."


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_auth_store(request: Request) -> AuthStore:
    return request.app.state.auth_store


def _require(request: Request, name: str):
    value = getattr(request.app.state, name, None)
    if value is None:
        raise APIError(503, "unavailable", "Service is not ready.")
    return value


def get_registry(request: Request):
    return _require(request, "registry")


def get_rate_limiter(request: Request):
    return _require(request, "rate_limiter")


def get_graph(request: Request):
    return _require(request, "graph")


async def current_tenant(
    request: Request,
    api_key: str | None = Security(_api_key_header),
    store: AuthStore = Depends(get_auth_store),
) -> Tenant:
    tenant = await asyncio.to_thread(store.authenticate, api_key)
    if tenant is None:
        raise APIError(401, "unauthorized", _UNAUTHORIZED)
    request.state.tenant_id = tenant.id  # read by the access log
    tenant_id_var.set(tenant.id)
    return tenant


async def require_admin(
    admin_key: str | None = Security(_admin_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    expected = settings.admin_key
    if expected is None:
        # Admin API disabled: indistinguishable from a route that does not exist.
        raise APIError(404, "not_found", "Not Found")
    if not admin_key or not constant_time_equals(admin_key, expected):
        raise APIError(401, "unauthorized", _UNAUTHORIZED)
