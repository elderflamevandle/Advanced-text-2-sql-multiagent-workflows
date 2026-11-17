"""Service settings, read from the T2S_* environment variables (docs/phase2-contracts.md)."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="T2S_", extra="ignore")

    # Bootstrap key for /v1/admin/*. Admin routes answer 404 when it is unset or empty.
    admin_api_key: str | None = None
    # Per-tenant request budget for the query endpoints (requests per 60 seconds).
    rate_limit_per_minute: int = Field(default=60, ge=1)
    # Per-tenant cap on queries that are running at the same time (per process).
    max_concurrent_queries: int = Field(default=8, ge=1)
    # Wall-clock budget for one graph run (query or approval resume).
    request_timeout_seconds: float = Field(default=120.0, gt=0)
    max_question_chars: int = Field(default=4000, ge=1)
    max_sql_chars: int = Field(default=20000, ge=1)
    # Rows returned in a query response; row_count/truncated describe the full result.
    max_result_rows: int = Field(default=100, ge=1)
    # Interval of the SSE keep-alive comment on /v1/query/stream.
    sse_heartbeat_seconds: float = Field(default=15.0, gt=0)
    # Comma-separated list of allowed origins. Empty (default) = CORS middleware not installed.
    cors_origins: str = ""

    @property
    def admin_key(self) -> str | None:
        key = (self.admin_api_key or "").strip()
        return key or None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
