import logging

logger = logging.getLogger(__name__)


def _make_openai_llm(cfg: dict, complexity: str = "simple"):
    """Factory: returns a ChatOpenAI instance. Uses gpt-4o for complex queries."""
    from langchain_openai import ChatOpenAI
    if complexity == "complex":
        model = cfg.get("openai_model_complex", "gpt-4o")
    else:
        model = cfg.get("openai_model_default", "gpt-4o-mini")
    logger.debug("_make_openai_llm: model=%s (complexity=%s)", model, complexity)
    return ChatOpenAI(model=model, temperature=0, max_tokens=2048)
