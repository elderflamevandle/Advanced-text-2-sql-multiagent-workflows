"""Key/value cache for JSON-serialisable values (see docs/phase2-contracts.md).

``get_cache()`` returns the Redis cache when ``T2S_REDIS_URL`` is set and the in-process
cache otherwise. A cache is never a source of truth, so the Redis cache fails open: when
Redis is unreachable ``get`` reports a miss and ``set``/``delete`` do nothing.

Values are stored as JSON text in both implementations. A value that is not
JSON-serialisable raises ``TypeError`` from ``set`` whichever backend is active, and
``get`` always returns a fresh copy the caller may mutate.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Protocol, runtime_checkable

from infra.redis_client import KEY_PREFIX, CircuitBreaker, create_redis_client, redis_url

logger = logging.getLogger(__name__)

MAX_ENTRIES_ENV_VAR = "T2S_CACHE_MAX_ENTRIES"
DEFAULT_MAX_ENTRIES = 1024


@runtime_checkable
class Cache(Protocol):
    async def get(self, key: str) -> Any | None: ...

    async def set(self, key: str, value: Any, ttl_seconds: float | None = None) -> None: ...

    async def delete(self, key: str) -> None: ...


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


class InMemoryCache:
    """LRU cache with per-entry TTL for one process. Safe across coroutines and threads.

    ``ttl_seconds=None`` keeps an entry until it is evicted by size; a TTL of zero or less
    removes the key.
    """

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max(1, max_entries)
        self._clock = clock
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[str, float | None]] = OrderedDict()

    async def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            payload, expires_at = entry
            if expires_at is not None and self._clock() >= expires_at:
                del self._data[key]
                return None
            self._data.move_to_end(key)
        return json.loads(payload)

    async def set(self, key: str, value: Any, ttl_seconds: float | None = None) -> None:
        payload = _dumps(value)
        with self._lock:
            if ttl_seconds is not None and ttl_seconds <= 0:
                self._data.pop(key, None)
                return
            now = self._clock()
            expires_at = None if ttl_seconds is None else now + ttl_seconds
            self._data[key] = (payload, expires_at)
            self._data.move_to_end(key)
            if len(self._data) > self._max_entries:
                # Expired entries go first, then the least recently used.
                for k in [k for k, (_, exp) in self._data.items()
                          if exp is not None and exp <= now]:
                    del self._data[k]
                while len(self._data) > self._max_entries:
                    self._data.popitem(last=False)

    async def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class RedisCache:
    """Redis-backed cache. Keys are stored under ``t2s:cache:``; errors count as misses."""

    def __init__(
        self,
        client,
        *,
        prefix: str = KEY_PREFIX + "cache:",
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._client = client
        self._prefix = prefix
        self._breaker = breaker or CircuitBreaker()

    def _failed(self, operation: str, exc: Exception) -> None:
        self._breaker.record_failure()
        logger.warning("Cache %s skipped: Redis unavailable (%s: %s)",
                       operation, type(exc).__name__, exc)

    async def get(self, key: str) -> Any | None:
        if self._breaker.is_open():
            return None
        try:
            payload = await self._client.get(self._prefix + key)
        except Exception as exc:  # noqa: BLE001 - fail open
            self._failed("get", exc)
            return None
        self._breaker.record_success()
        if payload is None:
            return None
        try:
            return json.loads(payload)
        except (TypeError, ValueError):
            logger.warning("Cache entry %r is not valid JSON; treating it as a miss", key)
            return None

    async def set(self, key: str, value: Any, ttl_seconds: float | None = None) -> None:
        payload = _dumps(value)   # serialisation errors are the caller's bug: let them raise
        if ttl_seconds is not None and ttl_seconds <= 0:
            await self.delete(key)
            return
        if self._breaker.is_open():
            return
        px = None if ttl_seconds is None else max(1, int(ttl_seconds * 1000))
        try:
            await self._client.set(self._prefix + key, payload, px=px)
        except Exception as exc:  # noqa: BLE001 - fail open
            self._failed("set", exc)
            return
        self._breaker.record_success()

    async def delete(self, key: str) -> None:
        if self._breaker.is_open():
            return
        try:
            await self._client.delete(self._prefix + key)
        except Exception as exc:  # noqa: BLE001 - fail open
            self._failed("delete", exc)
            return
        self._breaker.record_success()

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001
            pass


def _max_entries_from_env() -> int:
    try:
        return max(1, int(os.getenv(MAX_ENTRIES_ENV_VAR, DEFAULT_MAX_ENTRIES)))
    except ValueError:
        return DEFAULT_MAX_ENTRIES


_cache: Cache | None = None
_cache_lock = threading.Lock()


def get_cache() -> Cache:
    """Process-wide cache: Redis when ``T2S_REDIS_URL`` is set, otherwise in-process."""
    global _cache
    if _cache is None:
        with _cache_lock:
            if _cache is None:
                url = redis_url()
                if url:
                    _cache = RedisCache(create_redis_client(url))
                else:
                    _cache = InMemoryCache(max_entries=_max_entries_from_env())
    return _cache


def set_cache(cache: Cache | None) -> None:
    """Install a specific cache (tests, application startup)."""
    global _cache
    with _cache_lock:
        _cache = cache


def reset_cache() -> None:
    """Forget the singleton so the next ``get_cache()`` re-reads the environment."""
    set_cache(None)
