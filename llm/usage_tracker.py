import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Rates: (input_cost_per_1k_tokens, output_cost_per_1k_tokens) in USD
# Source: groq.com/pricing and openai.com/api/pricing (March 2026)
COST_TABLE: dict[str, tuple[float, float]] = {
    "llama-3.3-70b-versatile": (0.00059, 0.00079),
    "llama-3.1-8b-instant":    (0.00005, 0.00008),
    "gpt-4o-mini":             (0.00015, 0.00060),
    "gpt-4o":                  (0.00250, 0.01000),
    "qwen3:8b":                (0.0, 0.0),
}


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = COST_TABLE.get(model)
    if rates is None and isinstance(model, str):
        # Providers may report dated variants (e.g. "gpt-4o-mini-2024-07-18"):
        # fall back to the longest known model name that prefixes it.
        matches = [name for name in COST_TABLE if model.startswith(name)]
        rates = COST_TABLE[max(matches, key=len)] if matches else None
    if rates is None:
        rates = (0.0, 0.0)
    return (input_tokens / 1000) * rates[0] + (output_tokens / 1000) * rates[1]


class UsageTracker:
    """In-memory per-query token/cost accumulator. Phase 9 adds DB persistence."""

    def __init__(self):
        self._entries: list[dict] = []

    def record(
        self,
        provider: str,
        model: str,
        node_name: str,
        input_tokens: int,
        output_tokens: int,
        state: dict | None = None,
    ) -> dict:
        entry = {
            "provider": provider,
            "model": model,
            "node_name": node_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "estimated_cost_usd": calculate_cost(model, input_tokens, output_tokens),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._entries.append(entry)
        if state is not None:
            # Rebinds the key on the dict the node was given. LangGraph does not persist
            # in-place state mutation; graph.resilience.resilient_node hands each node a
            # shallow copy and returns the entries added here as a state update.
            existing = state.get("usage_metadata") or []
            state["usage_metadata"] = list(existing) + [entry]
        logger.debug("UsageTracker.record: %s/%s node=%s tokens=%d cost=$%.6f",
                     provider, model, node_name, input_tokens + output_tokens,
                     entry["estimated_cost_usd"])
        return entry

    @property
    def entries(self) -> list[dict]:
        return list(self._entries)

    @property
    def total_cost_usd(self) -> float:
        return sum(e["estimated_cost_usd"] for e in self._entries)
