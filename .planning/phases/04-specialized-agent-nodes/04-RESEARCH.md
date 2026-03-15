# Phase 4: Specialized Agent Nodes - Research

**Researched:** 2026-03-15
**Domain:** LLM-powered agent nodes (ChatGroq, LangChain, async LangGraph nodes, text-to-SQL prompting)
**Confidence:** HIGH

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**Gatekeeper Behavior**
- Classification approach: LLM-based classification via ChatGroq. Send query + chat history to LLM with a classification prompt.
- 4 classification categories: `sql` (data query), `conversational` (greeting/thanks/off-topic), `follow_up` (references prior query context), `ambiguous` (unclear intent — ask for clarification).
- Ambiguous handling: Set `query_type='ambiguous'`, return a clarification message in `final_answer`. Route to formatter.
- Follow-up detection & rewrite: Gatekeeper detects follow-up queries referencing prior context. Rewrites to standalone query using chat history. Rewritten query stored in a new `resolved_query` field in state (original `user_query` preserved). Downstream nodes check `resolved_query` first, fall back to `user_query`.
- DB connection check: If `state['db_manager']` is None, return early with "Please connect to a database first."
- Intent extraction: LLM returns classification category + a one-line intent summary.
- Conversational responses: LLM-generated friendly conversational replies. Sets `final_answer`, routes to formatter.
- NL safety check: Basic detection of destructive intent in natural language. Block and explain.
- Routing update: `sql` and `follow_up` (rewritten) → schema_linker; `conversational` and `ambiguous` → formatter.

**Query Planning Depth**
- Plan format: Structured JSON plan: `{"select": [...], "from": [...], "joins": [...], "where": [...], "group_by": [...], "order_by": [...], "limit": N, "ctes": [...]}`.
- Schema input: Full schema context — relevant_tables + their column definitions, types, FK info, and JOIN hints.
- Complexity classification: `simple`, `moderate`, or `complex`. Stored in state.
- CTE/subquery decomposition: For complex queries, plan breaks into explicit sub-steps.

**SQL Generation Style**
- Inline comments: Brief comments for non-obvious logic.
- Dialect handling: Pass `db_type` in the LLM prompt context. Include dialect-specific reminders.
- Validation: Basic structure check — verify output starts with SELECT/WITH, isn't empty, strip markdown fences.
- SQL explanation: LLM returns SQL + a 1-2 sentence human-readable explanation. Displayed in debugging panel.

**LLM Usage**
- Access pattern: Use LangChain `ChatGroq` directly in each node. Import from `langchain_groq`.
- Default provider: Groq with Llama-3 70B. Requires `GROQ_API_KEY` env var.
- Prompt storage: Python string constants in each node file (module-level).
- Schema linker fallback: If retriever fails or isn't configured, fall back to `state['schema']`. Log warning.

### Claude's Discretion
- Exact prompt wording for each node (gatekeeper classification, planner CoT, generator dialect instructions)
- ChatGroq model parameter (temperature, max_tokens)
- How to parse LLM JSON output (structured output vs regex extraction)
- `resolved_query` field type in AgentState (Optional[str])
- Error handling when LLM returns malformed JSON plan
- How schema linker populates `relevant_tables` and `schema` from retriever output

### Deferred Ideas (OUT OF SCOPE)

None — discussion stayed within phase scope.
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| AGENT-001 | Gatekeeper Node: validates queries, classifies as sql/conversational/follow_up/ambiguous, rewrites follow-ups | ChatGroq ainvoke pattern; JSON output parsing; conditions.py routing expansion |
| AGENT-002 | Schema Linker Node: integrates Pinecone/ChromaDB retriever into graph, populates relevant_tables | BaseRetriever.retrieve_tables() contract; fallback to state['schema']; state field mapping |
| AGENT-003 | Query Planner Node: generates Chain-of-Thought JSON execution plan | ChatGroq structured JSON output; CoT prompting patterns; JSON parsing + error recovery |
| AGENT-004 | SQL Generator Node: translates plan to dialect-specific SQL with comments and explanation | ChatGroq ainvoke; dialect-specific prompt construction; markdown fence stripping; SELECT/WITH validation |
| LLM-003 | Specialized Prompts: optimized prompts for each agent node stored in node files | Module-level prompt constants pattern; few-shot examples; prompt engineering for text-to-SQL |
</phase_requirements>

