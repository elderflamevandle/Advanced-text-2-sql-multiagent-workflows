# Phase 8: Streamlit Frontend - Research

**Researched:** 2026-03-17
**Domain:** Streamlit 1.55, LangGraph 0.3.34 HITL resumption, Plotly Express, async bridge
**Confidence:** HIGH

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Chat Interaction**
- Streaming: Token-by-token streaming using `astream()` from `FallbackClient` (already implemented in Phase 7). Use `st.write_stream()` for rendering.
- Loading indicator: Spinner with stage label while the graph runs before streaming starts — e.g., "Linking schema...", "Generating SQL...", "Executing..." — updated as graph nodes fire.
- Empty state: Welcome message + 3-4 clickable sample questions that pre-fill the input. Reduces cold-start friction.
- Session persistence: In-memory only (`st.session_state`). Page refresh clears the session. (Phase 9 handles persistent history.)
- Clear session: "New Session" / "Clear Chat" button in the sidebar. Resets `st.session_state` and generates a new `thread_id`.

**HITL Approval Flow**
- Placement: Inline approval card rendered as an assistant message in the chat thread when `interrupt()` pauses the graph.
- Card contents: SQL displayed with syntax highlighting, plus three action buttons: Approve / Edit / Reject.
- SQL editing: Editable `st.text_area` pre-filled with `generated_sql`. User edits in-place and clicks "Save & Run" to resume the graph with the corrected SQL.

**Debugging Panel**
- Placement: `st.expander("▶ Debug Details")` inline below each assistant response.
- Default state: Collapsed by default. Auto-expands when the query hit an error or used a retry (`retry_count > 0`).
- Sections displayed (all 4):
  1. Generated SQL + Explanation — `generated_sql` with syntax highlighting + `sql_explanation` text
  2. Query Plan (CoT) — `query_plan` field showing the Chain-of-Thought breakdown
  3. Retry Logs & Error Details — `retry_count`, `sql_history` (prior attempts), `error_log`, `correction_plan` diagnosis
  4. LLM Usage — `usage_metadata` list: provider/model/tokens/cost per node call
- Edit & Rerun SQL: `st.text_area` pre-filled with `generated_sql`. "Rerun" bypasses gatekeeper/planner/generator and calls `executor_node` directly with the edited SQL. Fast shortcut, no full re-planning.

**Configuration Sidebar**
- Four section groups (all included):
  1. Database connection — DB type dropdown (DuckDB/MySQL/PostgreSQL/SQLite), credentials (host/port/database/user/password for remote; file path for DuckDB), Connect button
  2. API keys — `st.text_input(type="password")` for Groq, OpenAI, Pinecone keys
  3. LLM model selector — Primary provider dropdown (Groq/OpenAI), model name dropdown reflecting `config.yaml` `llm` block
  4. HITL toggle + session controls — Human-in-the-loop on/off toggle, "New Session" / "Clear Chat" button, compact token/cost ticker
- Persistence: Session-only (`st.session_state`). Pre-fill API keys from environment variables on startup. Nothing written to disk from the UI.
- Connect behavior: Connect button tests the connection via `DatabaseManager` schema introspection. Shows "Connected (N tables)" or "Error: [message]" inline below the button.
- Session cost ticker: Compact summary below the HITL toggle — "Session: 1,240 tokens | $0.0012" — updated after each query using `usage_metadata` from AgentState.

**Results & Visualizations**
- Table placement: Raw results table (`st.dataframe`) appears inside the chat response — same assistant message that contains the natural language `final_answer`.
- Download CSV: `st.download_button` placed directly below the results table in the chat message.
- Auto-chart detection: `formatter_node` already flags time-series/categorical data. Auto-render the appropriate chart on display.
- Chart library: Plotly (`plotly.express`) — interactive hover, zoom, and pan.
- Chart type toggle: Three-option radio/button group below the chart: Line | Bar | Table. Lets users switch chart types after auto-selection.
- Ragas confidence score: Compact badge/progress bar ("Confidence: 0.87") visible below the assistant message in the chat. Full breakdown (faithfulness, answer_relevance, context_precision) in the debugging panel's LLM Usage section.

**Page Layout & Branding**
- Layout: Wide (`st.set_page_config(layout="wide")`). Gives room for sidebar + full-width chat with embedded tables and charts.
- Title & icon: Claude's discretion — pick something appropriate for the project branding.

