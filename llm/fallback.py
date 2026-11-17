"""LLM fallback chain: Groq -> OpenAI -> Ollama with per-call token/cost tracking."""
import asyncio
import logging
import os
import weakref

from llm.errors import LLMUnavailableError

logger = logging.getLogger(__name__)

# Pause between providers so a rate-limited account is not hammered. Tests set this to 0.
RETRY_DELAY_SECONDS = 1.0


def _model_name(llm) -> str:
    """Return the configured model name of a LangChain chat model as a plain string."""
    for attr in ("model_name", "model"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value:
            return value
    return "unknown"


class FallbackClient:
    """Groq -> OpenAI -> Ollama fallback chain with per-call token/cost tracking.

    Exposes ainvoke() and astream() matching the LangChain Runnable interface
    so agent nodes use it as a drop-in for ChatGroq.

    A provider passed as None (for example because its API key is missing) is skipped.
    When no provider can serve the request, LLMUnavailableError is raised.
    """

    def __init__(self, groq_llm, openai_llm, ollama_llm, tracker, node_name: str,
                 state: dict | None = None):
        candidates = [
            ("groq", groq_llm),
            ("openai", openai_llm),
            ("ollama", ollama_llm),
        ]
        self._providers = [(name, llm) for name, llm in candidates if llm is not None]
        self._tracker = tracker
        self._node_name = node_name
        # State the client was created for (get_llm(node, state)). Usage is recorded on it
        # when a call does not pass state= explicitly, which most nodes do not.
        self._state = state

    @property
    def provider_names(self) -> list[str]:
        return [name for name, _ in self._providers]

    def _record(self, provider_name: str, llm, usage: dict | None, state: dict | None) -> None:
        usage = usage if isinstance(usage, dict) else {}
        self._tracker.record(
            provider=provider_name,
            model=_model_name(llm),
            node_name=self._node_name,
            input_tokens=usage.get("input_tokens", 0) or 0,
            output_tokens=usage.get("output_tokens", 0) or 0,
            state=state if state is not None else self._state,
        )

    def _unavailable(self, last_exc: Exception | None) -> LLMUnavailableError:
        if not self._providers:
            message = "No LLM provider is configured (no API key set and Ollama unavailable)."
        else:
            message = (
                f"All LLM providers failed ({', '.join(self.provider_names)}). "
                f"Last error: {type(last_exc).__name__}: {last_exc}"
            )
        logger.error("FallbackClient: node=%s %s", self._node_name, message)
        return LLMUnavailableError(message, last_error=last_exc,
                                   providers_tried=self.provider_names)

    async def ainvoke(self, messages: list, state: dict | None = None):
        """Try each provider in order and return the first response.

        Raises:
            LLMUnavailableError: every provider failed, or none is configured.
        """
        last_exc: Exception | None = None
        last_index = len(self._providers) - 1
        for index, (provider_name, llm) in enumerate(self._providers):
            try:
                response = await llm.ainvoke(messages)
            except Exception as exc:  # noqa: BLE001 - any provider failure triggers fallback
                logger.warning("FallbackClient: %s failed (%s: %s); trying next provider",
                               provider_name, type(exc).__name__, exc)
                last_exc = exc
                if index < last_index and RETRY_DELAY_SECONDS > 0:
                    await asyncio.sleep(RETRY_DELAY_SECONDS)
                continue
            self._record(provider_name, llm, getattr(response, "usage_metadata", None), state)
            if index > 0:
                logger.warning("FallbackClient: served by %s (earlier providers failed) node=%s",
                               provider_name, self._node_name)
            else:
                logger.info("FallbackClient: served by %s node=%s",
                            provider_name, self._node_name)
            return response
        raise self._unavailable(last_exc)

    async def astream(self, messages: list, state: dict | None = None):
        """Stream str chunks, falling back to the next provider on failure.

        A provider is only abandoned if it fails before yielding anything; a failure after
        partial output is raised, because replaying on another provider would duplicate text.

        Raises:
            LLMUnavailableError: every provider failed, or none is configured.
        """
        last_exc: Exception | None = None
        for provider_name, llm in self._providers:
            yielded = False
            final_chunk = None
            try:
                async for chunk in llm.astream(messages):
                    text = chunk.content if hasattr(chunk, "content") else str(chunk)
                    final_chunk = chunk
                    if text:
                        yielded = True
                        yield text
            except Exception as exc:  # noqa: BLE001
                if yielded:
                    logger.error("FallbackClient.astream: %s failed mid-stream (%s)",
                                 provider_name, exc)
                    raise LLMUnavailableError(
                        f"LLM provider {provider_name} failed mid-stream: {exc}",
                        last_error=exc, providers_tried=[provider_name],
                    ) from exc
                logger.warning("FallbackClient.astream: %s failed (%s: %s); trying next",
                               provider_name, type(exc).__name__, exc)
                last_exc = exc
                continue
            self._record(provider_name, llm, getattr(final_chunk, "usage_metadata", None), state)
            return
        raise self._unavailable(last_exc)


# ---------------------------------------------------------------------------
# Provider construction and caching
# ---------------------------------------------------------------------------

# Provider clients hold async HTTP connections that belong to one event loop, and the
# Streamlit app runs every graph invocation in a fresh loop. Clients are therefore cached
# per event loop: all nodes of one graph run share them, and nothing is reused across loops.
_loop_caches: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_no_loop_cache: dict = {}


def _client_cache() -> dict:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return _no_loop_cache
    return _loop_caches.setdefault(loop, {})


def clear_llm_cache() -> None:
    """Drop every cached provider client (used by tests and after config changes)."""
    _loop_caches.clear()
    _no_loop_cache.clear()


def _load_llm_config() -> dict:
    """Return the `llm` section of config/config.yaml (cached by file mtime)."""
    from llm.config import get_section
    return get_section("llm")


def _cached(key: tuple, factory):
    cache = _client_cache()
    if key not in cache:
        cache[key] = factory()
    return cache[key]


def _resolve_api_key(provider: str, env_name: str) -> tuple[str, bool]:
    """Return ``(api_key, request_scoped)`` for a provider.

    A key set with ``llm.context.use_llm_credentials`` wins over the environment.
    An empty string set there disables the provider for that request.
    """
    from llm.context import get_llm_credential
    scoped = get_llm_credential(provider)
    if scoped is not None:
        return scoped.strip(), True
    return os.environ.get(env_name, "").strip(), False


def _build_groq(cfg: dict):
    api_key, scoped = _resolve_api_key("groq", "GROQ_API_KEY")
    if not api_key:
        logger.debug("get_llm: no Groq API key; skipping Groq")
        return None
    from llm.groq_client import _make_groq_llm
    # The API key is part of the cache key: two tenants never share a client.
    key = ("groq", cfg.get("groq_model"), cfg.get("request_timeout"), api_key)
    if scoped:
        return _cached(key, lambda: _make_groq_llm(cfg, api_key=api_key))
    return _cached(key, lambda: _make_groq_llm(cfg))


def _build_openai(cfg: dict, complexity: str):
    api_key, scoped = _resolve_api_key("openai", "OPENAI_API_KEY")
    if not api_key:
        logger.debug("get_llm: no OpenAI API key; skipping OpenAI")
        return None
    from llm.openai_client import _make_openai_llm
    tier = "complex" if complexity == "complex" else "simple"
    model = cfg.get("openai_model_complex" if tier == "complex" else "openai_model_default")
    key = ("openai", tier, model, cfg.get("request_timeout"), api_key)
    if scoped:
        return _cached(key, lambda: _make_openai_llm(cfg, complexity=tier, api_key=api_key))
    return _cached(key, lambda: _make_openai_llm(cfg, complexity=tier))


def _make_ollama_llm(cfg: dict):
    from langchain_ollama import ChatOllama
    return ChatOllama(
        model=cfg.get("ollama_model", "qwen3:8b"),
        base_url=cfg.get("ollama_base_url", "http://localhost:11434"),
        temperature=0,
    )


def _build_ollama(cfg: dict):
    if cfg.get("ollama_enabled", True) is False:
        return None
    key = ("ollama", cfg.get("ollama_model"), cfg.get("ollama_base_url"))
    return _cached(key, lambda: _make_ollama_llm(cfg))


def _safe_build(name: str, builder):
    """Run a provider builder; a provider that cannot be constructed is skipped, not fatal."""
    try:
        return builder()
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_llm: could not construct %s client (%s: %s); skipping",
                       name, type(exc).__name__, exc)
        return None


def get_llm(node: str, state: dict | None = None) -> "FallbackClient":
    """Factory: returns a configured FallbackClient. Called inside agent node function bodies.

    Args:
        node: Name of the calling node (e.g. 'gatekeeper', 'sql_generator'); recorded in
              usage_metadata.
        state: Current AgentState dict. Used to read query_plan.complexity for
               OpenAI model selection (gpt-4o vs gpt-4o-mini).

    Providers without an API key are left out of the chain. The returned client raises
    LLMUnavailableError on use if no provider can serve the request.
    """
    from llm.usage_tracker import UsageTracker

    cfg = _load_llm_config()

    query_plan = (state or {}).get("query_plan")
    complexity = "simple"
    if isinstance(query_plan, dict):
        complexity = query_plan.get("complexity", "simple")

    groq_llm = _safe_build("groq", lambda: _build_groq(cfg))
    openai_llm = _safe_build("openai", lambda: _build_openai(cfg, complexity))
    ollama_llm = _safe_build("ollama", lambda: _build_ollama(cfg))

    return FallbackClient(groq_llm, openai_llm, ollama_llm, UsageTracker(), node_name=node,
                          state=state)