---

## Summary

Phase 4 replaces four async placeholder stubs with real LLM-powered logic using LangChain's `ChatGroq`. The primary technical concern is wiring ChatGroq's `ainvoke` into async LangGraph nodes that return partial state dicts. The model to use is `llama-3.3-70b-versatile` (the current Groq 70B, replacing deprecated `llama3-70b-8192`). It supports JSON mode, tool use, and 128K context.

For JSON output parsing, the established project pattern points to manual `json.loads()` on `AIMessage.content` rather than `with_structured_output`, which requires Pydantic and adds complexity. A thin helper that strips markdown fences, calls `json.loads`, and raises a descriptive exception on parse failure is the right approach. The gatekeeper and SQL generator can use plain string output (`AIMessage.content`) since they produce structured but short responses; the planner benefits from explicit JSON instructions in the prompt.

The schema linker node is the most mechanical: call `get_retriever()` inside a try/except, call `retrieve_tables()`, map the result dict onto state fields (`relevant_tables`, `schema`), and fall back to `state['schema']` on failure. No LLM call needed. The `conditions.py` routing function needs expansion from 2 to 4 categories, and `state.py` needs `resolved_query: Optional[str]` added (which will break the existing `test_agentstate_has_required_fields` test that asserts exactly 13 fields — this must be updated to 14).

**Primary recommendation:** Use `ChatGroq(model="llama-3.3-70b-versatile", temperature=0, max_tokens=1024)` for classification/generation nodes; parse JSON manually with fence-stripping; expand routing to handle `follow_up` and `ambiguous` categories alongside existing `sql` and `conversational`.

---

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| langchain-groq | >=0.1.0 (in core deps) | ChatGroq chat model | Project-mandated Groq primary provider |
| langchain-core | transitive dep | HumanMessage, SystemMessage, AIMessage, ChatPromptTemplate | Official LangChain message types |
| groq | >=0.4.0,<2.0.0 (in pyproject.toml) | Groq SDK (auto-used by langchain-groq) | Already declared in project deps |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| json (stdlib) | built-in | Parse LLM JSON output | Planner + gatekeeper classification response |
| re (stdlib) | built-in | Strip markdown code fences from LLM output | SQL generator output cleanup |
| unittest.mock.AsyncMock | built-in (Python 3.8+) | Mock ChatGroq ainvoke in unit tests | All agent node tests |
| langchain-core FakeListChatModel | langchain-core | Deterministic fake chat model for integration tests | Full-graph tests avoiding real API calls |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| json.loads manual parsing | with_structured_output(Pydantic) | with_structured_output requires json_schema method not supported on all models; manual parsing is simpler and matches existing project patterns |
| json.loads manual parsing | JsonOutputParser | Valid alternative — adds format_instructions to prompt; slight over-engineering for 4 node files |
| module-level prompt constants | config/prompts.yaml | CONTEXT.md explicitly defers this to Phase 7; keep prompts colocated for now |

**Installation:**
```bash
# All dependencies already declared in pyproject.toml
# langchain, langchain-groq, groq are in core dependencies
pip install -e ".[dev]"
```

---

## Architecture Patterns

### Recommended Project Structure
```
agents/
└── nodes/
    ├── gatekeeper.py       # AGENT-001: classification, follow-up rewrite, NL safety
    ├── schema_linker.py    # AGENT-002: retriever integration, state population
    ├── query_planner.py    # AGENT-003: CoT JSON plan generation
    ├── sql_generator.py    # AGENT-004: dialect SQL + explanation
    └── __init__.py         # already complete, re-exports all 9 nodes
graph/
    ├── state.py            # add resolved_query: Optional[str] field
    └── conditions.py       # expand route_after_gatekeeper to 4 categories
tests/
└── agents/
    ├── __init__.py
    ├── conftest.py         # shared fixtures: mock_llm, sample AgentState
    ├── test_gatekeeper.py
    ├── test_schema_linker.py
    ├── test_query_planner.py
    └── test_sql_generator.py
```

