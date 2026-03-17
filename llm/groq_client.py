import logging

logger = logging.getLogger(__name__)


def _make_groq_llm(cfg: dict):
    """Factory: returns a ChatGroq instance from config dict. Lazy import."""
    from langchain_groq import ChatGroq
    model = cfg.get("groq_model", "llama-3.3-70b-versatile")
    timeout = cfg.get("request_timeout", 60)
    logger.debug("_make_groq_llm: model=%s timeout=%s", model, timeout)
    return ChatGroq(model=model, temperature=0, max_tokens=2048, timeout=timeout)