### Claude's Discretion
- App title and page icon (`page_icon=` / `page_title=`)
- Sidebar section ordering and visual grouping (headers, dividers)
- Exact spinner label text for each graph node stage
- Color theme (Streamlit default or light customization via `config.toml`)
- DuckDB sidebar: whether to show a file path input or file uploader component
- Chart x/y axis auto-selection logic (which column becomes the x-axis for time-series)
- Chart height / container sizing
- Error message format for failed queries (all retries exhausted) in the chat
- "Conversational" response formatting (non-SQL answers from the formatter node)

### Deferred Ideas (OUT OF SCOPE)
- Query history browser (search, filter, re-run previous queries) — Phase 9
- Analytics dashboard (query volume trends, error rates, popular queries) — Phase 11
- Save/load configuration profiles — future enhancement
- DuckDB file upload (drag-and-drop .db file) vs path input — Claude's discretion for V1
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| UI-001 | Configuration Sidebar — DB type dropdown, credentials, API keys, LLM selector, token/cost tracker | Sidebar pattern using `st.sidebar`, `st.selectbox`, `st.text_input(type="password")`, `st.toggle`. DatabaseManager.test_connection() + get_schema() for Connect button. Config.yaml pre-fill established. |
| UI-002 | Chat Interface — text input, message history, session persistence, clear button, loading indicator | `st.chat_input`, `st.chat_message`, `st.session_state` for in-memory history, `st.status` for multi-stage progress labels. FallbackClient.astream() + st.write_stream() for streaming. |
| UI-003 | Debugging Panel — expandable per-response, query plan, generated SQL, retry logs, Edit & Rerun SQL | `st.expander` with `expanded` param driven by `retry_count > 0`. `st.code(language="sql")`. Direct executor_node call for Edit & Rerun. usage_metadata list from AgentState already structured for display. |
| UI-004 | Visualization Dashboard — auto line/bar chart for time-series/categorical, chart type toggle, up to 1000 rows | `plotly.express` (not installed — must add to pyproject.toml). `st.plotly_chart`. `st.radio` for type toggle. formatter_node already flags data type. pandas DataFrame for intermediate transform. |
</phase_requirements>

---

## Summary

Phase 8 builds the Streamlit frontend that surfaces the complete multi-agent pipeline (Phases 1–7) as an interactive web UI. The backend graph, HITL mechanism, LLM streaming, and usage tracking are all fully implemented; this phase is purely a presentation and integration layer. The risk profile centers on three technical challenges: (1) bridging Streamlit's synchronous execution model to the async LangGraph graph, (2) correctly catching and resuming `GraphInterrupt` exceptions for the HITL flow without triggering Streamlit re-run loops, and (3) Plotly not being installed — it must be added.

Streamlit 1.55 provides all required APIs: `st.write_stream` for generator-based streaming, `st.chat_message`/`st.chat_input` for conversational UI, `st.status` for multi-stage progress labels, and `st.expander` with runtime `expanded` control. The `st.cache_resource` decorator is the correct mechanism for caching the compiled LangGraph object across Streamlit re-runs without rebuilding it every request.

**Primary recommendation:** Use `asyncio.run()` as the async bridge inside Streamlit's sync context. Cache the compiled graph with `@st.cache_resource`. Catch `GraphInterrupt` from `langgraph.errors` to detect HITL pauses. Render the HITL card as a special `st.chat_message("assistant")` block with inline `st.text_area` + buttons that mutate `st.session_state` and trigger `st.rerun()`.

