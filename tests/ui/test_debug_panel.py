"""UI-003: Debugging Panel tests."""
import pytest


@pytest.mark.skip(reason="Wave 2 stub — implement after debug_panel.py created (08-04-PLAN)")
def test_debug_panel_collapsed_by_default(sample_agent_state):
    """Debug expander is collapsed when retry_count=0 and no error."""
    pass


@pytest.mark.skip(reason="Wave 2 stub — implement after debug_panel.py created (08-04-PLAN)")
def test_debug_panel_auto_expands_on_retry(sample_agent_state_with_error):
    """Debug expander is expanded when retry_count > 0."""
    pass


@pytest.mark.skip(reason="Wave 2 stub — implement after debug_panel.py created (08-04-PLAN)")
def test_debug_panel_shows_generated_sql(sample_agent_state):
    """generated_sql is displayed inside the expander."""
    pass


@pytest.mark.skip(reason="Wave 2 stub — implement after debug_panel.py created (08-04-PLAN)")
def test_edit_rerun_calls_executor_directly(sample_agent_state):
    """Edit & Rerun button invokes executor_node directly (not full graph)."""
    pass


@pytest.mark.skip(reason="Wave 2 stub — implement after debug_panel.py created (08-04-PLAN)")
def test_debug_panel_shows_usage_metadata(sample_agent_state):
    """LLM usage section displays provider/model/tokens/cost from usage_metadata list."""
    pass
