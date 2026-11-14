"""Text-to-SQL Assistant — Streamlit application entry point."""
import os
import sys
import uuid
from pathlib import Path

# Ensure project root is on sys.path so that `database`, `graph`, `llm`, etc. are importable
# regardless of which directory Streamlit is launched from.
_PROJECT_ROOT = str(Path(__file__).parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)



def _configure_local_defaults():
    """Environment defaults for the Streamlit process (values already set always win).

    The app is the local, single-user front end, and each browser session registers its
    own connection in the connection registry (database/registry.py). The registry is
    locked down by default because it also backs the multi-tenant API, so three settings
    are relaxed here to keep the bundled demo working out of the box:

    * T2S_ALLOW_FILE_DATABASES: the demo database is a local SQLite file.
    * T2S_ALLOW_PRIVATE_HOSTS: local use usually means a database on localhost.
    * T2S_METADATA_URL: connections of UI sessions are throwaway, so they are kept in an
      in-memory store. Nothing is written to disk and no T2S_SECRET_KEY is needed.

    Set these variables explicitly before exposing the app to other people.
    """
    from dotenv import load_dotenv
    load_dotenv()  # .env first, so the defaults below never override it
    os.environ.setdefault("T2S_ALLOW_FILE_DATABASES", "true")
    os.environ.setdefault("T2S_ALLOW_PRIVATE_HOSTS", "true")
    os.environ.setdefault("T2S_METADATA_URL", "sqlite://")


_configure_local_defaults()

import streamlit as st  # noqa: E402
import yaml  # noqa: E402

# --- Page config (must be first Streamlit call) ---
st.set_page_config(
    page_title="Text-to-SQL Assistant",
    page_icon="database",
    layout="wide",
    initial_sidebar_state="expanded",
)


# --- Graph caching ---
@st.cache_resource
def get_graph():
    """Cache the compiled LangGraph object. Never rebuild on re-runs."""
    from graph.builder import build_graph
    return build_graph()


# --- Session state management ---
def init_session():
    """Initialize st.session_state keys on first run only."""
    from dotenv import load_dotenv
    load_dotenv()

    # Read config defaults
    try:
        cfg = yaml.safe_load(open("config/config.yaml"))
        hitl_default = cfg.get("hitl", {}).get("enabled", True)
    except Exception:
        hitl_default = True

    defaults = {
        "messages": [],
        "thread_id": str(uuid.uuid4()),
        # Each browser session is its own tenant in the connection registry, so two
        # sessions can never see or replace each other's database.
        "tenant_id": "ui-" + uuid.uuid4().hex,
        "connection_id": None,
        "connection_db_type": None,
        "db_manager": None,  # set only on the legacy path (registry unavailable)
        "session_tokens": 0,
        "session_cost": 0.0,
        "hitl_enabled": hitl_default,
        "hitl_pending": None,
        "hitl_config": None,
        "hitl_decision": None,
        "hitl_initial_sql": None,
        "last_state": None,  # last AgentState for Edit & Rerun
        "groq_api_key": os.getenv("GROQ_API_KEY", ""),
        "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
        "pinecone_api_key": os.getenv("PINECONE_API_KEY", ""),
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def reset_session():
    """Clear all session state and reinitialize with a fresh thread_id."""
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    init_session()


# --- Main app ---
def main():
    init_session()

    from components.sidebar import render_sidebar
    render_sidebar()

    # Chat area placeholder — filled by chat.py in Plan 03
    st.title("Text-to-SQL Assistant")
    if not st.session_state.messages and not st.session_state.get("hitl_pending"):
        # Empty state: welcome message + sample questions
        st.markdown("### Ask a question about your database")
        st.markdown("Try one of these examples:")
        col1, col2 = st.columns(2)
        samples = [
            "Show total sales by artist",
            "Which albums have more than 10 tracks?",
            "Top 5 customers by invoice total",
            "List all tracks in the Rock genre",
        ]
        for i, q in enumerate(samples):
            (col1 if i % 2 == 0 else col2).button(
                q,
                key=f"sample_{i}",
                on_click=lambda s=q: st.session_state.update({"_pending_query": s}),
            )
    else:
        # Render chat — messages, HITL card (no chat_input here)
        from components.chat import render_chat
        render_chat()

    # Chat input always visible — handles both empty state and active chat
    pending = st.session_state.pop("_pending_query", None)
    if prompt := (st.chat_input("Ask a question about your data...") or pending):
        from components.chat import submit_query
        submit_query(prompt)


main()