---

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| streamlit | 1.55.0 (installed) | Web UI framework | Already in pyproject.toml; all required APIs confirmed present |
| plotly | latest (not installed) | Interactive charts | Decided in CONTEXT.md; `st.plotly_chart` present in st 1.55 |
| pandas | 2.3.3 (installed) | DataFrame transform for charts and tables | Already installed as transitive dep; st.dataframe accepts DataFrame directly |
| langgraph | 0.3.34 (installed) | Graph execution + HITL interrupt/resume | `Command`, `interrupt`, `GraphInterrupt` all confirmed available |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| asyncio (stdlib) | Python 3.14 builtin | Bridge from sync Streamlit to async graph | Every graph.ainvoke/astream call |
| uuid (stdlib) | Python 3.14 builtin | Generate unique thread_id per session | On session init and "New Session" reset |
| yaml (pyyaml 6.x, installed) | 6.x | Read config.yaml for sidebar defaults | On startup to pre-fill provider/model dropdowns |
| python-dotenv (installed) | 1.x | Read .env for API key pre-fill | On startup, before sidebar renders |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| plotly.express | st native charts (st.line_chart) | Native charts lack hover/zoom/pan; Plotly gives richer interaction; CONTEXT.md locks Plotly |
| asyncio.run() | nest_asyncio + existing loop | nest_asyncio patches running loops; Python 3.14 has no running loop in Streamlit main thread — asyncio.run() works cleanly |
| st.status | st.spinner | st.spinner has a single static message; st.status.update() lets the label change per graph node stage |

**Installation (plotly not yet in project):**
```bash
pip install plotly
# Add to pyproject.toml dependencies:
# "plotly>=5.0.0,<7.0.0",
```

---

## Architecture Patterns

### Recommended Project Structure
```
streamlit_app/
├── app.py                  # Entry point: st.set_page_config, session init, main chat loop
├── __init__.py             # Already exists (empty)
├── components/
│   ├── __init__.py
│   ├── sidebar.py          # Configuration sidebar rendering
│   ├── chat.py             # Chat history rendering + message submission
│   ├── debug_panel.py      # Expander with SQL/plan/retry/usage display
│   └── charts.py           # Plotly chart generation and type toggle
└── styles/
    └── custom.css          # Optional st.markdown(unsafe_allow_html=True) overrides
.streamlit/
    └── config.toml         # Page theme (optional)
```

### Pattern 1: Graph Caching with @st.cache_resource
**What:** Cache the compiled LangGraph object across Streamlit re-runs to avoid rebuilding on every interaction.
**When to use:** On app.py startup, called once.
**Example:**
```python
# Source: Streamlit docs — st.cache_resource for non-serializable objects
import streamlit as st
from graph.builder import build_graph

@st.cache_resource
def get_graph():
    return build_graph()
```

### Pattern 2: Async Bridge via asyncio.run()
**What:** Streamlit's main execution is synchronous. All LangGraph nodes are `async def`. `asyncio.run()` creates a new event loop for each call.
**When to use:** Every call to `graph.ainvoke()` or `graph.astream()`.
**Verified:** Python 3.14 has no running event loop in Streamlit's main thread — `asyncio.run()` succeeds without `nest_asyncio`.
**Example:**
```python
import asyncio

def invoke_graph(graph, initial_state, config):
    """Synchronous wrapper for async graph execution."""
    return asyncio.run(graph.ainvoke(initial_state, config=config))
```

### Pattern 3: Graph Streaming with astream() + st.status Stage Labels
**What:** Use `graph.astream(..., stream_mode="updates")` to receive per-node state updates. Update `st.status` label as each node fires.
**When to use:** All query submissions.
**Example:**
```python
import asyncio

NODE_LABELS = {
    "gatekeeper": "Validating query...",
    "schema_linker": "Linking schema...",
    "query_planner": "Planning query...",
    "sql_generator": "Generating SQL...",
    "hitl": "Awaiting approval...",
    "executor": "Executing SQL...",
    "correction_plan_node": "Diagnosing error...",
    "correction_sql_node": "Correcting SQL...",
    "formatter": "Formatting answer...",
    "evaluator": "Evaluating quality...",
}

async def stream_graph(graph, state, config, status_container):
    final_state = {}
    async for chunk in graph.astream(state, config=config, stream_mode="updates"):
        for node_name, node_output in chunk.items():
            label = NODE_LABELS.get(node_name, f"Running {node_name}...")
            status_container.update(label=label, state="running")
            final_state.update(node_output)
    return final_state
```