### Pattern 1: Standard Async Node with ChatGroq
**What:** Every real LLM node follows this pattern — lazy-import ChatGroq, build messages, await ainvoke, parse AIMessage.content, return partial state dict.
**When to use:** All 3 LLM-powered nodes (gatekeeper, query_planner, sql_generator).
**Example:**
```python
# Source: LangChain ChatGroq docs + established project async node pattern
import json
import logging
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """..."""   # module-level constant (LLM-003)

async def query_planner_node(state: AgentState) -> dict:
    """Generates Chain-of-Thought SQL execution plan."""
    from langchain_groq import ChatGroq  # lazy import — optional extra pattern

    query = state.get("resolved_query") or state.get("user_query", "")
    logger.info("query_planner_node: planning for query: %s", query)

    llm = ChatGroq(model="llama-3.3-70b-versatile", temperature=0, max_tokens=1024)
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=query),
    ]
    response = await llm.ainvoke(messages)
    raw_content = response.content

    try:
        plan = _parse_json_response(raw_content)
    except ValueError as exc:
        logger.warning("query_planner_node: JSON parse failure: %s", exc)
        plan = {"select": ["*"], "from": [], "joins": [], "where": [],
                "group_by": [], "order_by": [], "limit": None, "ctes": []}

    return {"query_plan": plan}
```

### Pattern 2: JSON Output Parsing with Fence Stripping
**What:** Strip markdown code fences (```json ... ```) that LLMs frequently add, then json.loads.
**When to use:** Planner (structured plan dict), gatekeeper (classification + intent dict).
**Example:**
```python
import json
import re

def _parse_json_response(raw: str) -> dict:
    """Strip markdown fences and parse JSON from LLM response."""
    # Remove ```json ... ``` or ``` ... ``` fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)
    try:
        return json.loads(cleaned.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM returned non-JSON: {exc}. Raw content: {raw[:200]}") from exc
```

### Pattern 3: Schema Linker Without LLM
**What:** Schema linker makes no LLM call. It calls the Phase 3 retriever, maps results to state fields.
**When to use:** schema_linker_node only.
**Example:**
```python
async def schema_linker_node(state: AgentState) -> dict:
    """Retrieves relevant schema tables via vector search (AGENT-002)."""
    from vector.retriever import get_retriever

    query = state.get("resolved_query") or state.get("user_query", "")
    db_type = state.get("db_type", "unknown")
    full_schema = state.get("schema")

    try:
        retriever = get_retriever()
        namespace = db_type  # e.g. "sqlite", "postgres"
        # embed_schema is idempotent — skips if namespace already exists
        if full_schema:
            retriever.embed_schema(full_schema, namespace)
        result = retriever.retrieve_tables(query, namespace, top_k=5)
        relevant_tables = result["tables"]
        # Narrow schema to only relevant tables for planner context
        narrowed_schema = {t: full_schema[t] for t in relevant_tables if full_schema and t in full_schema}
        return {
            "relevant_tables": relevant_tables,
            "schema": narrowed_schema if narrowed_schema else full_schema,
        }
    except Exception as exc:
        logger.warning("schema_linker_node: retriever failed (%s), using full schema fallback", exc)
        return {
            "relevant_tables": list(full_schema.keys()) if full_schema else [],
            "schema": full_schema,
        }
```

### Pattern 4: Routing Expansion in conditions.py
**What:** `route_after_gatekeeper` must handle 4 categories: sql, follow_up, conversational, ambiguous.
**When to use:** conditions.py update — required before gatekeeper returns new categories.
**Example:**
```python
def route_after_gatekeeper(state: AgentState) -> str:
    """Route based on 4 gatekeeper classification categories."""
    query_type = state.get("query_type")
    if query_type in ("conversational", "ambiguous"):
        return "formatter"
    # sql, follow_up (rewritten), and None all proceed to schema_linker
    return "schema_linker"
```
Note: `builder.py` path map `{"schema_linker": "schema_linker", "formatter": "formatter"}` already covers both destinations — no change needed to builder.py.

### Pattern 5: Unit Testing Async Nodes with AsyncMock
**What:** Patch ChatGroq at the point of import in the node module, replace ainvoke with AsyncMock.
**When to use:** All LLM node unit tests.
**Example:**
```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import AIMessage

def test_query_planner_returns_plan(make_state):
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content='{"select": ["total"], "from": ["sales"]}'))

    with patch("agents.nodes.query_planner.ChatGroq", return_value=mock_llm):
        result = asyncio.run(query_planner_node(make_state("show total sales")))

    assert "query_plan" in result
    assert result["query_plan"]["from"] == ["sales"]
```

