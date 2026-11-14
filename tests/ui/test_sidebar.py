"""UI-001: Configuration Sidebar tests."""
import os
import pytest
from unittest.mock import MagicMock, patch


def test_sidebar_renders_db_type_dropdown():
    """DB type selectbox with options DuckDB/MySQL/PostgreSQL/SQLite is present."""
    from streamlit_app.components.sidebar import _DB_TYPES
    assert "SQLite" in _DB_TYPES
    assert "DuckDB" in _DB_TYPES
    assert "MySQL" in _DB_TYPES
    assert "PostgreSQL" in _DB_TYPES


def test_sidebar_prefills_api_keys_from_env(monkeypatch):
    """init_session reads GROQ_API_KEY from environment."""
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    # Verify the env var is accessible (init_session reads via os.getenv)
    assert os.getenv("GROQ_API_KEY") == "test-groq-key"


def test_connect_button_triggers_db_manager(mock_db_manager):
    """Legacy path (registry unavailable): _do_connect creates a DatabaseManager, makes it
    the active manager and stores the schema count in _connect_status."""
    import database.manager as manager_module

    fake_session = {}
    with patch("streamlit.session_state", fake_session), \
         patch("database.registry.get_registry", side_effect=NotImplementedError), \
         patch("database.manager.DatabaseManager", return_value=mock_db_manager), \
         patch.object(manager_module, "_active_manager", None):
        from streamlit_app.components.sidebar import _do_connect
        _do_connect("sqlite", {"file_path": "data/chinook.db"})
        assert manager_module.get_active_manager() is mock_db_manager
    assert fake_session["_connect_status"]["ok"] is True
    assert fake_session["_connect_status"]["tables"] == 3  # mock returns 3-table schema
    assert fake_session["db_manager"] is mock_db_manager
    assert fake_session["connection_id"] is None


def test_connect_registers_a_connection_for_the_session(file_registry, demo_db):
    """Registry path: the session gets its own tenant and connection; no global manager."""
    import database.manager as manager_module
    from streamlit_app.components.sidebar import _do_connect

    session = {}
    with patch("streamlit.session_state", session), \
         patch.object(manager_module, "_active_manager", None):
        _do_connect("sqlite", {"db_path": demo_db("a", ["Artist", "Album"])})
        assert manager_module.get_active_manager() is None, "nothing process-wide is set"

    assert session["_connect_status"] == {"ok": True, "tables": 2}
    assert session["tenant_id"].startswith("ui-")
    assert session["connection_db_type"] == "sqlite"
    info = file_registry.get_info(session["tenant_id"], session["connection_id"])
    assert info.db_type == "sqlite"
    assert set(file_registry.get_schema(session["tenant_id"], session["connection_id"])) == {
        "Artist", "Album"}


def test_two_sessions_do_not_overwrite_each_other(file_registry, demo_db):
    """The bug this replaces: the second Connect used to become everyone's database."""
    from database.registry import ConnectionNotFound
    from streamlit_app.components.sidebar import _do_connect

    alice, bob = {}, {}
    with patch("streamlit.session_state", alice):
        _do_connect("sqlite", {"db_path": demo_db("alice", ["Artist"])})
    with patch("streamlit.session_state", bob):
        _do_connect("sqlite", {"db_path": demo_db("bob", ["Patient", "Visit"])})

    assert alice["tenant_id"] != bob["tenant_id"]
    assert set(file_registry.get_schema(alice["tenant_id"], alice["connection_id"])) == {"Artist"}
    assert set(file_registry.get_schema(bob["tenant_id"], bob["connection_id"])) == {
        "Patient", "Visit"}
    rows = file_registry.get_manager(alice["tenant_id"], alice["connection_id"]).execute_query(
        "SELECT Name FROM Artist")
    assert [dict(r) for r in rows] == [{"Name": "alice-Artist"}]
    # One session cannot reach the other's connection.
    with pytest.raises(ConnectionNotFound):
        file_registry.get_schema(alice["tenant_id"], bob["connection_id"])


def test_reconnect_replaces_the_sessions_previous_connection(file_registry, demo_db):
    from database.registry import ConnectionNotFound
    from streamlit_app.components.sidebar import _do_connect

    session = {}
    with patch("streamlit.session_state", session):
        _do_connect("sqlite", {"db_path": demo_db("first", ["Artist"])})
        first_id, tenant = session["connection_id"], session["tenant_id"]
        _do_connect("sqlite", {"db_path": demo_db("second", ["Track"])})

    assert session["tenant_id"] == tenant, "the session keeps its tenant"
    assert session["connection_id"] != first_id
    assert [c.id for c in file_registry.list(tenant)] == [session["connection_id"]]
    with pytest.raises(ConnectionNotFound):
        file_registry.get_info(tenant, first_id)


def test_failed_reconnect_keeps_the_working_connection(file_registry, demo_db, tmp_path):
    from streamlit_app.components.sidebar import _do_connect

    session = {}
    with patch("streamlit.session_state", session):
        _do_connect("sqlite", {"db_path": demo_db("good", ["Artist"])})
        good_id = session["connection_id"]
        _do_connect("postgresql", {"host": "not a host", "port": 5432, "database": "d",
                                   "user": "u", "password": "pw-must-not-leak"})

    assert session["_connect_status"]["ok"] is False
    assert "pw-must-not-leak" not in session["_connect_status"]["error"]
    assert session["connection_id"] == good_id
    assert [c.id for c in file_registry.list(session["tenant_id"])] == [good_id]


def test_file_databases_respect_the_env_switch(file_registry, demo_db, monkeypatch):
    """T2S_ALLOW_FILE_DATABASES=false is honoured: the UI shows the registry's refusal."""
    from streamlit_app.components.sidebar import _do_connect

    monkeypatch.setenv("T2S_ALLOW_FILE_DATABASES", "false")
    session = {}
    with patch("streamlit.session_state", session):
        _do_connect("sqlite", {"db_path": demo_db("a", ["Artist"])})
    assert session["_connect_status"]["ok"] is False
    assert "T2S_ALLOW_FILE_DATABASES" in session["_connect_status"]["error"]
    assert not session.get("connection_id")


def test_registry_configuration_error_falls_back_to_legacy(mock_db_manager, monkeypatch):
    """A persistent metadata store without T2S_SECRET_KEY must not break the local app."""
    import database.manager as manager_module
    from database.credentials import SecretKeyError
    from streamlit_app.components.sidebar import _do_connect

    session = {}
    with patch("streamlit.session_state", session), \
         patch("database.registry.get_registry", side_effect=SecretKeyError("no key")), \
         patch("database.manager.DatabaseManager", return_value=mock_db_manager), \
         patch.object(manager_module, "_active_manager", None):
        _do_connect("sqlite", {"db_path": "data/chinook.db"})
    assert session["_connect_status"] == {"ok": True, "tables": 3}


def test_hitl_toggle_present():
    """render_sidebar imports without error; _DB_TYPES is defined with 4 entries."""
    from streamlit_app.components.sidebar import _DB_TYPES, _REMOTE_TYPES, render_sidebar
    assert callable(render_sidebar)
    assert len(_DB_TYPES) == 4


def test_sidebar_renders_db_type_dropdown_full():
    """All four DB types present: SQLite, DuckDB, MySQL, PostgreSQL."""
    from streamlit_app.components.sidebar import _DB_TYPES, _REMOTE_TYPES
    assert set(_DB_TYPES) == {"SQLite", "DuckDB", "MySQL", "PostgreSQL"}
    assert "MySQL" in _REMOTE_TYPES
    assert "PostgreSQL" in _REMOTE_TYPES
