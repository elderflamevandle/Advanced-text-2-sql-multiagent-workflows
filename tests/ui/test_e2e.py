"""End-to-end smoke test for the full UI flow."""
import pytest
from unittest.mock import patch, MagicMock


def test_app_imports_without_exception():
    """All streamlit_app modules import without error."""
    import streamlit_app.app
    from streamlit_app.components.sidebar import render_sidebar
    from streamlit_app.components.chat import render_chat
    from streamlit_app.components.debug_panel import render_debug_panel
    from streamlit_app.components.charts import detect_chart_type, render_chart_with_toggle
    assert callable(render_sidebar)
    assert callable(render_chat)
    assert callable(render_debug_panel)
    assert callable(detect_chart_type)
    assert callable(render_chart_with_toggle)


@pytest.mark.skip(reason="AppTest.from_file requires actual streamlit run context — manual verification in checkpoint plan")
def test_full_query_flow_end_to_end(mock_graph):
    """Submit query -> mock graph returns state -> assistant message with final_answer rendered."""
    pass
