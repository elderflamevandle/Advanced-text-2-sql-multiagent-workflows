"""Query planner agent node — generates a structured JSON execution plan via ChatGroq."""
import json
import logging
import re
from typing import Any

from agents.prompt_context import SCHEMA_RULES, extra_context, schema_prompt
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

# Appended to the planner prompt when app.clarification_enabled is true.
_CLARIFICATION_BLOCK = """
Also include these two keys:
  "needs_clarification"    : true or false
  "clarification_question" : string (empty when needs_clarification is false)
Set "needs_clarification" to true ONLY when the question cannot be answered from the listed \
schema without guessing between materially different readings that would give different \
results (for example two unrelated columns could both be the "amount" asked for). \
Never ask when a sensible default resolves it: an unstated time range means all time, an \
unstated sort order or row limit is your choice, "top" means highest first. Never ask about \
SQL details, and never ask again when the question already contains the user's answer to an \
earlier clarification. When true, "clarification_question" is one short question that names \
the options, and the other keys still hold your best-guess plan.
"""

_CLARIFICATION_MAX_CHARS = 500

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



def _clarification_enabled() -> bool:
    """app.clarification_enabled from config/config.yaml (default: on)."""
    try:
        from llm.config import get_section

        return bool(get_section("app").get("clarification_enabled", True))
    except Exception:  # noqa: BLE001 - unreadable config: keep the default
        return True


def _clarification_question(plan: dict) -> str | None:
    """The question to ask the user, when the plan says one is needed."""
    if not isinstance(plan, dict) or plan.get("needs_clarification") is not True:
        return None
    question = " ".join(str(plan.get("clarification_question") or "").split())
    return question[:_CLARIFICATION_MAX_CHARS] or None


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def query_planner_node(state: AgentState) -> dict:
    """Generate a Chain-of-Thought JSON query plan from the resolved query and schema."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from llm.fallback import get_llm

    query = state.get("resolved_query") or state.get("user_query", "")
    relevant_tables = state.get("relevant_tables") or []

    system_content = _PLANNER_PROMPT.format(
        schema_context=schema_prompt(state),
        relevant_tables=relevant_tables,
    )
    # Never ask on the second pass after a failed query: the user already got past here.
    may_clarify = _clarification_enabled() and not state.get("reselect_count")
    if may_clarify:
        system_content += _CLARIFICATION_BLOCK
    system_content += f"\n{SCHEMA_RULES}" + extra_context(state, previous_attempt=True)

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

    question = _clarification_question(plan) if may_clarify else None
    if question:
        logger.info("query_planner_node: asking the user for clarification")
        return {
            "query_plan": plan,
            "clarification_question": question,
            "final_answer": question,
            "query_type": "clarification",
        }

    return {"query_plan": plan}
