"""Rate limiting (see docs/phase2-contracts.md).

Both implementations are sliding-window logs: a request is allowed when fewer than
``limit`` requests were allowed for the same key during the last ``window_seconds``.

``get_rate_limiter()`` returns the Redis limiter when ``T2S_REDIS_URL`` is set (limits are
then shared by every API process) and the in-process limiter otherwise.

When Redis is configured but unreachable
----------------------------------------
``T2S_RATE_LIMIT_FAIL_MODE`` decides what ``allow()`` does:

``local`` (default)
    Enforce the same limit with the in-process limiter of this worker until Redis answers
    again. Requests keep flowing and every worker still caps its own traffic, so the worst
    case is ``limit x number_of_workers`` instead of no limit at all.
``open``
    Allow everything. Only sensible when something else in front of the API limits traffic.
``closed``
    Reject everything (the caller turns that into HTTP 429). Choose this when an unmetered
    request is worse than an outage.

The default is ``local`` because the limiter protects LLM spend and customer databases:
failing open removes that protection exactly when the system is already degraded, and
failing closed turns a cache-tier outage into a full outage of the API.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import OrderedDict, deque
from typing import Callable, Protocol, runtime_checkable

from infra.redis_client import KEY_PREFIX, CircuitBreaker, create_redis_client, redis_url

logger = logging.getLogger(__name__)

FAIL_MODE_ENV_VAR = "T2S_RATE_LIMIT_FAIL_MODE"
FAIL_MODES = ("local", "open", "closed")
DEFAULT_FAIL_MODE = "local"


@runtime_checkable
class RateLimiter(Protocol):
    async def allow(self, key: str, limit: int, window_seconds: float) -> tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)``; retry_after is 0.0 when allowed."""
        ...


