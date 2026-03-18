"""UI-004: Visualization Dashboard tests."""
import pytest
import pandas as pd


def test_detect_chart_type_returns_line_for_date_column():
    """detect_chart_type returns 'Line' when a date/time column is present."""
    from streamlit_app.components.charts import detect_chart_type
    results = [{"date": "2024-01", "revenue": 1200}, {"date": "2024-02", "revenue": 1500}]
    assert detect_chart_type(results) == "Line"


def test_detect_chart_type_returns_bar_for_categorical():
    """detect_chart_type returns 'Bar' for string+numeric columns without datetime."""
    from streamlit_app.components.charts import detect_chart_type
    results = [{"artist": "AC/DC", "total": 4.95}, {"artist": "Accept", "total": 3.96}]
    assert detect_chart_type(results) == "Bar"


def test_detect_chart_type_returns_none_for_no_chart():
    """detect_chart_type returns None for single-column or all-numeric data."""
    from streamlit_app.components.charts import detect_chart_type
    assert detect_chart_type([]) is None
    assert detect_chart_type([{"value": 42}]) is None  # only 1 column
    assert detect_chart_type([{"a": 1, "b": 2}]) is None  # all numeric, no string


def test_chart_module_importable():
    """charts.py imports without error (plotly installed in Plan 01)."""
    from streamlit_app.components.charts import detect_chart_type, render_chart_with_toggle
    assert callable(detect_chart_type)
    assert callable(render_chart_with_toggle)


def test_csv_download_data_valid(sample_agent_state):
    """DataFrame(db_results).to_csv() produces valid CSV with headers."""
    db_results = sample_agent_state["db_results"]
    df = pd.DataFrame(db_results)
    csv = df.to_csv(index=False)
    assert "Name" in csv or "total" in csv  # headers present
    lines = csv.strip().split("\n")
    assert len(lines) > 1  # header + at least one data row
