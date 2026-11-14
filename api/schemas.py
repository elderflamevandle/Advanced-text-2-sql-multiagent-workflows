"""Request and response models."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

THREAD_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# -- admin -------------------------------------------------------------------

class TenantCreate(_Request):
    name: str = Field(min_length=1, max_length=200)


class TenantOut(BaseModel):
    id: str
    name: str
    created_at: str


class ApiKeyOut(BaseModel):
    id: str
    tenant_id: str
    prefix: str
    created_at: str
    revoked_at: str | None = None
    last_used_at: str | None = None


class ApiKeyCreated(BaseModel):
    """The only response that ever contains the key itself."""
    id: str
    tenant_id: str
    prefix: str
    created_at: str
    key: str


class TenantCreated(BaseModel):
    tenant: TenantOut
    api_key: ApiKeyCreated


class TenantWithKeys(TenantOut):
    keys: list[ApiKeyOut] = []


class TenantList(BaseModel):
    tenants: list[TenantWithKeys]


# -- connections -------------------------------------------------------------

class ConnectionCreate(_Request):
    name: str = Field(min_length=1, max_length=100)
    db_type: str = Field(min_length=1, max_length=32)
    params: dict[str, Any] = Field(default_factory=dict)


class ConnectionOut(BaseModel):
    id: str
    name: str
    db_type: str
    params: dict[str, Any]
    created_at: str


class ConnectionList(BaseModel):
    connections: list[ConnectionOut]


class SchemaOut(BaseModel):
    connection_id: str
    tables: list[dict[str, Any]]


# -- query -------------------------------------------------------------------

class QueryRequest(_Request):
    connection_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1)
    thread_id: str | None = Field(default=None, pattern=THREAD_ID_PATTERN)
    # True: always pause before execution for approval. False/omitted: server policy applies.
    require_approval: bool | None = None


class ApprovalRequest(_Request):
    decision: Literal["approve", "reject", "edit"]
    sql: str | None = None


class ErrorInfo(BaseModel):
    type: str
    message: str


class Usage(BaseModel):
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    calls: int = 0


class PendingApproval(BaseModel):
    sql: str | None = None
    explanation: str | None = None


class QueryResponse(BaseModel):
    thread_id: str
    status: Literal["completed", "awaiting_approval", "failed"]
    answer: str | None = None
    sql: str | None = None
    sql_explanation: str | None = None
    rows: list[dict[str, Any]] | None = None
    row_count: int = 0
    truncated: bool = False
    error: ErrorInfo | None = None
    usage: Usage = Usage()
    pending_approval: PendingApproval | None = None
