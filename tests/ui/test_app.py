"""Tests for streamlit_app/app.py — Task 1 TDD (08-02-PLAN)."""
import sys
import importlib
import uuid
import pytest
from unittest.mock import patch, MagicMock


class AttrDict(dict):
    """Dict that also supports attribute access — mirrors st.session_state behavior."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return MagicMock()

    def __setattr__(self, key, val):
        self[key] = val

    def __delattr__(self, key):
        del self[key]


def _make_st_mock():
    """Build a MagicMock that behaves like Streamlit's session_state."""
    st_mock = MagicMock()
    fake_state = AttrDict()
    st_mock.session_state = fake_state
    # Make `key in st.session_state` work
    st_mock.session_state.__contains__ = lambda self_inner, key: key in fake_state
    # Make st.columns(N) return N MagicMock columns (unpacking support)
    st_mock.columns.side_effect = lambda n: [MagicMock() for _ in range(n)]
    return st_mock, fake_state


def _import_app(st_mock=None, extra_mocks=None):
    """Import streamlit_app.app with optional streamlit mock, returning the module."""
    mods_to_clear = [m for m in sys.modules if m.startswith("streamlit_app.app")]
    for m in mods_to_clear:
        del sys.modules[m]

    patches = {}
    if st_mock is not None:
        patches["streamlit"] = st_mock
    if extra_mocks:
        patches.update(extra_mocks)

    with patch.dict(sys.modules, patches):
        app_mod = importlib.import_module("streamlit_app.app")
    return app_mod


def test_app_imports_without_error():
    """app.py can be imported without circular imports or missing deps."""
    st_mock, _ = _make_st_mock()
    builder_mock = MagicMock()
    builder_mock.build_graph.return_value = MagicMock()
    sidebar_mock = MagicMock()
    sidebar_mock.render_sidebar = MagicMock()

    app_mod = _import_app(
        st_mock=st_mock,
        extra_mocks={
            "graph.builder": builder_mock,
            "streamlit_app.components.sidebar": sidebar_mock,
        },
    )
    # If we get here without exception, import succeeded
    assert app_mod is not None


def test_get_graph_is_cache_resource():
    """get_graph() is defined, callable, and decorated with @st.cache_resource."""
    st_mock, _ = _make_st_mock()
    builder_mock = MagicMock()
    builder_mock.build_graph.return_value = MagicMock()
    sidebar_mock = MagicMock()
    sidebar_mock.render_sidebar = MagicMock()

    app_mod = _import_app(
        st_mock=st_mock,
        extra_mocks={
            "graph.builder": builder_mock,
            "streamlit_app.components.sidebar": sidebar_mock,
        },
    )

    assert callable(app_mod.get_graph)
    # st.cache_resource must have been called on the function
    assert st_mock.cache_resource.called


def test_init_session_sets_required_keys():
    """init_session() sets all required keys in st.session_state."""
    st_mock, fake_state = _make_st_mock()
    sidebar_mock = MagicMock()
    sidebar_mock.render_sidebar = MagicMock()

    app_mod = _import_app(
        st_mock=st_mock,
        extra_mocks={"streamlit_app.components.sidebar": sidebar_mock},
    )

    # Reset state, then call init_session directly
    fake_state.clear()
    with patch("streamlit_app.app.st", st_mock), \
         patch("streamlit_app.app.yaml") as mock_yaml, \
         patch("streamlit_app.app.os") as mock_os:
        mock_yaml.safe_load.return_value = {"hitl": {"enabled": True}}
        mock_os.getenv.return_value = ""
        app_mod.init_session()

    required_keys = [
        "messages", "thread_id", "db_manager",
        "session_tokens", "session_cost", "hitl_pending",
        "hitl_config", "hitl_decision", "hitl_enabled",
    ]
    for key in required_keys:
        assert key in fake_state, f"Missing key in session_state: {key}"


def test_reset_session_clears_and_reinitializes():
    """reset_session() clears all keys then re-initializes with fresh thread_id."""
    st_mock, fake_state = _make_st_mock()
    sidebar_mock = MagicMock()
    sidebar_mock.render_sidebar = MagicMock()

    app_mod = _import_app(
        st_mock=st_mock,
        extra_mocks={"streamlit_app.components.sidebar": sidebar_mock},
    )

    # Pre-populate state with a stale key
    fake_state["old_key"] = "old_value"
    fake_state["thread_id"] = "old-thread-id"

    with patch("streamlit_app.app.st", st_mock), \
         patch("streamlit_app.app.yaml") as mock_yaml, \
         patch("streamlit_app.app.os") as mock_os:
        mock_yaml.safe_load.return_value = {"hitl": {"enabled": True}}
        mock_os.getenv.return_value = ""
        app_mod.reset_session()

    # old_key must be gone
    assert "old_key" not in fake_state
    # thread_id must be re-initialized
    assert "thread_id" in fake_state
