"""Admin API: tenants and API keys. Guarded by T2S_ADMIN_API_KEY (X-Admin-Key header)."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Response

from api.auth import ApiKeyRecord, AuthStore, Tenant
from api.deps import get_auth_store, require_admin
from api.errors import not_found
from api.schemas import (ApiKeyCreated, TenantCreate, TenantCreated, TenantList, TenantOut,
                         TenantWithKeys)

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _tenant_out(tenant: Tenant) -> dict:
    return {"id": tenant.id, "name": tenant.name, "created_at": tenant.created_at}


def _key_created(record: ApiKeyRecord, plaintext: str) -> ApiKeyCreated:
    return ApiKeyCreated(id=record.id, tenant_id=record.tenant_id, prefix=record.prefix,
                         created_at=record.created_at, key=plaintext)


@router.post("/tenants", status_code=201, response_model=TenantCreated,
             summary="Create a tenant and its first API key (the key is shown once)")
async def create_tenant(body: TenantCreate, store: AuthStore = Depends(get_auth_store)):
    def work():
        tenant = store.create_tenant(body.name)
        return tenant, store.create_key(tenant.id)

    tenant, (record, plaintext) = await asyncio.to_thread(work)
    return TenantCreated(tenant=TenantOut(**_tenant_out(tenant)),
                         api_key=_key_created(record, plaintext))


@router.get("/tenants", response_model=TenantList, summary="List tenants and key metadata")
async def list_tenants(store: AuthStore = Depends(get_auth_store)):
    def work():
        return store.list_tenants(), store.list_keys()

    tenants, keys = await asyncio.to_thread(work)
    by_tenant: dict[str, list[dict]] = {}
    for key in keys:
        by_tenant.setdefault(key.tenant_id, []).append({
            "id": key.id, "tenant_id": key.tenant_id, "prefix": key.prefix,
            "created_at": key.created_at, "revoked_at": key.revoked_at,
            "last_used_at": key.last_used_at})
    return TenantList(tenants=[
        TenantWithKeys(**_tenant_out(t), keys=by_tenant.get(t.id, [])) for t in tenants])


@router.post("/tenants/{tenant_id}/keys", status_code=201, response_model=ApiKeyCreated,
             summary="Create another API key for a tenant (shown once)")
async def create_key(tenant_id: str, store: AuthStore = Depends(get_auth_store)):
    def work():
        if store.get_tenant(tenant_id) is None:
            return None
        return store.create_key(tenant_id)

    created = await asyncio.to_thread(work)
    if created is None:
        raise not_found("Tenant not found.")
    return _key_created(*created)


@router.delete("/keys/{key_id}", status_code=204, summary="Revoke an API key")
async def revoke_key(key_id: str, store: AuthStore = Depends(get_auth_store)):
    if not await asyncio.to_thread(store.revoke_key, key_id):
        raise not_found("API key not found.")
    return Response(status_code=204)