### Anti-Patterns to Avoid
- **Returning None from a node:** LangGraph raises TypeError on state merge. Always return `{}` minimum.
- **Top-level `from langchain_groq import ChatGroq`:** Follows project's lazy-import pattern for optional extras. Keep inside the function body.
- **Blocking `llm.invoke()` inside async node:** Use `await llm.ainvoke()` — synchronous invoke blocks the event loop in an async LangGraph context.
- **Raising exceptions from nodes:** LangGraph does not catch node exceptions gracefully in all versions. Catch and log internally, return error info in state fields.
- **Overwriting `user_query` with resolved query:** CONTEXT.md locks this — always write to `resolved_query`, preserve `user_query`.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| LLM chat invocation | Custom HTTP client for Groq API | `ChatGroq.ainvoke()` from langchain-groq | Handles retries, rate limiting, auth, token counting |
| JSON output parsing | Complex regex tree | `json.loads()` + fence-stripping helper | All LLM JSON is valid JSON — no custom parser needed |
| Async fake LLM for tests | Custom mock class | `AsyncMock` on `llm.ainvoke` or `FakeListChatModel` | Built into Python's unittest.mock (3.8+) |
| Schema retrieval logic | Re-implementing vector search | `get_retriever()` + `retrieve_tables()` from vector/ | Already implemented and tested in Phase 3 |
| FK expansion in schema linker | Manual FK traversal | `SchemaGraph` (already used inside retriever) | Already wired inside `BaseRetriever.retrieve_tables()` |

**Key insight:** The Phase 3 retriever already handles the hardest part of schema linking (two-stage retrieval, FK expansion, join hints). The schema_linker node is a thin adapter that calls it and maps results to state fields.

---

## Common Pitfalls

### Pitfall 1: Groq Model Name Is Outdated
**What goes wrong:** Using `llama3-70b-8192` (old name) throws an API error — Groq deprecated all 3.1 model IDs in January 2025. Requests return errors.
**Why it happens:** Training data and older tutorials reference the deprecated name.
**How to avoid:** Use `llama-3.3-70b-versatile` (128K context, JSON mode, tool use). Confirmed on Groq docs as of 2025.
**Warning signs:** `400 Bad Request` or "model not found" from Groq API.

### Pitfall 2: test_agentstate_has_required_fields Asserts 13 Fields
**What goes wrong:** Adding `resolved_query` to `AgentState` breaks `test_state.py::test_agentstate_has_required_fields`, which checks `len(hints) == 13` and `EXPECTED_FIELDS` set.
**Why it happens:** The test was written with the exact field count. AgentState expansion requires updating the test.
**How to avoid:** When adding `resolved_query: Optional[str]` to state.py, also update `EXPECTED_FIELDS` set and `assert len(hints) == 14` in test_state.py.
**Warning signs:** `AssertionError: Field mismatch` on the state test.

### Pitfall 3: LLM Returns Malformed JSON for Query Plan
**What goes wrong:** The planner prompt returns partial JSON, text wrapped in markdown fences, or appended explanation text after the JSON block.
**Why it happens:** LLMs at temperature > 0 are non-deterministic. Even at temperature=0 they sometimes add commentary.
**How to avoid:** Use `_parse_json_response()` helper that strips fences. Catch `ValueError` and return a safe default plan dict. Use temperature=0 for planning and generation nodes.
**Warning signs:** `json.JSONDecodeError` raised from planner node.

### Pitfall 4: route_after_gatekeeper Path Map Missing New Categories
**What goes wrong:** If gatekeeper returns `"follow_up"` or `"ambiguous"` but `builder.py`'s path map only has `{"schema_linker": ..., "formatter": ...}`, LangGraph raises a `ValueError: Branch ... returned unexpected value`.
**Why it happens:** LangGraph's `add_conditional_edges` validates that routing function return values are in the provided path map.
**How to avoid:** The new routing function returns only `"schema_linker"` or `"formatter"` — `follow_up` maps to `"schema_linker"`, `ambiguous` maps to `"formatter"`. The existing 2-key path map in builder.py already covers both. No change to builder.py required.
**Warning signs:** `ValueError` during graph traversal mentioning unexpected branch value.

