"""infra.ratelimit: in-process and Redis (fakeredis) sliding-window limiters."""
import asyncio
import threading

import fakeredis
import pytest

from infra.ratelimit import (
    InMemoryRateLimiter,
    RateLimiter,
    RedisRateLimiter,
    get_rate_limiter,
    reset_rate_limiter,
    set_rate_limiter,
)
from infra.redis_client import CircuitBreaker


def _run(coro):
    return asyncio.run(coro)


# --- in-memory ---------------------------------------------------------------

def test_memory_allows_up_to_limit_then_reports_retry_after(clock):
    limiter = InMemoryRateLimiter(clock=clock)

    async def scenario():
        results = []
        for _ in range(3):
            results.append(await limiter.allow("tenant-a", 3, 60))
            clock.advance(10)
        denied = await limiter.allow("tenant-a", 3, 60)   # t=30, oldest hit at t=0
        return results, denied

    results, denied = _run(scenario())
    assert results == [(True, 0.0)] * 3
    assert denied[0] is False
    assert denied[1] == pytest.approx(30.0)


def test_memory_window_slides_and_keys_are_independent(clock):
    limiter = InMemoryRateLimiter(clock=clock)

    async def scenario():
        assert (await limiter.allow("a", 1, 10))[0] is True
        assert (await limiter.allow("a", 1, 10))[0] is False
        assert (await limiter.allow("b", 1, 10))[0] is True    # another tenant is unaffected
        clock.advance(10.01)
        assert (await limiter.allow("a", 1, 10))[0] is True

    _run(scenario())


def test_memory_denied_requests_do_not_extend_the_window(clock):
    limiter = InMemoryRateLimiter(clock=clock)

    async def scenario():
        await limiter.allow("a", 1, 10)
        for _ in range(50):                      # hammering while limited
            clock.advance(0.1)
            assert (await limiter.allow("a", 1, 10))[0] is False
        clock.advance(5.01)                      # 10.01s after the one allowed request
        return await limiter.allow("a", 1, 10)

    assert _run(scenario()) == (True, 0.0)


def test_memory_zero_limit_always_denies(clock):
    limiter = InMemoryRateLimiter(clock=clock)
    assert _run(limiter.allow("a", 0, 60)) == (False, 60.0)


def test_memory_100_concurrent_calls_never_exceed_limit():
    limiter = InMemoryRateLimiter()

    async def scenario():
        return await asyncio.gather(*(limiter.allow("k", 10, 60) for _ in range(100)))

    results = _run(scenario())
    assert sum(1 for ok, _ in results if ok) == 10
    assert all(retry > 0 for ok, retry in results if not ok)


def test_memory_is_safe_across_threads():
    limiter = InMemoryRateLimiter()
    allowed = []

    def worker():
        for _ in range(50):
            if asyncio.run(limiter.allow("k", 25, 60))[0]:
                allowed.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(allowed) == 25


def test_memory_idle_keys_expire(clock):
    limiter = InMemoryRateLimiter(clock=clock, sweep_interval_seconds=5)

    async def scenario():
        for i in range(200):
            await limiter.allow(f"k{i}", 5, 10)
        assert len(limiter) == 200
        clock.advance(11)                        # every window has passed
        await limiter.allow("fresh", 5, 10)      # the next call sweeps
        return len(limiter)

    assert _run(scenario()) == 1


def test_memory_key_count_is_capped(clock):
    limiter = InMemoryRateLimiter(clock=clock, max_keys=50)

    async def scenario():
        for i in range(500):
            await limiter.allow(f"k{i}", 5, 3600)
        return len(limiter)

    assert _run(scenario()) == 50


# --- redis (fakeredis) -------------------------------------------------------

def _fake_redis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


def test_redis_allows_up_to_limit_then_denies_with_retry_after(clock):
    async def scenario():
        client = _fake_redis()
        limiter = RedisRateLimiter(client, clock=clock)
        first = [await limiter.allow("tenant-a", 2, 60) for _ in range(2)]
        clock.advance(20)
        denied = await limiter.allow("tenant-a", 2, 60)
        other = await limiter.allow("tenant-b", 2, 60)
        keys = sorted(await client.keys("*"))
        ttl = await client.pttl("t2s:rl:tenant-a")
        size = await client.zcard("t2s:rl:tenant-a")
        clock.advance(40.01)
        after = await limiter.allow("tenant-a", 2, 60)
        await client.aclose()
        return first, denied, other, keys, ttl, size, after

    first, denied, other, keys, ttl, size, after = _run(scenario())
    assert first == [(True, 0.0)] * 2
    assert denied[0] is False and denied[1] == pytest.approx(40.0, abs=0.01)
    assert other == (True, 0.0)
    assert keys == ["t2s:rl:tenant-a", "t2s:rl:tenant-b"], "keys carry the t2s: prefix"
    assert 0 < ttl <= 61_000, "idle keys must expire on their own"
    assert size == 2, "a rejected request leaves no entry behind"
    assert after == (True, 0.0)


