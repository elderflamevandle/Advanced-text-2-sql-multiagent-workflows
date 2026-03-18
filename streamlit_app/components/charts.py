"""Visualization dashboard — auto chart detection and Plotly rendering with type toggle."""
import streamlit as st
import plotly.express as px
import pandas as pd


_DATE_KEYWORDS = {"date", "time", "year", "month", "day", "week", "timestamp", "period", "quarter"}


def detect_chart_type(results: list[dict]) -> str | None:
    """Detect appropriate chart type from column names and data types.

    Returns: "Line" for time-series, "Bar" for categorical, None if no chart appropriate.

    Strategy:
      1. If any column name contains a date/time keyword -> "Line" (time-series)
      2. If there is at least one string column AND at least one numeric column -> "Bar"
      3. Otherwise -> None (show table only)
    """
    if not results:
        return None

    df = pd.DataFrame(results)
    if len(df.columns) < 2:
        return None

    col_names_lower = [c.lower() for c in df.columns]

    # Check for time-series columns by name
    for col in col_names_lower:
        for kw in _DATE_KEYWORDS:
            if kw in col:
                return "Line"

    # Check for categorical: at least one string column + one numeric
    has_string = any(df[c].dtype == object for c in df.columns)
    has_numeric = any(pd.api.types.is_numeric_dtype(df[c]) for c in df.columns)
    if has_string and has_numeric:
        return "Bar"

    return None


def _select_axes(df: pd.DataFrame, chart_type: str) -> tuple[str, str]:
    """Auto-select x and y columns for the chart.

    For Line: x = first date/time-keyword column, y = first numeric
    For Bar: x = first string (object) column, y = first numeric
    """
    numeric_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    string_cols = [c for c in df.columns if df[c].dtype == object]

    if chart_type == "Line":
        date_cols = [c for c in df.columns
                     if any(kw in c.lower() for kw in _DATE_KEYWORDS)]
        x_col = date_cols[0] if date_cols else df.columns[0]
    else:
        x_col = string_cols[0] if string_cols else df.columns[0]

    y_col = numeric_cols[0] if numeric_cols else df.columns[-1]
    return x_col, y_col


def render_chart_with_toggle(results: list[dict], state: dict):
    """Render a Plotly chart with a Line/Bar/Table toggle below it.

    Auto-detects chart type from column names. Shows nothing if no chart is appropriate
    and only one representation (table) would be shown anyway — table is already rendered
    by chat.py's st.dataframe call.
    """
    if not results:
        return

    auto_type = detect_chart_type(results)
    if auto_type is None:
        return  # No chart-worthy structure; table already shown by chat.py

    df = pd.DataFrame(results)
    # Chart type toggle — default to auto-detected type
    # Use a unique key to avoid widget conflicts between different messages
    toggle_key = f"chart_toggle_{id(results)}"
    chart_type = st.radio(
        "Chart type",
        ["Line", "Bar", "Table"],
        index=["Line", "Bar", "Table"].index(auto_type),
        horizontal=True,
        key=toggle_key,
        label_visibility="collapsed",
    )

    if chart_type == "Table":
        return  # Table already rendered by chat.py

    x_col, y_col = _select_axes(df, chart_type)
    try:
        if chart_type == "Line":
            fig = px.line(df, x=x_col, y=y_col, title=f"{y_col} over {x_col}")
        else:
            fig = px.bar(df, x=x_col, y=y_col, title=f"{y_col} by {x_col}")
        st.plotly_chart(fig, use_container_width=True)
    except Exception as exc:
        st.caption(f"Chart rendering failed: {exc}")
