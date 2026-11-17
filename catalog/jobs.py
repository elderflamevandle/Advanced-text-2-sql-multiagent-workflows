"""Background catalog builds: one at a time per connection, on a small shared pool."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone

from catalog import builder
from catalog.models import BUILDING, ERROR, MISSING, CatalogStatus

logger = logging.getLogger(__name__)

DEFAULT_BUILD_WORKERS = 2
DEFAULT_STALE_BUILD_SECONDS = 900.0
_STALE_MESSAGE = "The catalog build was interrupted. Start it again."

_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_running: dict[tuple[str, str], Future] = {}


def _config() -> dict:
    try:
        from llm.config import get_section  # noqa: PLC0415
        return get_section("catalog")
    except Exception:  # noqa: BLE001
        return {}


def _number(key: str, default: float) -> float:
    try:
        return float(_config().get(key, default))
    except (TypeError, ValueError):
        return default


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        workers = max(1, int(_number("build_workers", DEFAULT_BUILD_WORKERS)))
        _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="catalog-build")
    return _executor


def _store():
    from catalog.store import get_catalog_store  # noqa: PLC0415
    return get_catalog_store()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_seconds(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


def is_running(tenant_id, connection_id) -> bool:
    """True while this process is building the connection's catalog."""
    with _lock:
        future = _running.get((tenant_id, connection_id))
        return future is not None and not future.done()


def reconcile(tenant_id, connection_id, *, store=None) -> CatalogStatus:
    """Current status, after failing a "building" row nobody is working on any more.

    A build writes a heartbeat with every status update. A row that still says building,
    is not running in this process and has had no heartbeat for
    ``catalog.stale_build_seconds`` (default 900) was left by a crashed process.
    """
    store = store or _store()
    status = store.status(tenant_id, connection_id)
    if status.state != BUILDING or is_running(tenant_id, connection_id):
        return status
    info = store.build_info(tenant_id, connection_id)
    age = _age_seconds(info.get("updated_at") or status.started_at)
    if age is None or age >= _number("stale_build_seconds", DEFAULT_STALE_BUILD_SECONDS):
        logger.warning("Catalog build of connection=%s looks abandoned; marking it failed",
                       connection_id)
        return builder.record_failure(store, tenant_id, connection_id, _STALE_MESSAGE)
    return status


def _work(tenant_id, connection_id, force: bool) -> CatalogStatus:
    try:
        return builder.build_catalog(tenant_id, connection_id, force=force)
    except BaseException as exc:  # noqa: BLE001 - nothing may escape the worker
        logger.error("Catalog build worker failed (%s)", type(exc).__name__)
        try:
            return builder.record_failure(_store(), tenant_id, connection_id,
                                          f"Catalog build failed ({type(exc).__name__}).")
        except BaseException:  # noqa: BLE001
            return CatalogStatus(state=ERROR, error="Catalog build failed.")


def start_build(tenant_id, connection_id, *, force=False) -> CatalogStatus:
    """Start a build in the background and return the status at once.

    If a build of this connection is already running (here, or in another process with a
    live heartbeat) no second one is started and its current status is returned. Unknown
    and foreign connections return ``state="missing"`` and leave no trace.
    """
    store = _store()
    try:
        from database.registry import get_registry  # noqa: PLC0415
        get_registry().get_info(tenant_id, connection_id)
    except LookupError:
        return CatalogStatus(state=MISSING)
    except Exception as exc:  # noqa: BLE001
        logger.warning("start_build: cannot resolve connection (%s)", type(exc).__name__)
        return CatalogStatus(state=ERROR, error="The connection could not be resolved.")

    key = (tenant_id, connection_id)
    with _lock:
        future = _running.get(key)
        if future is not None and not future.done():
            return store.status(tenant_id, connection_id)
    if reconcile(tenant_id, connection_id, store=store).state == BUILDING:
        return store.status(tenant_id, connection_id)   # another process is on it
    with _lock:
        future = _running.get(key)
        if future is not None and not future.done():
            return store.status(tenant_id, connection_id)
        try:
            store.set_status(tenant_id, connection_id, state=BUILDING, progress=0.0,
                             error=None, started_at=_now(), finished_at=None)
            status = store.status(tenant_id, connection_id)
            future = _get_executor().submit(_work, tenant_id, connection_id, bool(force))
            _running[key] = future
        except Exception as exc:  # noqa: BLE001
            logger.error("start_build: could not start (%s)", type(exc).__name__)
            return builder.record_failure(store, tenant_id, connection_id,
                                          "The catalog build could not be started.")

    def forget(done: Future) -> None:
        with _lock:
            if _running.get(key) is done:
                _running.pop(key, None)

    future.add_done_callback(forget)   # outside the lock: it may run right here
    return status


def wait_for_build(tenant_id, connection_id, timeout=None) -> CatalogStatus:
    """Block until the running build (if any) ends, then return the status."""
    with _lock:
        future = _running.get((tenant_id, connection_id))
    if future is not None:
        try:
            future.result(timeout=timeout)
        except FutureTimeout:
            pass
        except Exception:  # noqa: BLE001 - _work does not raise; be safe anyway
            logger.debug("wait_for_build: worker raised", exc_info=True)
    return _store().status(tenant_id, connection_id)


def shutdown(wait: bool = True) -> None:
    """Stop the worker pool (tests, process exit). A new one is created on demand."""
    global _executor
    with _lock:
        executor, _executor = _executor, None
    if executor is not None:
        executor.shutdown(wait=wait)
    with _lock:
        _running.clear()