### Pitfall 5: Schema Linker Breaks When Retriever Has No Embedded Schema
**What goes wrong:** `retrieve_tables()` on a fresh retriever (no schema embedded) returns empty results or raises on an empty namespace query.
**Why it happens:** Pinecone/ChromaDB require the namespace to be populated before querying.
**How to avoid:** In schema_linker_node, call `embed_schema()` before `retrieve_tables()`. `embed_schema()` is idempotent — it checks `namespace_exists()` and skips if already populated. Wrap entire retrieval in try/except with full-schema fallback.
**Warning signs:** Empty `relevant_tables` list in state even when schema is populated.

### Pitfall 6: ChatGroq Not in Installed Packages
**What goes wrong:** `from langchain_groq import ChatGroq` raises `ImportError` in tests if langchain-groq is not installed.
**Why it happens:** The package is in core deps but the test environment may not have it installed.
**How to avoid:** The lazy import (inside function body) protects against this in tests — patch `agents.nodes.gatekeeper.ChatGroq` before the import executes. In CI, ensure `pip install -e ".[dev]"` includes langchain-groq.
**Warning signs:** `ImportError: No module named 'langchain_groq'` in tests.

---

## Code Examples

Verified patterns from official sources and existing project conventions:

### ChatGroq Instantiation and Async Invocation
```python
# Source: https://console.groq.com/docs/langchain + langchain-groq package docs
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage

llm = ChatGroq(
    model="llama-3.3-70b-versatile",  # current model name (not deprecated llama3-70b-8192)
    temperature=0,
    max_tokens=1024,
    # api_key auto-read from GROQ_API_KEY env var
)
messages = [
    SystemMessage(content="You are a SQL expert."),
    HumanMessage(content="Show me total sales by region"),
]
response = await llm.ainvoke(messages)
content = response.content  # str — the LLM's text response
```

### Gatekeeper Classification Prompt Structure
```python
# Source: CONTEXT.md locked decisions + text-to-SQL SOTA research
_GATEKEEPER_PROMPT = """
You are a query classifier for a text-to-SQL system.

Classify the user query into EXACTLY ONE category:
- sql: The user wants data from the database (counts, totals, lists, trends, etc.)
- conversational: Greeting, thanks, or off-topic message not related to data
- follow_up: References a previous query ("what about last year?", "and for region X?")
- ambiguous: Cannot determine intent — need clarification

Also extract a one-line intent summary.

Respond with ONLY valid JSON:
{"category": "<sql|conversational|follow_up|ambiguous>", "intent": "<one-line summary>", "response": "<conversational reply or clarification question, empty string for sql/follow_up>"}

Chat history:
{chat_history}

User query: {user_query}
"""
```

### SQL Generator Dialect Prompt Pattern
```python
# Source: CONTEXT.md locked decisions
_DIALECT_REMINDERS = {
    "postgres": "Use ILIKE for case-insensitive matching. Use TO_CHAR() for date formatting.",
    "mysql": "Use DATE_FORMAT() for dates. Use CONCAT() for string concatenation.",
    "sqlite": "Use strftime() for dates. Use || for string concatenation.",
    "duckdb": "Use strptime() for date parsing. DuckDB supports QUALIFY clause for window filtering.",
}

# In sql_generator_node:
dialect_hint = _DIALECT_REMINDERS.get(db_type, "Generate standard SQL.")
```

### Markdown Fence Stripping
```python
# Source: common LLM output handling pattern
import json, re

def _parse_json_response(raw: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", raw.strip())
    cleaned = re.sub(r"\n?```\s*$", "", cleaned.strip())
    return json.loads(cleaned.strip())
```

### SQL Structural Validation
```python
# Source: CONTEXT.md locked decisions
def _validate_sql(sql: str) -> str:
    """Strip fences and verify starts with SELECT or WITH."""
    sql = re.sub(r"^```(?:sql)?\s*\n?", "", sql.strip())
    sql = re.sub(r"\n?```\s*$", "", sql.strip())
    upper = sql.strip().upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise ValueError(f"Generated SQL does not start with SELECT/WITH: {sql[:100]}")
    return sql.strip()
```

### Unit Test Pattern for Async Nodes
```python
# Source: existing project test patterns (asyncio.run + MagicMock)
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import AIMessage

