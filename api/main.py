"""Application factory. ``uvicorn api.main:app`` or ``python -m api``."""
from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from api.auth import AuthStore
from api.context import install_log_record_factory
from api.errors import install_error_handlers
from api.limits import QueryAdmission
from api.observability import RequestContextMiddleware
from api.routes import admin, connections, ops, query
from api.settings import Settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Build the collaborators that were not injected through create_app()."""
    state = app.state
    async with AsyncExitStack() as stack:
        await asyncio.to_thread(state.auth_store.ensure_schema)

        owns_registry = False
        if state.registry is None:
            from database.registry import get_registry
            state.registry = get_registry()
            owns_registry = True
        if state.rate_limiter is None:
            from infra.ratelimit import get_rate_limiter
            state.rate_limiter = get_rate_limiter()
        owns_graph = False
        if state.graph is None:
            from graph.builder import build_graph
            from graph.checkpointing import open_checkpointer
            checkpointer = await stack.enter_async_context(open_checkpointer())
            state.graph = build_graph(checkpointer)
            owns_graph = True
        try:
            yield
        finally:
            if owns_graph:
                state.graph = None  # its checkpointer is closed by the exit stack
            if owns_registry:
                registry, state.registry = state.registry, None
                try:
                    await asyncio.to_thread(registry.close_all)
                except Exception:  # noqa: BLE001
                    logger.warning("Closing database connections failed", exc_info=True)


def create_app(settings: Settings | None = None, *, registry=None, rate_limiter=None,
               graph=None, auth_store: AuthStore | None = None) -> FastAPI:
    """Create the API.

    ``registry`` (database.registry.ConnectionRegistry), ``rate_limiter``
    (infra.ratelimit.RateLimiter) and ``graph`` (compiled LangGraph) are created in the
    lifespan when omitted; tests pass fakes. They are always read from ``app.state``.
    """
    settings = settings or Settings()
    install_log_record_factory()

    app = FastAPI(
        title="Text-to-SQL service",
        version="2.0.0",
        description="Multi-tenant natural-language-to-SQL API. Authenticate with X-API-Key.",
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.auth_store = auth_store or AuthStore()
    app.state.registry = registry
    if registry is not None:
        # The graph nodes resolve databases through database.registry.get_registry():
        # make them use the same instance as the HTTP layer.
        from database.registry import set_registry
        set_registry(registry)
    app.state.rate_limiter = rate_limiter
    app.state.graph = graph
    app.state.admission = QueryAdmission(settings.max_concurrent_queries)

    install_error_handlers(app)
    app.include_router(ops.router)
    app.include_router(admin.router)
    app.include_router(connections.router)
    app.include_router(query.router)

    if settings.cors_origin_list:  # off unless T2S_CORS_ORIGINS is set
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["X-API-Key", "X-Admin-Key", "X-Request-ID", "Content-Type",
                           "X-Groq-Api-Key", "X-OpenAI-Api-Key"],
            expose_headers=["X-Request-ID", "Retry-After"],
        )
    app.add_middleware(RequestContextMiddleware)  # added last = outermost
    return app


app = create_app()