### Pattern 4: HITL GraphInterrupt Detection and Resumption
**What:** The graph raises `GraphInterrupt` (from `langgraph.errors`) when `hitl_node` calls `interrupt()`. The UI must catch this, render the approval card, and resume with `Command(resume=decision)`.
**Key insight:** `GraphInterrupt` is in `langgraph.errors`, NOT `langgraph.types`. `GraphInterrupt` is NOT the same as `interrupt` (the function).
**When to use:** Whenever `graph.ainvoke()` or `graph.astream()` is called with HITL enabled.
**Example:**
```python
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

async def run_with_hitl(graph, state, config):
    try:
        result = await graph.ainvoke(state, config=config)
        return {"type": "complete", "state": result}
    except GraphInterrupt as exc:
        # exc.args[0] is a tuple of Interrupt objects
        interrupt_data = exc.args[0][0].value if exc.args[0] else {}
        return {"type": "hitl_pending", "data": interrupt_data}

def resume_graph(graph, decision, config):
    """Resume after HITL. decision = {"action": "approved"|"rejected"|"edited", "sql": str}"""
    return asyncio.run(
        graph.ainvoke(Command(resume=decision), config=config)
    )
```

### Pattern 5: Session State Initialization
**What:** Initialize `st.session_state` keys on first run only. Never overwrite existing keys.
**Example:**
```python
import uuid

def init_session():
    defaults = {
        "messages": [],           # list[dict] — {role, content, state, timestamp}
        "thread_id": str(uuid.uuid4()),
        "db_manager": None,
        "session_tokens": 0,
        "session_cost": 0.0,
        "hitl_pending": None,     # interrupt data while awaiting user approval
        "hitl_config": None,      # graph config to use for resumption
        "hitl_initial_sql": None, # original SQL for edit pre-fill
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val

def reset_session():
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    init_session()
```

### Pattern 6: HITL Inline Approval Card
**What:** Render HITL decision as a persistent chat message. Store `hitl_pending` in session_state so the card survives Streamlit re-runs. On button click: update session_state, call `st.rerun()`.
**Example:**
```python
def render_hitl_card(sql: str, explanation: str):
    with st.chat_message("assistant"):
        st.warning("SQL Review Required")
        st.code(sql, language="sql")
        if explanation:
            st.caption(explanation)
        col1, col2, col3 = st.columns(3)
        if col1.button("Approve", key="hitl_approve"):
            st.session_state.hitl_decision = {"action": "approved", "sql": sql}
            st.rerun()
        edited_sql = st.text_area("Edit SQL:", value=sql, key="hitl_edit_area")
        if col2.button("Save & Run", key="hitl_save"):
            st.session_state.hitl_decision = {"action": "edited", "sql": edited_sql}
            st.rerun()
        if col3.button("Reject", key="hitl_reject"):
            st.session_state.hitl_decision = {"action": "rejected", "sql": sql}
            st.rerun()
```

### Pattern 7: Edit & Rerun SQL (Debug Panel)
**What:** "Rerun" in debug panel bypasses the full pipeline and calls `executor_node` directly with edited SQL injected into state.
**Example:**
```python
import asyncio
from agents.nodes.executor import executor_node

def rerun_sql(edited_sql: str, last_state: dict) -> dict:
    """Bypass planning/generation — execute edited SQL directly."""
    modified_state = {**last_state, "generated_sql": edited_sql}
    return asyncio.run(executor_node(modified_state))
```

### Pattern 8: Plotly Chart with Type Toggle
**What:** Detect data type from `AgentState` flags (set by formatter_node). Render chart. Provide radio toggle for switching types.
**Example:**
```python
import plotly.express as px
import pandas as pd

def render_chart(results: list[dict], chart_type: str, x_col: str, y_col: str):
    df = pd.DataFrame(results)
    if chart_type == "Line":
        fig = px.line(df, x=x_col, y=y_col)
    elif chart_type == "Bar":
        fig = px.bar(df, x=x_col, y=y_col)
    else:
        return st.dataframe(df)
    st.plotly_chart(fig, use_container_width=True)
```

