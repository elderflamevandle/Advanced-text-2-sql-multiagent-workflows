"""Configuration sidebar — renders all 4 section groups into st.sidebar."""
import logging
import uuid

import streamlit as st
import yaml

logger = logging.getLogger(__name__)

# Survive "New Session / Clear Chat": the chat is cleared, the database stays connected.
_CONNECTION_KEYS = ("tenant_id", "connection_id", "connection_db_type", "db_manager",
                    "_connect_status")

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
            creds = {"db_path": file_path}
        elif db_type == "SQLite":
            file_path = st.text_input("File Path", value="data/chinook.db", key="sqlite_path")
            creds = {"db_path": file_path}
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
            for key in list(st.session_state.keys()):
                if key not in _CONNECTION_KEYS:
                    del st.session_state[key]
            st.rerun()


def _session_tenant_id() -> str:
    """Tenant id of this browser session (created on first use)."""
    tenant_id = st.session_state.get("tenant_id")
    if not tenant_id:
        tenant_id = "ui-" + uuid.uuid4().hex
        st.session_state["tenant_id"] = tenant_id
    return tenant_id


def _get_registry():
    """The connection registry, or None when it cannot be used in this process.

    None selects the legacy path (one process-wide database). That happens when the
    registry is not implemented or not configured, for example a persistent
    T2S_METADATA_URL without T2S_SECRET_KEY.
    """
    try:
        from database.registry import get_registry
        return get_registry()
    except Exception as exc:  # noqa: BLE001 - any failure here means "no registry"
        logger.warning(
            "Connection registry unavailable (%s: %s): using one shared database "
            "connection for all sessions", type(exc).__name__, exc,
        )
        return None


def _connect_with_registry(registry, db_type: str, creds: dict) -> dict:
    """Register this session's connection and return its schema."""
    tenant_id = _session_tenant_id()
    previous = st.session_state.get("connection_id")
    info = registry.register(tenant_id, f"ui-{uuid.uuid4().hex[:8]}", db_type, dict(creds))
    try:
        schema = registry.get_schema(tenant_id, info.id)
    except Exception:
        _delete_connection(registry, tenant_id, info.id)
        raise
    st.session_state["connection_id"] = info.id
    st.session_state["connection_db_type"] = info.db_type
    st.session_state["db_manager"] = None
    # The old connection goes only after the new one works, so a failed reconnect
    # leaves the session with the database it had.
    if previous and previous != info.id:
        _delete_connection(registry, tenant_id, previous)
    return schema


def _delete_connection(registry, tenant_id: str, connection_id: str) -> None:
    try:
        registry.delete(tenant_id, connection_id)
    except Exception as exc:  # noqa: BLE001 - already gone is fine
        logger.debug("Could not delete connection %s: %s", connection_id, exc)


def _connect_legacy(db_type: str, creds: dict) -> dict:
    """Pre-registry behaviour: one DatabaseManager shared by the whole process."""
    from database.manager import DatabaseManager, set_active_manager

    mgr = DatabaseManager(db_type, **creds)
    schema = mgr.get_schema()
    set_active_manager(mgr)
    st.session_state["db_manager"] = mgr  # kept for UI checks only
    st.session_state["connection_id"] = None
    st.session_state["connection_db_type"] = db_type
    return schema


def _do_connect(db_type: str, creds: dict):
    """Connect this session to a database and store the outcome in session_state."""
    try:
        registry = _get_registry()
        if registry is None:
            schema = _connect_legacy(db_type, creds)
        else:
            schema = _connect_with_registry(registry, db_type, creds)
        st.session_state["_connect_status"] = {"ok": True, "tables": len(schema)}
    except Exception as exc:
        st.session_state["_connect_status"] = {"ok": False, "error": str(exc)[:120]}
