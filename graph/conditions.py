from graph.state import AgentState


def route_after_gatekeeper(state: AgentState) -> str:
    """Route to schema_linker for SQL/follow_up, formatter for conversational/ambiguous."""
    if state.get("query_type") in ("conversational", "ambiguous"):
        return "formatter"
    return "schema_linker"


def route_after_executor(state: AgentState) -> str:
    """Route to correction loop on error (up to max_retries=2), else formatter."""
    if state.get("error_log") is not None:
        if state.get("retry_count", 0) < 2:
            return "correction_plan"
        return "formatter"  # retries exhausted
    return "formatter"  # success