### Anti-Patterns to Avoid
- **Building the graph inside the chat loop:** The graph includes `MemorySaver` which holds in-memory state. Rebuilding it on every re-run destroys session memory. Always use `@st.cache_resource`.
- **Calling `asyncio.get_event_loop().run_until_complete()`:** Deprecated in Python 3.10+ and raises `DeprecationWarning` in Python 3.12+, fails in Python 3.14. Use `asyncio.run()` instead.
- **Using `st.button()` in a loop that re-renders every run:** Button state in Streamlit is `True` for exactly one re-run. Store button outcomes in `st.session_state` before `st.rerun()` if they need to persist across re-runs (critical for HITL).
- **Storing non-serializable objects in `st.session_state`:** `DatabaseManager` and the compiled graph must be stored carefully. `DatabaseManager` can go in `session_state` (Python object, not serialized). The compiled graph must be in `@st.cache_resource` (shared across sessions, never in session_state).
- **Calling `st.write_stream()` with a sync generator from an async function:** `st.write_stream()` accepts async generators directly. The `FallbackClient.astream()` is an async generator — pass it directly, don't wrap in `asyncio.run()`.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| SQL syntax highlighting | Custom regex color markup | `st.code(sql, language="sql")` | Streamlit uses Prism.js internally; handles all SQL dialects |
| Async → sync bridge | Thread pools, event loop hacks | `asyncio.run()` | Clean, supported, verified on Python 3.14 + Streamlit 1.55 |
| Multi-stage progress | Custom spinner replacement | `st.status` + `.update(label=..., state=...)` | Built-in, animates, supports "complete"/"error" terminal states |
| CSV download | Server-side file write | `st.download_button(data=df.to_csv())` | Client-side download, no temp file management |
| Interactive charts | matplotlib/seaborn embedding | `plotly.express` + `st.plotly_chart` | Plotly is locked in CONTEXT.md; native hover/zoom without extra code |
| HITL state persistence | URL query params, cookies | `st.session_state` dict | Streamlit's official pattern; survives widget interactions within same session |

**Key insight:** Streamlit's reactive execution model (the entire script reruns on every interaction) means state management is the dominant complexity. Lean on `st.session_state` for everything mutable and `@st.cache_resource` for expensive singleton objects.

---

## Common Pitfalls

### Pitfall 1: GraphInterrupt Not Caught — App Crashes on HITL
**What goes wrong:** `graph.ainvoke()` raises `GraphInterrupt` when `hitl_node` fires `interrupt()`. If uncaught, Streamlit shows an unhandled exception error page.
**Why it happens:** `GraphInterrupt` is in `langgraph.errors`, not `langgraph.types`. Developers import `interrupt` (the function) from `langgraph.types` and assume that's all they need to know about interrupts.
**How to avoid:** Always wrap graph invocation in `try/except GraphInterrupt` when HITL is enabled. Import: `from langgraph.errors import GraphInterrupt`.
**Warning signs:** "GraphInterrupt" in Streamlit error traceback.

### Pitfall 2: Streamlit Re-Run Loops on HITL Button Clicks
**What goes wrong:** Clicking "Approve" in the HITL card triggers a re-run. The HITL card re-renders before the graph resumes, showing the old approval card again. If the graph re-invokes, it may raise another `GraphInterrupt`.
**Why it happens:** Streamlit reruns the entire script on every widget interaction. Button state (`True`) is only held for one re-run.
**How to avoid:** Use a multi-stage session_state flag:
  1. On `GraphInterrupt`: set `st.session_state.hitl_pending = interrupt_data`, store `thread_id` config as `st.session_state.hitl_config`.
  2. On button click: set `st.session_state.hitl_decision = {action, sql}`, call `st.rerun()`.
  3. On next re-run: detect `hitl_decision` in session_state, call `resume_graph()`, clear both `hitl_pending` and `hitl_decision`.
**Warning signs:** HITL card appears twice, or approval button has no effect.

### Pitfall 3: Compiled Graph Rebuilt on Every Re-Run
**What goes wrong:** `build_graph()` is called inside the main script body (not in a cached function). Every Streamlit re-run builds a new `MemorySaver`-backed graph, destroying all prior conversation thread memory.
**Why it happens:** Forgetting that Streamlit reruns the entire script on every interaction.
**How to avoid:** Always wrap `build_graph()` in `@st.cache_resource`. This creates a single shared graph instance across all re-runs.
**Warning signs:** Conversation context disappears mid-session; prior messages are not referenced in follow-up queries.

### Pitfall 4: asyncio.run() Called Inside an Already-Running Event Loop
**What goes wrong:** `RuntimeError: This event loop is already running` if called from an async context (e.g., if another library started a loop).
**Why it happens:** `asyncio.run()` refuses to create a new loop when one is already running.
**How to avoid:** In Streamlit's synchronous script context, there is no running loop — `asyncio.run()` works. Avoid integrating with libraries that call `asyncio.get_event_loop().run_forever()` before the Streamlit call.
**Warning signs:** `RuntimeError: This event loop is already running`.

