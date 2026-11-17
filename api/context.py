"""Request-scoped context variables shared by the middleware, auth and logging."""
from __future__ import annotations

import logging
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("t2s_request_id", default=None)
tenant_id_var: ContextVar[str | None] = ContextVar("t2s_tenant_id", default=None)

_FACTORY_FLAG = "_t2s_request_context"


def install_log_record_factory() -> None:
    """Add ``request_id`` and ``tenant_id`` attributes to every log record (idempotent)."""
    current = logging.getLogRecordFactory()
    if getattr(current, _FACTORY_FLAG, False):
        return

    def factory(*args, **kwargs):
        record = current(*args, **kwargs)
        record.request_id = request_id_var.get() or "-"
        record.tenant_id = tenant_id_var.get() or "-"
        return record

    setattr(factory, _FACTORY_FLAG, True)
    logging.setLogRecordFactory(factory)
