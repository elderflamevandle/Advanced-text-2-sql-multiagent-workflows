"""Tenant-scoped database connections, backed by the connection registry."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Response

from api.auth import Tenant
from api.deps import current_tenant, get_registry, optional_catalog, optional_memory
from api.schemas import ConnectionCreate, ConnectionList, ConnectionOut, SchemaOut

router = APIRouter(prefix="/v1/connections", tags=["connections"])
logger = logging.getLogger(__name__)

# The registry promises params_public holds no secrets; this is a second line of defence.
_SECRET_MARKERS = ("password", "passwd", "secret", "token", "credential", "private_key",
                   "api_key", "apikey", "dsn", "uri", "url")


def _public_params(params: Any) -> dict:
    if not isinstance(params, dict):
        return {}
    return {str(k): v for k, v in params.items()
            if not any(marker in str(k).lower() for marker in _SECRET_MARKERS)}


def _auto_build_enabled() -> bool:
    """catalog.auto_build from config/config.yaml (default: on)."""
    try:
        from llm.config import get_section

        return bool(get_section("catalog").get("auto_build", True))
    except Exception:  # noqa: BLE001
        return True


def connection_out(info) -> ConnectionOut:
    return ConnectionOut(id=info.id, name=info.name, db_type=info.db_type,
                         params=_public_params(info.params_public),
                         created_at=info.created_at or "")


def _schema_tables(schema: Any, include_samples: bool) -> list[dict]:
    tables = []
    for name, table in (schema or {}).items():
        table = table if isinstance(table, dict) else {}
        entry = {
            "name": name,
            "columns": table.get("columns") or [],
            "primary_keys": table.get("primary_keys") or [],
            "foreign_keys": table.get("foreign_keys") or [],
        }
        if include_samples:
            entry["sample_rows"] = table.get("sample_rows") or []
        tables.append(entry)
    return tables


@router.post("", status_code=201, response_model=ConnectionOut,
             summary="Register a database connection (verified before it is stored)")
async def create_connection(body: ConnectionCreate, tenant: Tenant = Depends(current_tenant),
                            registry=Depends(get_registry),
                            catalog=Depends(optional_catalog)):
    info = await asyncio.to_thread(
        registry.register, tenant.id, body.name, body.db_type.lower(), body.params)
    if catalog is not None and _auto_build_enabled():
        # Returns at once (the build runs in the background). A catalog problem must never
        # fail the registration: queries fall back to the plain schema until one is built.
        try:
            await asyncio.to_thread(catalog.start_build, tenant.id, info.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Catalog build could not be started for a new connection (%s)",
                           type(exc).__name__)
    return connection_out(info)


@router.get("", response_model=ConnectionList, summary="List this tenant's connections")
async def list_connections(tenant: Tenant = Depends(current_tenant),
                           registry=Depends(get_registry)):
    infos = await asyncio.to_thread(registry.list, tenant.id)
    return ConnectionList(connections=[connection_out(i) for i in infos])


@router.get("/{connection_id}", response_model=ConnectionOut, summary="Get one connection")
async def get_connection(connection_id: str, tenant: Tenant = Depends(current_tenant),
                         registry=Depends(get_registry)):
    return connection_out(await asyncio.to_thread(registry.get_info, tenant.id, connection_id))


@router.delete("/{connection_id}", status_code=204, summary="Delete a connection")
async def delete_connection(connection_id: str, tenant: Tenant = Depends(current_tenant),
                            registry=Depends(get_registry),
                            catalog=Depends(optional_catalog),
                            memory=Depends(optional_memory)):
    await asyncio.to_thread(registry.delete, tenant.id, connection_id)
    # The connection is gone: drop what was derived from it. Failures are logged only.
    for label, call in (("catalog", getattr(catalog, "delete", None)),
                        ("query memory", getattr(memory, "delete_connection", None))):
        if call is None:
            continue
        try:
            await asyncio.to_thread(call, tenant.id, connection_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Deleting the %s of a removed connection failed (%s)",
                           label, type(exc).__name__)
    return Response(status_code=204)


@router.get("/{connection_id}/schema", response_model=SchemaOut,
            summary="Tables, columns and keys of a connection")
async def get_connection_schema(
    connection_id: str,
    refresh: bool = Query(default=False, description="Re-read the schema from the database."),
    include_samples: bool = Query(default=False, description="Include sample rows."),
    tenant: Tenant = Depends(current_tenant),
    registry=Depends(get_registry),
):
    from api.query_service import _json_safe

    def work():
        return registry.get_schema(tenant.id, connection_id, refresh=refresh)

    schema = await asyncio.to_thread(work)
    return SchemaOut(connection_id=connection_id,
                     tables=_json_safe(_schema_tables(schema, include_samples)))
