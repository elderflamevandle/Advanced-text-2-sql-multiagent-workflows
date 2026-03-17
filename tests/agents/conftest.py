def make_agent_state(user_query: str = "test query", db_manager: object = None) -> dict:
    """Return a complete AgentState dict with all 19 fields. Use a fresh copy per test."""
    if db_manager is None:
        db_manager = object()  # non-None = connected
    return {
        "user_query": user_query,
        "resolved_query": None,
        "db_type": "sqlite",
        "db_manager": db_manager,
        "query_type": None,
        "schema": {"Invoice": {"columns": [{"name": "Total", "type": "NUMERIC"}]}},
        "relevant_tables": ["Invoice", "InvoiceLine", "Customer", "Artist", "Track"],
        "query_plan": None,
        "generated_sql": None,
        "sql_explanation": None,
        "db_results": None,
        "execution_metadata": None,
        "approval_status": None,
        "error_log": None,
        "correction_plan": None,
        "sql_history": None,
        "retry_count": 0,
        "final_answer": None,
    }
