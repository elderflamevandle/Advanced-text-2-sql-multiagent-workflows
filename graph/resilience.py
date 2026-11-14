"""Node wrapper applied to every node when the graph is built (see graph/builder.py).

resilient_node(fn) returns an async node that:

1. Calls the node with a shallow copy of the state, so in-place writes by the node
   (notably UsageTracker.record appending to state["usage_metadata"]) never touch the
   state object owned by LangGraph.
2. Catches LLMUnavailableError and turns it into a structured error_log update
   (error_type "llm_all_providers_failed") instead of crashing the run. Routing in
   graph/conditions.py then sends the run straight to the formatter.
3. Returns the usage entries the node added to its copy as a "usage_metadata" update.
   LangGraph persists only what a node returns, and AgentState.usage_metadata is a
   reducer field, so the entries accumulate across nodes.

The node function is looked up on its module at call time, so tests that patch
e.g. "agents.nodes.query_planner.query_planner_node" also affect the compiled graph.
"""
import functools
import logging
import sys

from llm.errors import LLMUnavailableError

logger = logging.getLogger(__name__)


def _resolve(fn):
    """Return the current module-level object named like fn (honours unittest.mock.patch)."""
    module = sys.modules.get(getattr(fn, "__module__", None))
    return getattr(module, getattr(fn, "__name__", ""), fn) if module else fn


def resilient_node(fn, name: str | None = None):
    """Wrap an async LangGraph node function. See module docstring for the behaviour."""
    node_name = name or getattr(fn, "__name__", "node")

    @functools.wraps(fn)
    async def wrapper(state):
        local_state = dict(state)
        seen = len(local_state.get("usage_metadata") or [])
        try:
            result = await _resolve(fn)(local_state)
        except LLMUnavailableError as exc:
            logger.error("%s: LLM unavailable: %s", node_name, exc)
            result = {"error_log": exc.to_error_log(dialect=local_state.get("db_type"))}

        update = dict(result) if isinstance(result, dict) else {}
        new_entries = list((local_state.get("usage_metadata") or [])[seen:])
        if "usage_metadata" in update:
            # A node that returns usage entries itself: treat them as new entries too.
            new_entries = list(update.pop("usage_metadata") or []) + new_entries
        if new_entries:
            update["usage_metadata"] = new_entries
        return update

    wrapper.__wrapped_node__ = fn
    return wrapper
