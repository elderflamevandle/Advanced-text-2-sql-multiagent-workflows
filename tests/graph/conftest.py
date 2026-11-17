import pytest


def make_initial_state(user_query: str = "test query") -> dict:
    """Return a complete AgentState dict with all keys set. Use a fresh copy per test."""
    return {
        "user_query": user_query,
        "resolved_query": None,
        "db_type": "sqlite",
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
        "correction_plan": None,
        "sql_history": None,
        "usage_metadata": None,
        "retry_count": 0,
        "final_answer": None,
    }


@pytest.fixture(autouse=True)
def _no_real_catalog_or_memory():
    """Tests run without the schema catalog and the query memory unless they inject fakes
    (agents.services.set_*_for_tests, or create_app(catalog=..., memory=...))."""
    from agents import services

    services.set_catalog_service_for_tests(None)
    services.set_memory_service_for_tests(None)
    yield
    services.reset_services_for_tests()
