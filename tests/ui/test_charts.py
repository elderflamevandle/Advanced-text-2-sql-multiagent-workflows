"""UI-004: Visualization Dashboard tests."""
import pytest


@pytest.mark.skip(reason="Wave 2 stub — implement after charts.py created (08-04-PLAN)")
def test_timeseries_detection_returns_line_type():
    """detect_chart_type() returns 'Line' when a date/time column is present."""
    pass


@pytest.mark.skip(reason="Wave 2 stub — implement after charts.py created (08-04-PLAN)")
def test_categorical_detection_returns_bar_type():
    """detect_chart_type() returns 'Bar' for string+numeric columns without datetime."""
    pass


@pytest.mark.skip(reason="Wave 2 stub — implement after charts.py created (08-04-PLAN)")
def test_chart_renders_without_error(sample_agent_state):
    """render_chart() completes without raising for valid db_results."""
    pass


@pytest.mark.skip(reason="Wave 2 stub — implement after charts.py created (08-04-PLAN)")
def test_csv_download_data_is_valid_csv(sample_agent_state):
    """DataFrame.to_csv() output for db_results is valid CSV with headers."""
    pass