def test_redis_100_concurrent_calls_never_exceed_limit():
    async def scenario():
        client = _fake_redis()
        limiter = RedisRateLimiter(client)
        results = await asyncio.gather(*(limiter.allow("k", 10, 60) for _ in range(100)))
        await client.aclose()
        return results

    results = _run(scenario())
    assert sum(1 for ok, _ in results if ok) == 10


def test_redis_limit_is_shared_between_limiter_instances():
    """Two API workers pointing at the same Redis share one budget."""
    async def scenario():
        server = fakeredis.FakeServer()
        c1 = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
        c2 = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
        w1, w2 = RedisRateLimiter(c1), RedisRateLimiter(c2)
        results = await asyncio.gather(
            *(w.allow("k", 10, 60) for _ in range(50) for w in (w1, w2)))
        await c1.aclose()
        await c2.aclose()
        return results

    assert sum(1 for ok, _ in _run(scenario()) if ok) == 10


# --- redis unreachable --------------------------------------------------------

def test_default_fail_mode_falls_back_to_local_limit(broken_redis):
    limiter = RedisRateLimiter(broken_redis)
    assert limiter.fail_mode == "local"

    async def scenario():
        return [await limiter.allow("k", 3, 60) for _ in range(5)]

    results = _run(scenario())
    assert [ok for ok, _ in results] == [True, True, True, False, False]


def test_fail_open_allows_everything(broken_redis):
    limiter = RedisRateLimiter(broken_redis, fail_mode="open")
    assert _run(limiter.allow("k", 1, 60)) == (True, 0.0)
    assert _run(limiter.allow("k", 1, 60)) == (True, 0.0)


def test_fail_closed_rejects_with_retry_after(broken_redis):
    limiter = RedisRateLimiter(broken_redis, fail_mode="closed")
    allowed, retry_after = _run(limiter.allow("k", 100, 60))
    assert allowed is False and retry_after > 0


def test_unknown_fail_mode_is_rejected(broken_redis):
    with pytest.raises(ValueError):
        RedisRateLimiter(broken_redis, fail_mode="maybe")


def test_breaker_skips_redis_during_cooldown_then_recovers(clock):
    class Flaky:
        """Fails while ``down`` is set, otherwise behaves like Redis."""

        def __init__(self):
            self.real = _fake_redis()
            self.down = True
            self.attempts = 0

        def pipeline(self, *a, **k):
            self.attempts += 1
            if self.down:
                raise ConnectionError("down")
            return self.real.pipeline(*a, **k)

        async def zrem(self, *a, **k):
            return await self.real.zrem(*a, **k)

    async def scenario():
        client = Flaky()
        breaker = CircuitBreaker(cooldown_seconds=5, clock=clock)
        limiter = RedisRateLimiter(client, fail_mode="closed", breaker=breaker, clock=clock)
        assert (await limiter.allow("k", 5, 60))[0] is False
        assert (await limiter.allow("k", 5, 60))[0] is False
        assert client.attempts == 1, "the second call must not wait on Redis again"
        client.down = False
        clock.advance(5.1)
        assert (await limiter.allow("k", 5, 60)) == (True, 0.0)
        await client.real.aclose()

    _run(scenario())


# --- factory ------------------------------------------------------------------

def test_factory_defaults_to_memory_and_is_a_singleton():
    limiter = get_rate_limiter()
    assert isinstance(limiter, InMemoryRateLimiter)
    assert isinstance(limiter, RateLimiter)
    assert get_rate_limiter() is limiter
    reset_rate_limiter()
    assert get_rate_limiter() is not limiter


def test_factory_uses_redis_when_url_is_set_and_survives_unreachable_server(monkeypatch):
    monkeypatch.setenv("T2S_REDIS_URL", "redis://127.0.0.1:1/0")   # nothing listens there
    monkeypatch.setenv("T2S_RATE_LIMIT_FAIL_MODE", "closed")
    limiter = get_rate_limiter()
    assert isinstance(limiter, RedisRateLimiter)
    assert limiter.fail_mode == "closed"

    async def scenario():
        try:
            return await limiter.allow("k", 5, 60)
        finally:
            await limiter.aclose()

    assert _run(scenario())[0] is False


def test_factory_ignores_unknown_fail_mode(monkeypatch):
    monkeypatch.setenv("T2S_REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("T2S_RATE_LIMIT_FAIL_MODE", "whatever")
    assert get_rate_limiter().fail_mode == "local"


def test_set_rate_limiter_injects_an_instance():
    mine = InMemoryRateLimiter()
    set_rate_limiter(mine)
    assert get_rate_limiter() is mine
