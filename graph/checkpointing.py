"""Checkpointer factory for the LangGraph pipeline (see docs/phase2-contracts.md).

``open_checkpointer`` turns a URL into a ready-to-use LangGraph checkpointer:

    memory://                    in-process MemorySaver (default, lost on restart)
    sqlite:///relative/file.db   AsyncSqliteSaver (four slashes for an absolute path)
    postgresql://user:pw@host/db AsyncPostgresSaver on a connection pool

A SQLAlchemy-style driver suffix (``postgresql+psycopg://``, ``sqlite+aiosqlite://``) is
accepted and ignored, so the same URL can be used for ``T2S_METADATA_URL``.

The URL defaults to the ``T2S_CHECKPOINT_URL`` environment variable. The savers for
SQLite and PostgreSQL are async and belong to the event loop that opened them, so open
the checkpointer once at application startup and keep it open until shutdown:

    async with open_checkpointer() as saver:
        graph = build_graph(saver)
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ENV_VAR = "T2S_CHECKPOINT_URL"
DEFAULT_URL = "memory://"
POOL_SIZE_ENV_VAR = "T2S_CHECKPOINT_POOL_SIZE"
DEFAULT_POOL_SIZE = 10

_SQLITE_SCHEMES = ("sqlite",)
_POSTGRES_SCHEMES = ("postgresql", "postgres")


def _scheme(url: str) -> str:
    """Lower-case scheme without a SQLAlchemy-style driver suffix (postgresql+psycopg)."""
    if "://" not in url:
        return ""
    return url.split("://", 1)[0].lower().split("+", 1)[0]


class CheckpointConfigError(ValueError):
    """The checkpoint URL is malformed, unsupported, or its driver is not installed."""


def resolve_checkpoint_url(url: str | None = None) -> str:
    """Explicit argument, else ``T2S_CHECKPOINT_URL``, else ``memory://``."""
    value = url if url is not None else os.getenv(ENV_VAR)
    value = (value or "").strip()
    return value or DEFAULT_URL


def redact_url(url: str) -> str:
    """The URL without its password, for logs and error messages."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    user = parts.username or ""
    return parts._replace(netloc=f"{user}:***@{host}").geturl()


def checkpoint_backend(url: str | None = None) -> str:
    """Return ``"memory"``, ``"sqlite"`` or ``"postgres"`` for *url*; raise if unsupported."""
    resolved = resolve_checkpoint_url(url)
    scheme = _scheme(resolved)
    if scheme == "memory":
        return "memory"
    if scheme in _SQLITE_SCHEMES:
        return "sqlite"
    if scheme in _POSTGRES_SCHEMES:
        return "postgres"
    raise CheckpointConfigError(
        f"Unsupported checkpoint URL {redact_url(resolved)!r} (from {ENV_VAR} or the url "
        "argument). Use memory://, sqlite:///path/to/file.db or postgresql://user:pass@host/db."
    )


def sqlite_path_from_url(url: str) -> str:
    """``sqlite:///rel/file.db`` -> ``rel/file.db``;
    ``sqlite:////abs/file.db`` -> ``/abs/file.db``."""
    _, sep, rest = url.partition("://")
    if not sep or _scheme(url) not in _SQLITE_SCHEMES:
        raise CheckpointConfigError(f"Not a sqlite checkpoint URL: {url!r}")
    if not rest.startswith("/"):
        raise CheckpointConfigError(
            f"Malformed sqlite checkpoint URL {url!r}: use sqlite:///relative/file.db "
            "or sqlite:////absolute/file.db."
        )
    path = rest[1:].split("?", 1)[0]
    if not path:
        raise CheckpointConfigError(f"sqlite checkpoint URL {url!r} has no file path.")
    return path


def postgres_conninfo(url: str) -> str:
    """Normalise the scheme to ``postgresql://`` (psycopg accepts both; be explicit)."""
    _, _, rest = url.partition("://")
    if _scheme(url) not in _POSTGRES_SCHEMES:
        raise CheckpointConfigError(f"Not a postgres checkpoint URL: {redact_url(url)!r}")
    return f"postgresql://{rest}"


def _pool_size() -> int:
    try:
        return max(1, int(os.getenv(POOL_SIZE_ENV_VAR, DEFAULT_POOL_SIZE)))
    except ValueError:
        return DEFAULT_POOL_SIZE


@asynccontextmanager
async def _open_sqlite(url: str) -> AsyncIterator[object]:
    path = sqlite_path_from_url(url)
    try:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    except ImportError as exc:
        raise CheckpointConfigError(
            "SQLite checkpoints need the langgraph-checkpoint-sqlite and aiosqlite packages."
        ) from exc

    if path != ":memory:":
        Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        path = str(Path(path).expanduser())
    async with aiosqlite.connect(path) as conn:
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        yield saver


@asynccontextmanager
async def _open_postgres(url: str) -> AsyncIterator[object]:
    conninfo = postgres_conninfo(url)
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:
        raise CheckpointConfigError(
            "PostgreSQL checkpoints need the langgraph-checkpoint-postgres and psycopg packages."
        ) from exc

    try:
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
    except ImportError:
        # No pool available: one connection, statements serialised by the saver's lock.
        logger.warning(
            "psycopg_pool is not installed: PostgreSQL checkpoints use a single connection."
        )
        async with AsyncPostgresSaver.from_conn_string(conninfo) as saver:
            await saver.setup()
            yield saver
        return

    pool = AsyncConnectionPool(
        conninfo=conninfo,
        min_size=1,
        max_size=_pool_size(),
        # The saver requires autocommit (setup runs CREATE INDEX CONCURRENTLY) and dict rows.
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        yield saver
    finally:
        await pool.close()


@asynccontextmanager
async def open_checkpointer(url: str | None = None) -> AsyncIterator[object]:
    """Yield a LangGraph checkpointer for *url* (default: ``T2S_CHECKPOINT_URL``).

    Tables are created on entry (``setup()`` runs once per open). Connections are closed
    on exit, so a graph compiled with the yielded saver must not be used afterwards.
    """
    resolved = resolve_checkpoint_url(url)
    backend = checkpoint_backend(resolved)
    if backend == "memory":
        from langgraph.checkpoint.memory import MemorySaver

        yield MemorySaver()
    elif backend == "sqlite":
        async with _open_sqlite(resolved) as saver:
            yield saver
    else:
        logger.info("Opening PostgreSQL checkpointer at %s", redact_url(resolved))
        async with _open_postgres(resolved) as saver:
            yield saver
