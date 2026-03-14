import pytest


def make_initial_state(user_query: str = "test query") -> dict:
    """Return a complete AgentState dict with all keys set. Use a fresh copy per test."""
    return {
        "user_query": user_query,
        "db_type": "sqlite",
        "db_manager": None,
        "query_type": None,
        "schema": None,
        "relevant_tables": None,
        "query_plan": None,
        "generated_sql": None,
        "db_results": None,
        "error_log": None,
        "retry_count": 0,
        "final_answer": None,
    }
