"""SQL generator agent node — translates query plan to dialect-specific SQL via ChatGroq."""
import json
import logging
import re
from typing import Optional

from graph.state import AgentState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM-003: module-level prompt constant
# ---------------------------------------------------------------------------

_GENERATOR_PROMPT = """\
You are an expert SQL developer. Given a structured query plan, a database schema, \
the target database dialect, and a natural-language question, generate dialect-specific SQL.

Query plan (JSON):
{query_plan_json}

Schema context:
{schema_context}

Database dialect: {db_type}
Dialect-specific reminders: {dialect_reminder}

Rules:
1. ONLY generate SELECT or WITH (CTE) statements — never INSERT, UPDATE, DELETE, DROP, etc.
2. Add brief inline comments for non-obvious logic.
3. Output format (use exactly these labels):
   SQL:
   <the sql statement>
   EXPLANATION:
   <1-2 sentence plain-English explanation of what the query does>
"""

# ---------------------------------------------------------------------------
# Dialect reminders
# ---------------------------------------------------------------------------

_DIALECT_REMINDERS: dict[str, str] = {
    "postgres": "Use ILIKE for case-insensitive. Use TO_CHAR() for dates. Use || for concatenation.",
    "mysql": "Use DATE_FORMAT() for dates. Use CONCAT() for concatenation. Use LIKE (case-insensitive by default).",
    "sqlite": "Use strftime() for dates. Use || for concatenation. No ILIKE — use LOWER().",
    "duckdb": "Use strptime() for date parsing. Supports QUALIFY for window filtering. Use ILIKE.",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_sql(sql: str) -> str:
    """Strip markdown fences and verify SQL starts with SELECT or WITH.

    Returns clean SQL string on success.
    Raises ValueError if statement is not a read-only SELECT/WITH query.
    """
    cleaned = re.sub(r"```(?:sql)?\s*", "", sql, flags=re.IGNORECASE).replace("```", "").strip()
    upper = cleaned.upper().lstrip()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise ValueError(f"Unsafe or unsupported SQL statement (must start with SELECT or WITH): {cleaned[:80]!r}")
    return cleaned


def _format_schema_context(schema: dict) -> str:
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

async def sql_generator_node(state: AgentState) -> dict:
    """Translate the structured query plan into dialect-specific SQL with explanation."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from llm.fallback import get_llm

    query = state.get("resolved_query") or state.get("user_query", "")
    query_plan = state.get("query_plan") or {}
    db_type = state.get("db_type") or "sqlite"
    schema = state.get("schema") or {}

    dialect_reminder = _DIALECT_REMINDERS.get(db_type, "Generate standard SQL.")
    schema_context = _format_schema_context(schema)
    query_plan_json = json.dumps(query_plan, indent=2) if query_plan else "{}"

    system_content = _GENERATOR_PROMPT.format(
        query_plan_json=query_plan_json,
        schema_context=schema_context,
        db_type=db_type,
        dialect_reminder=dialect_reminder,
    )

    llm = get_llm(node="sql_generator", state=state)
    messages = [
        SystemMessage(content=system_content),
        HumanMessage(content=query),
    ]

    response = await llm.ainvoke(messages)
    raw = response.content

    # Split on EXPLANATION: label
    parts = re.split(r"EXPLANATION\s*:\s*", raw, maxsplit=1, flags=re.IGNORECASE)
    sql_part = parts[0]
    explanation: Optional[str] = parts[1].strip() if len(parts) > 1 else None

    # Strip leading "SQL:" label if present
    sql_part = re.sub(r"^SQL\s*:\s*", "", sql_part.strip(), flags=re.IGNORECASE).strip()

    try:
        validated_sql = _validate_sql(sql_part)
    except ValueError as exc:
        logger.warning("sql_generator_node: SQL validation failed — %s", exc)
        return {"generated_sql": None, "error_log": str(exc)}

    return {"generated_sql": validated_sql, "sql_explanation": explanation}
