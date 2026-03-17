"""Query planner agent node — generates a structured JSON execution plan via ChatGroq."""
import json
import logging
import re
from typing import Any

from graph.state import AgentState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM-003: module-level prompt constant
# ---------------------------------------------------------------------------

_PLANNER_PROMPT = """\
You are an expert SQL query planner. Given a natural-language question, a database schema, \
and a list of relevant tables, produce a JSON execution plan for the SQL query.

Schema context:
{schema_context}

Relevant tables: {relevant_tables}

Respond with ONLY a single valid JSON object — no prose, no markdown fences. \
The JSON must have exactly these keys:
  "select"     : list of column expressions (strings)
  "from"       : primary table name (string)
  "joins"      : list of objects with keys "table", "type", "on"
  "where"      : list of condition strings
  "group_by"   : list of column names
  "order_by"   : list of column names or expressions
  "limit"      : integer or null
  "ctes"       : list of objects with keys "name", "query"
  "complexity" : one of "simple", "moderate", "complex"
"""

# ---------------------------------------------------------------------------
# Safe-default plan (returned when JSON parsing fails)
# ---------------------------------------------------------------------------

_DEFAULT_PLAN: dict[str, Any] = {
    "select": [],
    "from": "",
    "joins": [],
    "where": [],
    "group_by": [],
    "order_by": [],
    "limit": None,
    "ctes": [],
    "complexity": "simple",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json_response(raw: str) -> dict:
    """Strip markdown fences then parse JSON. Raises ValueError on failure."""
    cleaned = re.sub(r"```(?:json)?\s*", "", raw, flags=re.IGNORECASE).replace("```", "").strip()
    return json.loads(cleaned)


def _format_schema_context(schema: dict) -> str:
    """Convert schema dict to a human-readable string for the prompt."""
    if not schema:
        return "(no schema provided)"
    lines = []
    for table, info in schema.items():
        cols = info.get("columns", [])
        col_strs = [f"{c.get('name', '?')} {c.get('type', '')}" for c in cols]
        lines.append(f"Table {table}: {', '.join(col_strs)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def query_planner_node(state: AgentState) -> dict:
    """Generate a Chain-of-Thought JSON query plan from the resolved query and schema."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from llm.fallback import get_llm

    query = state.get("resolved_query") or state.get("user_query", "")
    schema = state.get("schema") or {}
    relevant_tables = state.get("relevant_tables") or []

    schema_context = _format_schema_context(schema)
    system_content = _PLANNER_PROMPT.format(
        schema_context=schema_context,
        relevant_tables=relevant_tables,
    )

    llm = get_llm(node="query_planner", state=state)
    messages = [
        SystemMessage(content=system_content),
        HumanMessage(content=query),
    ]

    response = await llm.ainvoke(messages)

    try:
        plan = _parse_json_response(response.content)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("query_planner_node: JSON parse failed (%s) — using default plan", exc)
        plan = dict(_DEFAULT_PLAN)  # fresh copy

    return {"query_plan": plan}