class InMemoryRateLimiter:
    """Sliding-window limiter for one process.

    ``allow`` never awaits while it holds the lock, so it is atomic for coroutines on one
    event loop and also for callers on different threads.

    Memory is bounded: a key stores at most ``limit`` timestamps, keys idle for longer than
    their window are dropped by a periodic sweep, and when more than ``max_keys`` keys are
    live the least recently used ones are evicted.
    """

    def __init__(
        self,
        *,
        max_keys: int = 10_000,
        sweep_interval_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_keys = max(1, max_keys)
        self._sweep_interval = sweep_interval_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # key -> (timestamps of allowed requests, window used on the last call)
        self._hits: OrderedDict[str, tuple[deque, float]] = OrderedDict()
        self._last_sweep = clock()

    async def allow(self, key: str, limit: int, window_seconds: float) -> tuple[bool, float]:
        return self.allow_nowait(key, limit, window_seconds)

    def allow_nowait(self, key: str, limit: int, window_seconds: float) -> tuple[bool, float]:
        if limit <= 0:
            return False, float(window_seconds)
        with self._lock:
            now = self._clock()
            if now - self._last_sweep >= self._sweep_interval:
                self._sweep(now)
            entry = self._hits.get(key)
            hits = entry[0] if entry else deque()
            cutoff = now - window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()
            # A lowered limit must not leave more history than the new limit needs.
            while len(hits) > limit:
                hits.popleft()
            if len(hits) < limit:
                hits.append(now)
                allowed, retry_after = True, 0.0
            else:
                allowed, retry_after = False, max(0.0, hits[0] + window_seconds - now)
            self._hits[key] = (hits, window_seconds)
            self._hits.move_to_end(key)
            while len(self._hits) > self._max_keys:
                self._hits.popitem(last=False)
            return allowed, retry_after

    def _sweep(self, now: float) -> None:
        self._last_sweep = now
        idle = [k for k, (hits, window) in self._hits.items()
                if not hits or hits[-1] <= now - window]
        for k in idle:
            del self._hits[k]

    def __len__(self) -> int:
        with self._lock:
            return len(self._hits)


class RedisRateLimiter:
    """Sliding-window limiter shared through Redis.

    One sorted set per key holds the timestamps of recent requests. Each call runs a single
    MULTI/EXEC transaction that trims expired entries, adds this request and counts the set.
    The request is allowed when the count, which already includes it and every concurrent
    request, is within the limit. Because every request is added before it is counted, two
    racing requests can never both take the last slot. A rejected request then removes its
    own entry; until it does, a concurrent request may be rejected although a slot was free,
    which errs on the safe side.

    Timestamps come from the calling process, so API hosts need roughly synchronised clocks.
    """

    def __init__(
        self,
        client,
        *,
        prefix: str = KEY_PREFIX + "rl:",
        fail_mode: str = DEFAULT_FAIL_MODE,
        breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if fail_mode not in FAIL_MODES:
            raise ValueError(f"fail_mode must be one of {FAIL_MODES}, got {fail_mode!r}")
        self._client = client
        self._prefix = prefix
        self._fail_mode = fail_mode
        self._breaker = breaker or CircuitBreaker()
        self._clock = clock
        self._local = InMemoryRateLimiter()

    @property
    def fail_mode(self) -> str:
        return self._fail_mode

    async def allow(self, key: str, limit: int, window_seconds: float) -> tuple[bool, float]:
        if limit <= 0:
            return False, float(window_seconds)
        if self._breaker.is_open():
            return self._on_failure(key, limit, window_seconds)
        try:
            result = await self._allow_redis(key, limit, window_seconds)
        except Exception as exc:  # noqa: BLE001 - any Redis/network error takes the fail path
            self._breaker.record_failure()
            logger.warning(
                "Rate limiter: Redis unavailable (%s: %s); fail mode '%s' for %.0fs",
                type(exc).__name__, exc, self._fail_mode, self._breaker.cooldown_seconds,
            )
            return self._on_failure(key, limit, window_seconds)
        self._breaker.record_success()
        return result

    async def _allow_redis(self, key: str, limit: int, window_seconds: float):
        redis_key = self._prefix + key
        now_ms = int(self._clock() * 1000)
        window_ms = max(1, int(window_seconds * 1000))
        member = f"{now_ms}-{uuid.uuid4().hex[:12]}"

        pipe = self._client.pipeline(transaction=True)
        pipe.zremrangebyscore(redis_key, 0, now_ms - window_ms)
        pipe.zadd(redis_key, {member: now_ms})
        pipe.zcard(redis_key)
        pipe.zrange(redis_key, 0, 0, withscores=True)
        pipe.pexpire(redis_key, window_ms + 1000)   # idle keys delete themselves
        _, _, count, oldest, _ = await pipe.execute()

        if count <= limit:
            return True, 0.0
        await self._client.zrem(redis_key, member)
        oldest_ms = float(oldest[0][1]) if oldest else float(now_ms)
        retry_after = max(0.0, (oldest_ms + window_ms - now_ms) / 1000.0)
        return False, retry_after

    def _on_failure(self, key: str, limit: int, window_seconds: float) -> tuple[bool, float]:
        if self._fail_mode == "open":
            return True, 0.0
        if self._fail_mode == "closed":
            return False, self._breaker.cooldown_seconds
        return self._local.allow_nowait(key, limit, window_seconds)

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001
            pass


def fail_mode_from_env() -> str:
    mode = (os.getenv(FAIL_MODE_ENV_VAR) or DEFAULT_FAIL_MODE).strip().lower()
    if mode not in FAIL_MODES:
        logger.warning("%s=%r is not one of %s; using '%s'",
                       FAIL_MODE_ENV_VAR, mode, FAIL_MODES, DEFAULT_FAIL_MODE)
        return DEFAULT_FAIL_MODE
    return mode


_limiter: RateLimiter | None = None
_limiter_lock = threading.Lock()


def get_rate_limiter() -> RateLimiter:
    """Process-wide limiter: Redis when ``T2S_REDIS_URL`` is set, otherwise in-process."""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                url = redis_url()
                if url:
                    _limiter = RedisRateLimiter(
                        create_redis_client(url), fail_mode=fail_mode_from_env()
                    )
                else:
                    _limiter = InMemoryRateLimiter()
    return _limiter


def set_rate_limiter(limiter: RateLimiter | None) -> None:
    """Install a specific limiter (tests, application startup)."""
    global _limiter
    with _limiter_lock:
        _limiter = limiter


def reset_rate_limiter() -> None:
    """Forget the singleton so the next ``get_rate_limiter()`` re-reads the environment."""
    set_rate_limiter(None)