def test_gatekeeper_classifies_sql():
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(
        content='{"category": "sql", "intent": "user wants total sales", "response": ""}'
    ))
    state = {"user_query": "show total sales", "db_manager": object(), "messages": []}

    with patch("agents.nodes.gatekeeper.ChatGroq", return_value=mock_llm):
        result = asyncio.run(gatekeeper_node(state))

    assert result["query_type"] == "sql"
    assert result["intent"] == "user wants total sales"
```

### conftest.py Pattern for Agent Tests
```python
# tests/agents/conftest.py — following existing graph/conftest.py pattern
def make_agent_state(user_query: str = "test query", db_manager=None) -> dict:
    return {
        "user_query": user_query,
        "resolved_query": None,
        "db_type": "sqlite",
        "db_manager": db_manager or object(),  # non-None = connected
        "query_type": None,
        "intent": None,
        "schema": {"Invoice": {"columns": [{"name": "Total", "type": "NUMERIC"}]}},
        "relevant_tables": None,
        "query_plan": None,
        "generated_sql": None,
        "db_results": None,
        "error_log": None,
        "retry_count": 0,
        "final_answer": None,
        "messages": [],
    }
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `llama3-70b-8192` model name | `llama-3.3-70b-versatile` | Dec 2024/Jan 2025 | Old name returns 400 error from Groq API |
| `model_name=` parameter | `model=` parameter | LangChain 0.2+ | Both work but `model=` is current canonical parameter name |
| Synchronous `llm.invoke()` in nodes | `await llm.ainvoke()` in async nodes | LangGraph 0.1+ | Synchronous invoke blocks event loop in async LangGraph |
| Full-schema context in every prompt | Schema linking + focused context | SOTA 2024-2025 | 80-95% token reduction; significantly improves generation accuracy |
| Plain text plan output | Structured JSON plan | Best practice 2024+ | Enables debugging panel rendering and programmatic SQL generation |

**Deprecated/outdated:**
- `llama3-70b-8192`: Groq deprecated all Llama 3.1 model IDs in January 2025. Use `llama-3.3-70b-versatile`.
- `create_structured_output_runnable`: Old LangChain API; use `with_structured_output()` or manual `json.loads` instead.
- `from langchain.chat_models import ChatGroq`: Old import path; use `from langchain_groq import ChatGroq`.

---

## Open Questions

1. **`intent` field in AgentState**
   - What we know: Gatekeeper returns `intent` (one-line summary) per CONTEXT.md. Downstream nodes (planner, schema linker) benefit from it.
   - What's unclear: Whether `intent` should be added as an explicit AgentState field or passed via an existing field (e.g., stored in `messages`).
   - Recommendation: Add `intent: Optional[str]` to AgentState alongside `resolved_query`. Update test_state.py to 15 fields. Low-risk, high-value for debugging.

2. **`query_complexity` field for planner output**
   - What we know: CONTEXT.md says complexity (`simple`/`moderate`/`complex`) is "stored in state for debugging panel."
   - What's unclear: Whether this needs a new AgentState field now or can be embedded in `query_plan` dict.
   - Recommendation: Include `complexity` as a key inside the `query_plan` dict rather than a separate state field. Avoids another state schema change: `{"select": ..., "complexity": "moderate", ...}`.