### Pitfall 5: st.write_stream() with Non-Async Generator from FallbackClient.astream()
**What goes wrong:** `FallbackClient.astream()` is an `async def` generator. `st.write_stream()` accepts sync generators and async generators. The key is passing the coroutine/generator object correctly.
**Why it happens:** `st.write_stream(client.astream(messages))` passes the async generator object — this is correct. But `st.write_stream(asyncio.run(client.astream(...)))` would fail because `asyncio.run()` would consume the async generator.
**How to avoid:** Pass the async generator object directly to `st.write_stream()`. Streamlit 1.55 handles async generators natively.
**Warning signs:** `TypeError` about generator type, or streaming produces no output.

### Pitfall 6: AgentState Missing `ragas_score` Field
**What goes wrong:** The debug panel tries to display `state["ragas_score"]` but that field is not in the current `AgentState` TypedDict (it uses `evaluator_node` which stores results differently). The `ragas_score` in REQUIREMENTS.md is a legacy field name.
**Why it happens:** REQUIREMENTS.md was written before Phase 7 added `usage_metadata`. The actual evaluator output structure may differ.
**How to avoid:** Check `AgentState` definition in `graph/state.py` (20 fields confirmed). The evaluator node is a placeholder — its output field structure should be confirmed before the debug panel tries to read it. Use `.get("ragas_score")` with a `None` default.
**Warning signs:** `KeyError: 'ragas_score'` in debug panel.

### Pitfall 7: Plotly Not Installed
**What goes wrong:** `import plotly.express as px` fails at app startup with `ModuleNotFoundError`.
**Why it happens:** Plotly is not in `pyproject.toml` and not installed in the environment (verified).
**How to avoid:** Add `"plotly>=5.0.0,<7.0.0"` to the `dependencies` list in `pyproject.toml`. Install before running.
**Warning signs:** `ModuleNotFoundError: No module named 'plotly'` on app start.

---

## Code Examples

Verified patterns from the installed environment:

### Streamlit Page Config (st 1.55)
```python
# Source: st.set_page_config signature verified in environment
st.set_page_config(
    page_title="Text-to-SQL Assistant",
    page_icon="database",   # emoji or URL
    layout="wide",
    initial_sidebar_state="expanded",
)
```

### Chat Interface Pattern (st 1.55)
```python
# Source: st.chat_message and st.chat_input signatures verified in environment
# Render message history
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Accept new input (returns None when no submission)
if prompt := st.chat_input("Ask a question about your data..."):
    # Process query...
    pass
```

### Status Container for Multi-Stage Progress (st 1.55)
```python
# Source: st.status + StatusContainer.update() signatures verified in environment
with st.status("Processing query...", expanded=True) as status:
    status.update(label="Validating query...", state="running")
    # ... run gatekeeper ...
    status.update(label="Generating SQL...", state="running")
    # ... run sql_generator ...
    status.update(label="Complete", state="complete", expanded=False)
```

### SQL Syntax Highlighting (st 1.55)
```python
# Source: st.code signature verified — language param accepts "sql"
st.code(generated_sql, language="sql", line_numbers=True)
```

### CSV Download (st 1.55)
```python
# Source: st.download_button confirmed available
import pandas as pd
df = pd.DataFrame(db_results)
csv = df.to_csv(index=False)
st.download_button("Download CSV", data=csv, file_name="results.csv", mime="text/csv")
```

### GraphInterrupt Catch (langgraph 0.3.34)
```python
# Source: langgraph.errors.GraphInterrupt confirmed in environment
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

try:
    result = asyncio.run(graph.ainvoke(state, config=config))
except GraphInterrupt as exc:
    interrupt_list = exc.args[0]  # tuple of Interrupt objects
    interrupt_value = interrupt_list[0].value if interrupt_list else {}
    st.session_state.hitl_pending = interrupt_value
```

### LangGraph Command Resume (langgraph 0.3.34)
```python
# Source: langgraph.types.Command confirmed available in environment
from langgraph.types import Command

decision = {"action": "approved", "sql": generated_sql}
result = asyncio.run(
    graph.ainvoke(Command(resume=decision), config={"configurable": {"thread_id": thread_id}})
)
```

