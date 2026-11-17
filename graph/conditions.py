import logging

from graph.state import AgentState

logger = logging.getLogger(__name__)

DEFAULT_MAX_CORRECTION_RETRIES = 2

LLM_FAILURE = "llm_all_providers_failed"

# Errors that rewriting the SQL cannot fix: the correction loop is skipped for these.
NON_CORRECTABLE_ERROR_TYPES = frozenset({
    LLM_FAILURE,
    "blocked_query",
    "no_connection",
    "user_rejected",
})


def get_max_correction_retries() -> int:
    """Return app.max_correction_retries from config/config.yaml (cached by file mtime)."""
    from llm.config import get_section
    value = get_section("app").get("max_correction_retries", DEFAULT_MAX_CORRECTION_RETRIES)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        logger.warning("Invalid app.max_correction_retries=%r; using %d",
                       value, DEFAULT_MAX_CORRECTION_RETRIES)
        return DEFAULT_MAX_CORRECTION_RETRIES


def _error_type(state: AgentState) -> str | None:
    """Return error_log.error_type when error_log is a structured dict, else None."""
    error_log = state.get("error_log")
    if isinstance(error_log, dict):
        return error_log.get("error_type")
    return None


def llm_failed(state: AgentState) -> bool:
    """True when a node reported that no LLM provider could serve its request."""
    return _error_type(state) == LLM_FAILURE


def route_after_gatekeeper(state: AgentState) -> str:
    """Route to schema_linker for SQL/follow_up, formatter for conversational/ambiguous."""
    if llm_failed(state):
        return "formatter"
    if state.get("query_type") in ("conversational", "ambiguous"):
        return "formatter"
    return "schema_linker"


def route_unless_llm_failed(state: AgentState) -> str:
    """Used after LLM-calling nodes: 'formatter' on an LLM outage, otherwise 'continue'."""
    return "formatter" if llm_failed(state) else "continue"


def route_after_planner(state: AgentState) -> str:
    """'formatter' on an LLM outage or when the planner asks the user a clarifying
    question (no SQL is generated), otherwise 'continue'."""
    if llm_failed(state):
        return "formatter"
    if state.get("query_type") == "clarification" and state.get("clarification_question"):
        return "formatter"
    return "continue"


def route_after_correction_plan(state: AgentState) -> str:
    """'formatter' on an LLM outage; 'schema_linker' when the diagnosis asks for a new
    table selection (missing table/column, at most once per query); else 'continue'."""
    if llm_failed(state):
        return "formatter"
    plan = state.get("correction_plan")
    if isinstance(plan, dict) and plan.get("action") == "reselect":
        return "schema_linker"
    return "continue"


def route_after_hitl(state: AgentState) -> str:
    """Route to executor on approval, formatter on rejection."""
    status = state.get("approval_status")
    if status == "rejected" or _error_type(state) == "user_rejected":
        return "formatter"
    return "executor"  # approved, auto_approved, or None


def route_after_executor(state: AgentState) -> str:
    """Route to the correction loop on a correctable error while retries remain."""
    if state.get("error_log") is None:
        return "formatter"  # success
    if _error_type(state) in NON_CORRECTABLE_ERROR_TYPES:
        return "formatter"  # rewriting the SQL cannot help
    if (state.get("retry_count") or 0) < get_max_correction_retries():
        return "correction_plan"
    return "formatter"  # retries exhausted
