import pytest


def make_initial_state(user_query: str = "test query") -> dict:
    """Return a complete AgentState dict with all keys set. Use a fresh copy per test."""
    return {
        "user_query": user_query,
        "resolved_query": None,
        "db_type": "sqlite",
        "db_manager": None,
        "query_type": None,
        "schema": None,
        "relevant_tables": None,
        "query_plan": None,
        "generated_sql": None,
        "sql_explanation": None,
        "db_results": None,
        "execution_metadata": None,
        "approval_status": None,
        "error_log": None,
        "retry_count": 0,
        "final_answer": None,
    }