### Expander with Conditional Auto-Expand (st 1.55)
```python
# Source: st.expander confirmed in environment
retry_count = state.get("retry_count", 0)
has_error = state.get("error_log") is not None
auto_expand = retry_count > 0 or has_error

with st.expander("Debug Details", expanded=auto_expand):
    # ... display debug content ...
```

### Streamlit AppTest for Unit Testing (st 1.55)
```python
# Source: streamlit.testing.v1.AppTest confirmed available in environment
from streamlit.testing.v1 import AppTest

def test_sidebar_renders():
    at = AppTest.from_file("streamlit_app/app.py")
    at.run()
    assert not at.exception
    # Check sidebar selectbox exists
    assert len(at.sidebar.selectbox) > 0
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `st.spinner()` (static label) | `st.status()` with `.update()` (dynamic labels) | Streamlit ~1.28 | Can show per-node stage labels during graph execution |
| `st.session_state` as simple dict | Same API, still recommended | N/A | No change needed |
| `loop.run_until_complete()` | `asyncio.run()` | Python 3.10+ recommended, required by Python 3.12+ | Must use `asyncio.run()` — no alternative on Python 3.14 |
| `st.experimental_rerun()` | `st.rerun()` | Streamlit ~1.27 | `experimental_rerun` is removed; only `st.rerun()` in 1.55 |
| `st.experimental_data_editor` | `st.data_editor` | Streamlit ~1.23 | Stable API available for inline data editing |

**Deprecated/outdated:**
- `st.experimental_rerun()`: removed — use `st.rerun()`.
- `asyncio.get_event_loop().run_until_complete()`: raises DeprecationWarning on 3.10+, error on 3.14 — use `asyncio.run()`.
- `GraphInterrupt` from `langgraph.types`: does NOT exist there — must import from `langgraph.errors`.

---

## Open Questions

1. **evaluator_node output field for Ragas score**
   - What we know: `AgentState` has 20 fields. `evaluator_node` is a placeholder node (from Phase 2 scaffolding). The Ragas score field name in `graph/state.py` is not confirmed.
   - What's unclear: Does `evaluator_node` currently write to a `ragas_score` field? Does that field exist in `AgentState`? (Current `graph/state.py` does not include it.)
   - Recommendation: Debug panel should guard all evaluator fields with `.get("ragas_score")` returning `None`. The Ragas score display should be conditionally rendered only when the field is non-None.

2. **formatter_node chart type flags**
   - What we know: CONTEXT.md states "formatter_node already flags time-series/categorical data." The chart auto-detection in `charts.py` should read this flag.
   - What's unclear: What exact field/value does `formatter_node` write to signal chart type? This must be confirmed in Phase 8 Plan 01 by reading `agents/nodes/formatter.py`.
   - Recommendation: If the flag field is absent, fall back to column-name heuristics (date/time in column name → time-series; string column + numeric column → categorical).

3. **FallbackClient.astream() — current behavior is ainvoke-wrapped**
   - What we know: Phase 7 STATE.md documents: "astream() wraps ainvoke() for Phase 7 — full token-level streaming deferred to Phase 8."
   - What's unclear: To get token-level streaming for `st.write_stream()`, `FallbackClient.astream()` needs upgrading to use the underlying LLM's `.astream()`. This is a Phase 8 task, not pre-existing.
   - Recommendation: Wave 1 of Phase 8 should upgrade `FallbackClient.astream()` to use real LLM streaming via `llm.astream(messages)` with usage aggregation from the final chunk. Until then, `st.write_stream()` will receive a single chunk (full response) — functional but not streaming word-by-word.

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 |
| Config file | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| Quick run command | `pytest tests/ui/ -x -q` |
| Full suite command | `pytest tests/ -v` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| UI-001 | Sidebar renders DB type dropdown, API key inputs, connect button | unit (AppTest) | `pytest tests/ui/test_sidebar.py -x` | Wave 0 |
| UI-001 | Connect button triggers DatabaseManager.test_connection() | unit (mock) | `pytest tests/ui/test_sidebar.py::test_connect_button -x` | Wave 0 |
| UI-002 | Chat input submission triggers graph invocation | unit (AppTest + mock graph) | `pytest tests/ui/test_chat.py::test_query_submission -x` | Wave 0 |
| UI-002 | Message history persists within session | unit (AppTest) | `pytest tests/ui/test_chat.py::test_message_history -x` | Wave 0 |
| UI-003 | Debug panel is collapsed by default when retry_count=0 | unit (AppTest) | `pytest tests/ui/test_debug_panel.py::test_collapsed_default -x` | Wave 0 |
| UI-003 | Debug panel auto-expands when retry_count > 0 | unit (AppTest) | `pytest tests/ui/test_debug_panel.py::test_auto_expand_on_retry -x` | Wave 0 |
| UI-003 | Edit & Rerun calls executor_node directly, not full graph | unit (mock) | `pytest tests/ui/test_debug_panel.py::test_edit_rerun -x` | Wave 0 |
| UI-004 | Chart renders for time-series data (line chart default) | unit (mock state) | `pytest tests/ui/test_charts.py::test_timeseries_line -x` | Wave 0 |
| UI-004 | Chart type toggle switches between Line/Bar/Table | unit (AppTest) | `pytest tests/ui/test_charts.py::test_chart_toggle -x` | Wave 0 |
| UI-001–004 | Complete query flow through UI end-to-end | integration (smoke) | `pytest tests/ui/test_e2e.py -x` | Wave 0 |

Note: `streamlit.testing.v1.AppTest` is available in Streamlit 1.55 (confirmed). It does not start a server — tests are pure Python, fast enough for the 30s budget.

### Sampling Rate
- **Per task commit:** `pytest tests/ui/ -x -q`
- **Per wave merge:** `pytest tests/ -v`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/ui/__init__.py` — package marker
- [ ] `tests/ui/test_sidebar.py` — covers UI-001
- [ ] `tests/ui/test_chat.py` — covers UI-002
- [ ] `tests/ui/test_debug_panel.py` — covers UI-003
- [ ] `tests/ui/test_charts.py` — covers UI-004
- [ ] `tests/ui/test_e2e.py` — end-to-end smoke test
- [ ] `tests/ui/conftest.py` — shared fixtures (mock graph, mock DatabaseManager, sample AgentState)
- [ ] Plotly install: `pip install plotly` + add to `pyproject.toml`
- [ ] `.streamlit/config.toml` — optional theming file (not required for tests)

