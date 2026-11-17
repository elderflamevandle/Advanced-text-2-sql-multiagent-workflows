"""Redis plumbing shared by infra.cache and infra.ratelimit."""
from __future__ import annotations

import os
import time

REDIS_URL_ENV_VAR = "T2S_REDIS_URL"
KEY_PREFIX = "t2s:"

# Short timeouts: a slow Redis must not hold up every request.
_CONNECT_TIMEOUT_SECONDS = 1.0
_SOCKET_TIMEOUT_SECONDS = 1.0


def redis_url() -> str | None:
    value = (os.getenv(REDIS_URL_ENV_VAR) or "").strip()
    return value or None


def create_redis_client(url: str):
    """Build a ``redis.asyncio`` client. Nothing is connected until the first command."""
    try:
        from redis import asyncio as aioredis
    except ImportError as exc:
        raise RuntimeError(
            f"{REDIS_URL_ENV_VAR} is set but the 'redis' package is not installed "
            "(pip install -e '.[service]')."
        ) from exc
    return aioredis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
    )


class CircuitBreaker:
    """After a failure, skip Redis for ``cooldown_seconds`` instead of timing out per call."""

    def __init__(self, cooldown_seconds: float = 5.0, clock=time.monotonic) -> None:
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._open_until = 0.0

    @property
    def cooldown_seconds(self) -> float:
        return self._cooldown

    def is_open(self) -> bool:
        return self._clock() < self._open_until

    def record_failure(self) -> None:
        self._open_until = self._clock() + self._cooldown

    def record_success(self) -> None:
        self._open_until = 0.0
