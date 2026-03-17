"""LLM fallback chain: Groq -> OpenAI -> Ollama with per-call token/cost tracking."""
import asyncio
import logging
import yaml

logger = logging.getLogger(__name__)


def _groq_exc():
    """Return the tuple of Groq exception classes (lazy import)."""
    import groq
    return (groq.RateLimitError, groq.APITimeoutError,
            groq.APIConnectionError, groq.InternalServerError)


def _openai_exc():
    """Return the tuple of OpenAI exception classes (lazy import)."""
    import openai
    return (openai.RateLimitError, openai.APITimeoutError,
            openai.APIConnectionError, openai.InternalServerError)


class FallbackClient:
    """Groq -> OpenAI -> Ollama fallback chain with per-call token/cost tracking.

    Exposes ainvoke() and astream() matching the LangChain Runnable interface
    so agent nodes use it as a drop-in for ChatGroq.
    """

    def __init__(self, groq_llm, openai_llm, ollama_llm, tracker, node_name: str):
        self._providers = [
            ("groq", groq_llm),
            ("openai", openai_llm),
            ("ollama", ollama_llm),
        ]
        self._tracker = tracker
        self._node_name = node_name

    async def ainvoke(self, messages: list, state: dict | None = None):
        """Try each provider in order. Return structured error dict on all-fail (never raises)."""
        last_exc = None
        for provider_name, llm in self._providers:
            try:
                response = await llm.ainvoke(messages)
                usage = response.usage_metadata or {}
                self._tracker.record(
                    provider=provider_name,
                    model=getattr(llm, "model_name", getattr(llm, "model", "unknown")),
                    node_name=self._node_name,
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    state=state,
                )
                if provider_name != "groq":
                    logger.warning("FallbackClient: served by %s (Groq unavailable)", provider_name)
                else:
                    logger.info("FallbackClient: served by groq node=%s", self._node_name)
                return response
            except _groq_exc() as exc:
                logger.warning("FallbackClient: Groq failed (%s) — trying OpenAI", type(exc).__name__)
                last_exc = exc
                await asyncio.sleep(1)
            except _openai_exc() as exc:
                logger.warning("FallbackClient: OpenAI failed (%s) — trying Ollama", type(exc).__name__)
                last_exc = exc
                await asyncio.sleep(1)
            except Exception as exc:
                logger.warning("FallbackClient: %s failed (%s) — trying next provider",
                               provider_name, type(exc).__name__)
                last_exc = exc
                await asyncio.sleep(1)

        logger.error("FallbackClient: all 3 providers failed. last_error=%s", last_exc)
        return {
            "error_type": "llm_all_providers_failed",
            "message": f"All LLM providers exhausted: {last_exc}",
            "dialect": None,
            "failed_sql": None,
            "hint": "Check GROQ_API_KEY, OPENAI_API_KEY, and Ollama server status.",
        }

    async def astream(self, messages: list, state: dict | None = None):
        """Stream interface for Phase 8 Streamlit chat. Delegates to ainvoke and yields chunks.

        For full streaming with token-level chunks, upgrade this in Phase 8
        to delegate to llm.astream() and aggregate the final chunk for usage_metadata.
        For Phase 7, wraps ainvoke() so the interface exists and callers can consume it.
        """
        response = await self.ainvoke(messages, state=state)
        yield response


def get_llm(node: str, state: dict | None = None) -> "FallbackClient":
    """Factory: returns a configured FallbackClient. Called inside agent node function bodies.

    Args:
        node: Name of the calling node (e.g. 'gatekeeper', 'sql_generator').
              Used in usage_metadata for debugging panel in Phase 8.
        state: Current AgentState dict. Used to read query_plan.complexity for
               OpenAI model selection (gpt-4o vs gpt-4o-mini).
    """
    from llm.groq_client import _make_groq_llm
    from llm.openai_client import _make_openai_llm
    from langchain_ollama import ChatOllama
    from llm.usage_tracker import UsageTracker

    cfg = yaml.safe_load(open("config/config.yaml"))["llm"]  # fresh read — allows test patching

    groq_llm = _make_groq_llm(cfg)

    # Determine OpenAI model: gpt-4o for complex plans, gpt-4o-mini otherwise
    query_plan = (state or {}).get("query_plan")
    complexity = "simple"
    if isinstance(query_plan, dict):
        complexity = query_plan.get("complexity", "simple")
    openai_llm = _make_openai_llm(cfg, complexity=complexity)

    ollama_llm = ChatOllama(
        model=cfg.get("ollama_model", "qwen3:8b"),
        base_url=cfg.get("ollama_base_url", "http://localhost:11434"),
        temperature=0,
    )

    tracker = UsageTracker()
    return FallbackClient(groq_llm, openai_llm, ollama_llm, tracker, node_name=node)