---

## Sources

### Primary (HIGH confidence)
- Environment inspection (`python -c "import streamlit; ..."`) — all Streamlit 1.55 API signatures verified directly
- `graph/state.py` — confirmed 20 AgentState fields
- `llm/fallback.py` — astream() behavior confirmed (wraps ainvoke, note for Wave 1 upgrade)
- `llm/usage_tracker.py` — usage_metadata entry structure confirmed
- `agents/nodes/hitl.py` — interrupt() call and resumption logic confirmed
- `graph/builder.py` — node names, graph structure, MemorySaver usage confirmed
- `config/config.yaml` — config keys for sidebar pre-fill confirmed
- `database/manager.py` — test_connection(), get_schema() API confirmed
- Environment check: `GraphInterrupt` in `langgraph.errors` (NOT `langgraph.types`) — verified directly
- Environment check: `Command`, `interrupt` in `langgraph.types` — verified directly
- Environment check: plotly NOT installed — verified directly
- Environment check: pandas 2.3.3 installed — verified directly
- Environment check: `asyncio.run()` works (no running loop in Streamlit context) — verified directly
- Environment check: `streamlit.testing.v1.AppTest` available — verified directly

### Secondary (MEDIUM confidence)
- pyproject.toml — current deps and test configuration
- STATE.md Phase 7 decisions — astream() wrapping strategy, thread_id pattern

### Tertiary (LOW confidence)
- None — all key claims verified from environment or source code.

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all libraries version-verified in environment; Plotly absence confirmed
- Architecture: HIGH — all integration points verified from existing source code
- Pitfalls: HIGH — GraphInterrupt import path verified, asyncio.run() behavior tested, astream() wrapping behavior from STATE.md
- Validation: HIGH — AppTest confirmed available in environment

**Research date:** 2026-03-17
**Valid until:** 2026-04-17 (Streamlit stable release; LangGraph APIs stable in 0.3.x line)
