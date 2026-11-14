"""infra.cache: in-process and Redis (fakeredis) caches."""
import asyncio

import fakeredis
import pytest

from infra.cache import Cache, InMemoryCache, RedisCache, get_cache, reset_cache, set_cache


def _run(coro):
    return asyncio.run(coro)


VALUE = {"tables": ["Artist", "Album"], "n": 3, "nested": {"ok": True, "none": None}}


# --- in-memory ---------------------------------------------------------------

def test_memory_roundtrip_miss_and_delete():
    cache = InMemoryCache()

    async def scenario():
        assert await cache.get("missing") is None
        await cache.set("k", VALUE)
        got = await cache.get("k")
        await cache.delete("k")
        await cache.delete("k")                  # deleting twice is fine
        return got, await cache.get("k")

    got, after = _run(scenario())
    assert got == VALUE and after is None


def test_memory_returns_copies():
    cache = InMemoryCache()

    async def scenario():
        original = {"rows": [1, 2]}
        await cache.set("k", original)
        original["rows"].append(3)               # mutating the source afterwards
        first = await cache.get("k")
        first["rows"].append(99)                 # mutating a returned value
        return await cache.get("k")

    assert _run(scenario()) == {"rows": [1, 2]}


def test_memory_ttl_expires(clock):
    cache = InMemoryCache(clock=clock)

    async def scenario():
        await cache.set("short", 1, ttl_seconds=5)
        await cache.set("forever", 2)
        clock.advance(4.9)
        before = await cache.get("short")
        clock.advance(0.2)
        return before, await cache.get("short"), await cache.get("forever"), len(cache)

    assert _run(scenario()) == (1, None, 2, 1)


def test_memory_non_positive_ttl_removes_the_key():
    cache = InMemoryCache()

    async def scenario():
        await cache.set("k", 1)
        await cache.set("k", 2, ttl_seconds=0)
        return await cache.get("k")

    assert _run(scenario()) is None


def test_memory_max_size_evicts_least_recently_used():
    cache = InMemoryCache(max_entries=3)

    async def scenario():
        for k in ("a", "b", "c"):
            await cache.set(k, k)
        await cache.get("a")                     # 'a' is now the most recently used
        await cache.set("d", "d")                # evicts 'b'
        return [await cache.get(k) for k in ("a", "b", "c", "d")], len(cache)

    assert _run(scenario()) == (["a", None, "c", "d"], 3)


def test_memory_expired_entries_are_evicted_before_live_ones(clock):
    cache = InMemoryCache(max_entries=2, clock=clock)

    async def scenario():
        await cache.set("live", 1)
        await cache.set("stale", 2, ttl_seconds=1)
        clock.advance(2)
        await cache.set("new", 3)
        return await cache.get("live"), await cache.get("new")

    assert _run(scenario()) == (1, 3)


def test_memory_rejects_non_json_values():
    with pytest.raises(TypeError):
        _run(InMemoryCache().set("k", object()))


def test_memory_concurrent_writers_stay_within_max_size():
    cache = InMemoryCache(max_entries=20)

    async def scenario():
        await asyncio.gather(*(cache.set(f"k{i}", i) for i in range(200)))
        return len(cache)

    assert _run(scenario()) == 20


# --- redis (fakeredis) -------------------------------------------------------

def test_redis_roundtrip_prefix_ttl_and_delete():
    async def scenario():
        client = fakeredis.FakeAsyncRedis(decode_responses=True)
        cache = RedisCache(client)
        assert await cache.get("schema:abc") is None
        await cache.set("schema:abc", VALUE, ttl_seconds=30)
        await cache.set("plain", [1, 2, 3])
        got = await cache.get("schema:abc")
        keys = sorted(await client.keys("*"))
        raw = await client.get("t2s:cache:plain")
        ttl = await client.pttl("t2s:cache:schema:abc")
        no_ttl = await client.pttl("t2s:cache:plain")
        await cache.delete("schema:abc")
        after = await cache.get("schema:abc")
        await cache.set("plain", 1, ttl_seconds=0)
        gone = await cache.get("plain")
        await client.aclose()
        return got, keys, raw, ttl, no_ttl, after, gone

    got, keys, raw, ttl, no_ttl, after, gone = _run(scenario())
    assert got == VALUE
    assert keys == ["t2s:cache:plain", "t2s:cache:schema:abc"]
    assert raw == "[1,2,3]", "values are stored as JSON text"
    assert 0 < ttl <= 30_000
    assert no_ttl == -1
    assert after is None and gone is None


def test_redis_corrupt_entry_is_a_miss():
    async def scenario():
        client = fakeredis.FakeAsyncRedis(decode_responses=True)
        await client.set("t2s:cache:k", "{not json")
        got = await RedisCache(client).get("k")
        await client.aclose()
        return got

    assert _run(scenario()) is None


def test_redis_rejects_non_json_values():
    async def scenario():
        client = fakeredis.FakeAsyncRedis(decode_responses=True)
        try:
            await RedisCache(client).set("k", {1, 2})
        finally:
            await client.aclose()

    with pytest.raises(TypeError):
        _run(scenario())


def test_redis_unreachable_fails_open(broken_redis):
    cache = RedisCache(broken_redis)

    async def scenario():
        await cache.set("k", VALUE)              # must not raise
        got = await cache.get("k")               # miss
        await cache.delete("k")                  # must not raise
        return got

    assert _run(scenario()) is None
    assert broken_redis.calls == 1, "after one failure the breaker skips Redis"


# --- factory ------------------------------------------------------------------

def test_factory_defaults_to_memory_and_is_a_singleton(monkeypatch):
    monkeypatch.setenv("T2S_CACHE_MAX_ENTRIES", "2")
    cache = get_cache()
    assert isinstance(cache, InMemoryCache) and isinstance(cache, Cache)
    assert get_cache() is cache

    async def fill():
        for i in range(5):
            await cache.set(str(i), i)
        return len(cache)

    assert _run(fill()) == 2
    reset_cache()
    assert get_cache() is not cache


def test_factory_uses_redis_when_url_is_set_and_unreachable_server_is_a_miss(monkeypatch):
    monkeypatch.setenv("T2S_REDIS_URL", "redis://127.0.0.1:1/0")   # nothing listens there
    cache = get_cache()
    assert isinstance(cache, RedisCache)

    async def scenario():
        try:
            await cache.set("k", 1)
            return await cache.get("k")
        finally:
            await cache.aclose()

    assert _run(scenario()) is None


def test_set_cache_injects_an_instance():
    mine = InMemoryCache()
    set_cache(mine)
    assert get_cache() is mine
