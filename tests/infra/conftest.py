import pytest


class FakeClock:
    """Manually advanced clock, usable as both monotonic and wall time."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class BrokenRedis:
    """Stands in for a Redis server that cannot be reached; counts the attempts."""

    def __init__(self) -> None:
        self.calls = 0

    def _fail(self, *args, **kwargs):
        self.calls += 1
        raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    async def get(self, *a, **k):
        self._fail()

    async def set(self, *a, **k):
        self._fail()

    async def delete(self, *a, **k):
        self._fail()

    async def zrem(self, *a, **k):
        self._fail()

    def pipeline(self, *a, **k):
        self._fail()

    async def aclose(self):
        pass


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def broken_redis():
    return BrokenRedis()


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch):
    from infra import cache, ratelimit

    monkeypatch.delenv("T2S_REDIS_URL", raising=False)
    monkeypatch.delenv("T2S_RATE_LIMIT_FAIL_MODE", raising=False)
    cache.reset_cache()
    ratelimit.reset_rate_limiter()
    yield
    cache.reset_cache()
    ratelimit.reset_rate_limiter()
