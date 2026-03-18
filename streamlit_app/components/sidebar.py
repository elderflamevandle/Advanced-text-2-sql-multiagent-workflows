"""Configuration sidebar — renders all 4 section groups into st.sidebar."""
import os

import streamlit as st
import yaml

_DB_TYPES = ["SQLite", "DuckDB", "MySQL", "PostgreSQL"]
_REMOTE_TYPES = {"MySQL", "PostgreSQL"}


def _load_config():
    """Load config/config.yaml, returning empty dict on any error."""
    try:
        return yaml.safe_load(open("config/config.yaml"))
    except Exception:
        return {}


def render_sidebar():
    """Render the full configuration sidebar into st.sidebar."""
    cfg = _load_config()
    llm_cfg = cfg.get("llm", {})

    with st.sidebar:
        st.title("Configuration")

        # --- Section 1: Database Connection ---
        st.header("Database")
        db_type = st.selectbox(
            "DB Type",
            _DB_TYPES,
            index=_DB_TYPES.index("SQLite"),
            key="sidebar_db_type",
        )

        creds = {}
        if db_type == "DuckDB":
            file_path = st.text_input("File Path", value="data/chinook.db", key="duckdb_path")
            creds = {"file_path": file_path}
        elif db_type == "SQLite":
            file_path = st.text_input("File Path", value="data/chinook.db", key="sqlite_path")
            creds = {"file_path": file_path}
        else:
            creds["host"] = st.text_input("Host", value="localhost", key="db_host")
            creds["port"] = st.number_input(
                "Port",
                value=5432 if db_type == "PostgreSQL" else 3306,
                min_value=1,
                max_value=65535,
                key="db_port",
            )
            creds["database"] = st.text_input("Database", key="db_name")
            creds["user"] = st.text_input("User", key="db_user")
            creds["password"] = st.text_input("Password", type="password", key="db_password")

        if st.button("Connect", key="connect_btn"):
            _do_connect(db_type.lower(), creds)

        if st.session_state.get("_connect_status"):
            status = st.session_state["_connect_status"]
            if status["ok"]:
                st.success(f"Connected ({status['tables']} tables)")
            else:
                st.error(f"Error: {status['error']}")

        st.divider()

        # --- Section 2: API Keys ---
        st.header("API Keys")
        st.session_state.groq_api_key = st.text_input(
            "Groq API Key",
            value=st.session_state.groq_api_key,
            type="password",
            key="groq_key_input",
        )
        st.session_state.openai_api_key = st.text_input(
            "OpenAI API Key",
            value=st.session_state.openai_api_key,
            type="password",
            key="openai_key_input",
        )
        st.session_state.pinecone_api_key = st.text_input(
            "Pinecone API Key",
            value=st.session_state.pinecone_api_key,
            type="password",
            key="pinecone_key_input",
        )

        st.divider()

        # --- Section 3: LLM Model Selector ---
        st.header("LLM Settings")
        providers = ["Groq", "OpenAI"]
        default_provider = (
            "Groq" if llm_cfg.get("primary_provider", "groq") == "groq" else "OpenAI"
        )
        provider = st.selectbox(
            "Primary Provider",
            providers,
            index=providers.index(default_provider),
            key="llm_provider",
        )

        if provider == "Groq":
            models = [llm_cfg.get("groq_model", "llama-3.3-70b-versatile")]
        else:
            models = [
                llm_cfg.get("openai_model_default", "gpt-4o-mini"),
                llm_cfg.get("openai_model_complex", "gpt-4o"),
            ]
        st.selectbox("Model", models, key="llm_model")

        st.divider()

        # --- Section 4: HITL + Session Controls ---
        st.header("Session")
        st.session_state.hitl_enabled = st.toggle(
            "Human-in-the-Loop Approval",
            value=st.session_state.hitl_enabled,
            key="hitl_toggle",
        )

        # Session cost ticker
        tokens = st.session_state.get("session_tokens", 0)
        cost = st.session_state.get("session_cost", 0.0)
        st.caption(f"Session: {tokens:,} tokens | ${cost:.4f}")

        if st.button("New Session / Clear Chat", key="clear_btn"):
            from streamlit_app.app import reset_session
            reset_session()
            st.rerun()


def _do_connect(db_type: str, creds: dict):
    """Test database connection and store status in session_state."""
    from database.manager import DatabaseManager

    try:
        mgr = DatabaseManager(db_type, **creds)
        schema = mgr.get_schema()
        st.session_state["db_manager"] = mgr
        st.session_state["_connect_status"] = {"ok": True, "tables": len(schema)}
    except Exception as exc:
        st.session_state["_connect_status"] = {"ok": False, "error": str(exc)[:120]}