3. **SQL explanation storage**
   - What we know: SQL generator returns "SQL + 1-2 sentence explanation." Displayed in debugging panel.
   - What's unclear: Whether explanation needs its own AgentState field or can be co-located with `generated_sql`.
   - Recommendation: Store as a new `sql_explanation: Optional[str]` state field, or embed in `query_plan` dict. Planner makes this decision when writing the PLAN.md.

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.x |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `pytest tests/agents/ -v --tb=short` |
| Full suite command | `pytest tests/ -v --tb=short` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| AGENT-001 | Gatekeeper classifies `sql` query correctly | unit | `pytest tests/agents/test_gatekeeper.py -x` | Wave 0 |
| AGENT-001 | Gatekeeper classifies `conversational` and sets final_answer | unit | `pytest tests/agents/test_gatekeeper.py -x` | Wave 0 |
| AGENT-001 | Gatekeeper detects `follow_up` and writes resolved_query | unit | `pytest tests/agents/test_gatekeeper.py -x` | Wave 0 |
| AGENT-001 | Gatekeeper returns `ambiguous` with clarification message | unit | `pytest tests/agents/test_gatekeeper.py -x` | Wave 0 |
| AGENT-001 | Gatekeeper returns early when db_manager is None | unit | `pytest tests/agents/test_gatekeeper.py -x` | Wave 0 |
| AGENT-001 | Gatekeeper blocks NL destructive intent | unit | `pytest tests/agents/test_gatekeeper.py -x` | Wave 0 |
| AGENT-002 | Schema linker calls retriever and populates relevant_tables | unit | `pytest tests/agents/test_schema_linker.py -x` | Wave 0 |
| AGENT-002 | Schema linker falls back to full schema on retriever failure | unit | `pytest tests/agents/test_schema_linker.py -x` | Wave 0 |
| AGENT-003 | Planner returns JSON plan with required keys | unit | `pytest tests/agents/test_query_planner.py -x` | Wave 0 |
| AGENT-003 | Planner handles malformed JSON gracefully (default plan) | unit | `pytest tests/agents/test_query_planner.py -x` | Wave 0 |
| AGENT-004 | SQL generator returns SELECT statement | unit | `pytest tests/agents/test_sql_generator.py -x` | Wave 0 |
| AGENT-004 | SQL generator strips markdown fences | unit | `pytest tests/agents/test_sql_generator.py -x` | Wave 0 |
| AGENT-004 | SQL generator rejects non-SELECT output | unit | `pytest tests/agents/test_sql_generator.py -x` | Wave 0 |
| LLM-003 | Each node's module-level prompt constant is non-empty string | unit | `pytest tests/agents/ -x -k prompt` | Wave 0 |

### Sampling Rate
- **Per task commit:** `pytest tests/agents/ -v --tb=short`
- **Per wave merge:** `pytest tests/ -v --tb=short`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/agents/__init__.py` — package marker
- [ ] `tests/agents/conftest.py` — `make_agent_state()` fixture
- [ ] `tests/agents/test_gatekeeper.py` — covers AGENT-001
- [ ] `tests/agents/test_schema_linker.py` — covers AGENT-002
- [ ] `tests/agents/test_query_planner.py` — covers AGENT-003
- [ ] `tests/agents/test_sql_generator.py` — covers AGENT-004
- [ ] Update `tests/graph/test_state.py` — update `EXPECTED_FIELDS` and field count after adding `resolved_query` (and optionally `intent`, `sql_explanation`) to AgentState

---

## Sources

### Primary (HIGH confidence)
- https://console.groq.com/docs/langchain — ChatGroq constructor, model names, async support confirmed
- https://console.groq.com/docs/model/llama-3.3-70b-versatile — current model ID `llama-3.3-70b-versatile`, context 131K, JSON mode confirmed
- https://console.groq.com/docs/deprecations — llama3-70b-8192 deprecation January 2025 confirmed
- Existing codebase (graph/state.py, graph/conditions.py, graph/builder.py, vector/retriever.py) — all integration points verified by direct file read
- pyproject.toml — dependency versions confirmed

### Secondary (MEDIUM confidence)
- https://docs.langchain.com/oss/python/integrations/chat/groq — `model=` parameter, async native support confirmed via feature matrix
- LangChain ChatGroq search results — `ainvoke` pattern, `AIMessage.content`, `AsyncMock` testing approach verified across multiple sources
- Text-to-SQL SOTA papers (arxiv 2024-2025) — JSON structured plan format consistent with CONTEXT.md decisions

### Tertiary (LOW confidence)
- Medium articles on mocking LangChain LLMs — testing patterns described consistent with Python unittest.mock docs; not directly verified against latest LangChain version

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — confirmed from Groq official docs and pyproject.toml
- Architecture: HIGH — derived from existing codebase patterns + locked CONTEXT.md decisions
- ChatGroq model name: HIGH — confirmed on Groq official docs, deprecation notice dated
- JSON parsing approach: HIGH — consistent with existing project patterns (no Pydantic dependency on output)
- Pitfalls: HIGH — most derived from direct code inspection (test_state.py field count, builder.py path map)
- Prompt wording: MEDIUM — Claude's discretion; examples are illustrative, not official

**Research date:** 2026-03-15
**Valid until:** 2026-04-15 (stable LangChain/Groq APIs; Groq model names may change — verify before use)
