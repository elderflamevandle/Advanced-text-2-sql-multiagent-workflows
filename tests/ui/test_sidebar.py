"""UI-001: Configuration Sidebar tests."""
import pytest


@pytest.mark.skip(reason="Wave 1 stub — implement after sidebar.py created (08-02-PLAN)")
def test_sidebar_renders_db_type_dropdown():
    """DB type selectbox with options DuckDB/MySQL/PostgreSQL/SQLite is present."""
    pass


@pytest.mark.skip(reason="Wave 1 stub — implement after sidebar.py created (08-02-PLAN)")
def test_sidebar_renders_api_key_inputs():
    """Groq, OpenAI, Pinecone password inputs present in sidebar."""
    pass


@pytest.mark.skip(reason="Wave 1 stub — implement after sidebar.py created (08-02-PLAN)")
def test_connect_button_triggers_db_manager():
    """Connect button calls DatabaseManager.get_schema() and shows table count."""
    pass


@pytest.mark.skip(reason="Wave 1 stub — implement after sidebar.py created (08-02-PLAN)")
def test_sidebar_prefills_api_keys_from_env(monkeypatch):
    """API key inputs pre-filled from GROQ_API_KEY / OPENAI_API_KEY env vars."""
    pass


@pytest.mark.skip(reason="Wave 1 stub — implement after sidebar.py created (08-02-PLAN)")
def test_hitl_toggle_present():
    """HITL on/off toggle is present and defaults to config.yaml value."""
    pass
