"""End-to-end smoke test for the full UI flow."""
import pytest


@pytest.mark.skip(reason="Wave 3 stub — implement after app.py wired (08-04-PLAN completes)")
def test_app_starts_without_exception():
    """AppTest.from_file('streamlit_app/app.py').run() raises no exception."""
    pass


@pytest.mark.skip(reason="Wave 3 stub — implement after app.py wired (08-04-PLAN completes)")
def test_full_query_flow_end_to_end(mock_graph):
    """Submit query -> mock graph returns state -> assistant message with final_answer rendered."""
    pass
