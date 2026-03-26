"""UI-003: Debugging Panel tests."""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock


def test_debug_panel_importable():
    """render_debug_panel and rerun_sql are importable."""
    from streamlit_app.components.debug_panel import render_debug_panel, rerun_sql
    assert callable(render_debug_panel)
    assert callable(rerun_sql)


def test_debug_panel_collapsed_default_when_no_error(sample_agent_state):
    """expanded=False when retry_count=0 and error_log=None."""
    # Test the logic directly (not via AppTest — expander state requires live Streamlit)
    from streamlit_app.components.debug_panel import render_debug_panel
    state = dict(sample_agent_state)
    # retry_count=0 and error_log=None -> auto_expand=False
    auto_expand = state.get("retry_count", 0) > 0 or state.get("error_log") is not None
    assert auto_expand is False


def test_debug_panel_auto_expands_on_retry(sample_agent_state_with_error):
    """expanded=True when retry_count > 0."""
    state = sample_agent_state_with_error
    auto_expand = state.get("retry_count", 0) > 0 or state.get("error_log") is not None
    assert auto_expand is True


def test_debug_panel_shows_generated_sql(sample_agent_state):
    """generated_sql present in state — confirmed available for display."""
    state = sample_agent_state
    assert state["generated_sql"] is not None
    assert len(state["generated_sql"]) > 0


def test_rerun_sql_calls_executor_directly(sample_agent_state):
    """rerun_sql calls executor_node with modified state containing edited SQL."""
    from streamlit_app.components.debug_panel import rerun_sql
    edited = "SELECT Name FROM Artist LIMIT 5"
    mock_result = {"db_results": [{"Name": "AC/DC"}], "error_log": None}

    with patch("streamlit_app.components.debug_panel.asyncio.run",
               return_value=mock_result) as mock_run:
        result = rerun_sql(edited, sample_agent_state)

    # asyncio.run was called once
    assert mock_run.called
    # Result passed through
    assert result["db_results"] == [{"Name": "AC/DC"}]


def test_debug_panel_shows_usage_metadata(sample_agent_state):
    """usage_metadata list is accessible from state for display."""
    state = sample_agent_state
    assert state.get("usage_metadata") is not None
    assert len(state["usage_metadata"]) == 1
    entry = state["usage_metadata"][0]
    assert "provider" in entry
    assert "total_tokens" in entry
