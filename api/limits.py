"""Per-tenant in-flight cap and per-thread exclusivity (in-process, single event loop)."""
from __future__ import annotations


class Permit:
    """Held for the duration of one graph run; ``release`` is idempotent."""

    def __init__(self, limiter: "QueryAdmission", tenant_id: str, thread_key: str):
        self._limiter = limiter
        self._tenant_id = tenant_id
        self._thread_key = thread_key
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._limiter._release(self._tenant_id, self._thread_key)


class QueryAdmission:
    """Counts running queries per tenant and keeps one run per thread at a time.

    Equivalent to a non-blocking semaphore per tenant: a request over the cap is refused
    immediately instead of queued. All calls happen on the event loop, so no lock is needed.
    """

    def __init__(self, max_concurrent: int):
        self.max_concurrent = max_concurrent
        self._in_flight: dict[str, int] = {}
        self._busy_threads: set[str] = set()

    def in_flight(self, tenant_id: str) -> int:
        return self._in_flight.get(tenant_id, 0)

    def try_acquire(self, tenant_id: str, thread_key: str) -> tuple[Permit | None, str | None]:
        """Return (permit, None) or (None, reason) with reason "concurrency" | "thread_busy"."""
        if self._in_flight.get(tenant_id, 0) >= self.max_concurrent:
            return None, "concurrency"
        if thread_key in self._busy_threads:
            return None, "thread_busy"
        self._in_flight[tenant_id] = self._in_flight.get(tenant_id, 0) + 1
        self._busy_threads.add(thread_key)
        return Permit(self, tenant_id, thread_key), None

    def _release(self, tenant_id: str, thread_key: str) -> None:
        self._busy_threads.discard(thread_key)
        remaining = self._in_flight.get(tenant_id, 0) - 1
        if remaining > 0:
            self._in_flight[tenant_id] = remaining
        else:
            self._in_flight.pop(tenant_id, None)
