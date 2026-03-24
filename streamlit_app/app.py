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

import streamlit as st
import yaml

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
        "db_manager": None,
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
