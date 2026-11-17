"""Schema catalog of a connection (build, status, descriptions) and query feedback."""
from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from api import query_service as qs
from api.auth import Tenant
from api.deps import (current_tenant, get_catalog, get_memory, get_rate_limiter,
                      get_registry, get_settings)
from api.errors import APIError
from api.schemas import (CatalogBuildRequest, CatalogStatusOut, DescriptionUpdate,
                         FeedbackRequest)
from api.settings import Settings

router = APIRouter(prefix="/v1", tags=["catalog"])

_UNAVAILABLE = APIError(503, "catalog_unavailable",
                        "The schema catalog is not available on this server.")


def _status_body(connection_id: str, status: Any) -> dict:
    if dataclasses.is_dataclass(status) and not isinstance(status, type):
        data = dataclasses.asdict(status)
    elif isinstance(status, dict):
        data = dict(status)
    else:
        data = {"state": str(getattr(status, "state", "missing"))}
    data.setdefault("state", "missing")
    return qs._json_safe({"connection_id": connection_id, **data})


async def _call(registry, tenant_id: str, connection_id: str, fn, *args, **kwargs):
    """Check that the connection is this tenant's (404 otherwise), then call the service."""
    def work():
        registry.get_info(tenant_id, connection_id)  # ConnectionNotFound -> 404
        return fn(tenant_id, connection_id, *args, **kwargs)

    try:
        return await asyncio.to_thread(work)
    except NotImplementedError:
        raise _UNAVAILABLE from None


@router.post("/connections/{connection_id}/catalog", status_code=202,
             response_model=CatalogStatusOut,
             summary="Start building the schema catalog (force=true rebuilds)")
async def build_catalog(connection_id: str, body: CatalogBuildRequest | None = None,
                        tenant: Tenant = Depends(current_tenant),
                        registry=Depends(get_registry), catalog=Depends(get_catalog)):
    force = bool(body.force) if body is not None else False
    status = await _call(registry, tenant.id, connection_id, catalog.start_build, force=force)
    return JSONResponse(_status_body(connection_id, status), status_code=202)


@router.get("/connections/{connection_id}/catalog", response_model=CatalogStatusOut,
            summary="Build state of the schema catalog")
async def catalog_status(connection_id: str, tenant: Tenant = Depends(current_tenant),
                         registry=Depends(get_registry), catalog=Depends(get_catalog)):
    status = await _call(registry, tenant.id, connection_id, catalog.status)
    return JSONResponse(_status_body(connection_id, status))


@router.get("/connections/{connection_id}/catalog/tables",
            summary="Tables, columns and descriptions in the catalog")
async def catalog_tables(connection_id: str, tenant: Tenant = Depends(current_tenant),
                         registry=Depends(get_registry), catalog=Depends(get_catalog)):
    described = await _call(registry, tenant.id, connection_id, catalog.describe)
    return JSONResponse(qs._json_safe(described))


@router.put("/connections/{connection_id}/catalog/descriptions", status_code=204,
            summary="Set the description of a table or a column")
async def set_description(connection_id: str, body: DescriptionUpdate,
                          tenant: Tenant = Depends(current_tenant),
                          registry=Depends(get_registry), catalog=Depends(get_catalog)):
    try:
        await _call(registry, tenant.id, connection_id, catalog.set_description,
                    body.table, body.column, body.description)
    except KeyError:
        raise APIError(404, "not_found", "Table or column not found in the catalog.") from None
    return Response(status_code=204)


@router.post("/queries/{query_id}/feedback", status_code=204,
             summary="Rate an answered query; optionally supply the correct SQL")
async def query_feedback(query_id: str, body: FeedbackRequest,
                         tenant: Tenant = Depends(current_tenant),
                         settings: Settings = Depends(get_settings),
                         rate_limiter=Depends(get_rate_limiter),
                         memory=Depends(get_memory)):
    if len(query_id) > 128:
        raise APIError(404, "not_found", "Query not found.")
    await qs.check_rate_limit(rate_limiter, settings, tenant.id)
    corrected_sql = (body.corrected_sql or "").strip() or None
    if corrected_sql and len(corrected_sql) > settings.max_sql_chars:
        raise APIError(422, "validation_error",
                       f"corrected_sql must be at most {settings.max_sql_chars} characters.")
    try:
        await asyncio.to_thread(memory.record_feedback, tenant.id, query_id,
                                rating=body.rating, comment=body.comment or None,
                                corrected_sql=corrected_sql)
    except LookupError:
        # Same answer for "missing" and "another tenant's query".
        raise APIError(404, "not_found", "Query not found.") from None
    except ValueError:
        raise APIError(422, "validation_error",
                       "corrected_sql must be a single read-only SQL query.") from None
    except NotImplementedError:
        raise APIError(503, "memory_unavailable",
                       "Query feedback is not available on this server.") from None
    return Response(status_code=204)
