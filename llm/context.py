"""Request-scoped LLM provider credentials (bring-your-own-key per tenant).

``use_llm_credentials`` stores keys in ``contextvars``, so they follow the
current request across ``await`` and into tasks created from it
(``asyncio.create_task``, ``asyncio.gather``, ``asyncio.to_thread``), and never
leak into other requests running on the same event loop. ``llm.fallback.get_llm``
prefers these keys over ``os.environ``.

Note: a plain ``ThreadPoolExecutor.submit`` / ``loop.run_in_executor`` does not
copy the context; use ``asyncio.to_thread`` or ``contextvars.copy_context().run``.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_KEYS: dict[str, ContextVar[str | None]] = {
    "groq": ContextVar("t2s_groq_api_key", default=None),
    "openai": ContextVar("t2s_openai_api_key", default=None),
}


def get_llm_credential(provider: str) -> str | None:
    """Return the request-scoped key for ``provider`` ("groq"/"openai"), or None."""
    var = _KEYS.get(provider)
    return var.get() if var is not None else None


@contextmanager
def use_llm_credentials(groq_api_key: str | None = None,
                        openai_api_key: str | None = None) -> Iterator[None]:
    """Use these provider keys for LLM calls made inside the block.

    ``None`` leaves a provider unchanged: it inherits an enclosing block's key,
    or falls back to the process environment. An empty string disables the
    provider inside the block (no environment fallback), for tenants that must
    never run on the operator's key.
    """
    tokens = []
    try:
        for provider, value in (("groq", groq_api_key), ("openai", openai_api_key)):
            if value is not None:
                tokens.append((_KEYS[provider], _KEYS[provider].set(value)))
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
