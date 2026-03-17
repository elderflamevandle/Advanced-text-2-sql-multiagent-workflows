"""Correction SQL agent node — rewrites failing SQL using the correction plan via ChatGroq."""
import logging
import re
from typing import Optional

from graph.state import AgentState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ERROR-002: module-level prompt constant
# ---------------------------------------------------------------------------

_CORRECTION_SQL_PROMPT = """\
You are an expert SQL developer performing targeted error correction.

Original SQL (failed):
{failed_sql}

Correction plan:
{correction_plan_json}

Database dialect: {db_type}
Schema context:
{schema_context}

Rules:
1. Fix ONLY the issues identified in the correction plan.
2. Preserve all other clauses, logic, and structure exactly.
3. Never introduce INSERT, UPDATE, DELETE, DROP, or other write operations.
4. Output format (use exactly these labels):
   SQL:
   <corrected sql>
   EXPLANATION:
   <what was changed and why, 1-3 sentences>\
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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

async def correction_sql_node(state: AgentState) -> dict:
    """Apply correction strategy to fix the failing SQL. Always returns error_log: None."""
    import json

    current_sql = state.get("generated_sql", "") or ""
    retry_count = state.get("retry_count", 0)
    correction_plan = state.get("correction_plan") or {}
    error_log = state.get("error_log")

    # Build sql_history entry for the current (failed) attempt
    new_entry = {
        "sql": current_sql,
        "error": error_log,
        "attempt_num": retry_count,
    }
    history = list(state.get("sql_history") or [])  # CRITICAL: list() copy to avoid mutation
    history.append(new_entry)

    # --- TRANSIENT PASSTHROUGH PATH ---
    steps = correction_plan.get("correction_steps", [])
    if steps == ["retry_unchanged"]:
        logger.info(
            "correction_sql_node: transient passthrough — retry_count=%d->%d",
            retry_count,
            retry_count + 1,
        )
        return {
            "sql_history": history,
            "retry_count": retry_count + 1,
            "error_log": None,  # clear so executor starts fresh with same SQL
        }

    # --- NORMAL LLM REWRITE PATH ---
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_groq import ChatGroq  # lazy import — optional dep

    db_type = state.get("db_type", "sqlite") or "sqlite"
    schema = state.get("schema") or {}
    schema_context = _format_schema_context(schema)
    correction_plan_json = json.dumps(correction_plan, indent=2)

    system_content = _CORRECTION_SQL_PROMPT.format(
        failed_sql=current_sql,
        correction_plan_json=correction_plan_json,
        db_type=db_type,
        schema_context=schema_context,
    )

    user_query = state.get("user_query", "") or ""
    llm = ChatGroq(model="llama-3.3-70b-versatile", temperature=0, max_tokens=2048)
    messages = [
        SystemMessage(content=system_content),
        HumanMessage(content=f"Fix the SQL for: {user_query}"),
    ]

    try:
        response = await llm.ainvoke(messages)
        raw = response.content

        # Split on EXPLANATION: label (same pattern as sql_generator.py)
        parts = re.split(r"EXPLANATION\s*:\s*", raw, maxsplit=1, flags=re.IGNORECASE)
        sql_part = parts[0]
        explanation: Optional[str] = parts[1].strip() if len(parts) > 1 else None

        # Strip leading "SQL:" label if present
        sql_part = re.sub(r"^SQL\s*:\s*", "", sql_part.strip(), flags=re.IGNORECASE).strip()

        # Strip markdown fences if present
        sql_part = re.sub(r"```(?:sql)?\s*", "", sql_part, flags=re.IGNORECASE).replace("```", "").strip()

        corrected_sql = sql_part if sql_part else current_sql

        logger.info(
            "correction_sql_node: LLM rewrite complete — retry_count=%d->%d",
            retry_count,
            retry_count + 1,
        )

        return {
            "generated_sql": corrected_sql,
            "sql_explanation": f"[Correction attempt {retry_count + 1}] {explanation}" if explanation else None,
            "sql_history": history,
            "retry_count": retry_count + 1,
            "error_log": None,  # CRITICAL: always clear to prevent routing loop
        }

    except Exception as exc:
        logger.warning(
            "correction_sql_node: LLM/parse failure (%s) — passthrough best effort", exc
        )
        return {
            "generated_sql": current_sql,  # best-effort passthrough
            "sql_history": history,
            "retry_count": retry_count + 1,
            "error_log": None,  # CRITICAL: still clear to prevent routing loop
        }
