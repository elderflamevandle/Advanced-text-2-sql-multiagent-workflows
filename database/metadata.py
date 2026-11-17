"""Shared SQLAlchemy engine for service metadata (tenants, API keys, connections)."""
from __future__ import annotations

import os
import threading
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

DEFAULT_METADATA_URL = "sqlite:///.t2s/metadata.db"

_engine: Engine | None = None
_lock = threading.Lock()


def metadata_url() -> str:
    return os.getenv("T2S_METADATA_URL", DEFAULT_METADATA_URL)


def is_persistent(url: str | None = None) -> bool:
    """False for in-memory SQLite, where an ephemeral encryption key is acceptable."""
    url = url or metadata_url()
    return not (url.startswith("sqlite") and (":memory:" in url or url.endswith("sqlite://")))


def get_engine() -> Engine:
    """Return the process-wide metadata engine, creating it on first use."""
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                url = metadata_url()
                kwargs: dict = {"future": True}
                if url.startswith("sqlite"):
                    kwargs["connect_args"] = {"check_same_thread": False}
                    if not is_persistent(url):
                        kwargs["poolclass"] = StaticPool  # one shared in-memory database
                    else:
                        path = url.split("///", 1)[-1]
                        if path:
                            Path(path).parent.mkdir(parents=True, exist_ok=True)
                else:
                    kwargs["pool_pre_ping"] = True
                _engine = create_engine(url, **kwargs)
    return _engine


def reset_engine() -> None:
    """Dispose the cached engine (tests, or after changing T2S_METADATA_URL)."""
    global _engine
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
