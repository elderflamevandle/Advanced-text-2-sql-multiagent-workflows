"""Correction plan agent node — classifies SQL errors and generates a structured correction plan."""
import json
import logging
import re

from agents.prompt_context import schema_prompt
from graph.state import AgentState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ERROR-002: module-level prompt constant
# ---------------------------------------------------------------------------

_CORRECTION_PLAN_PROMPT = """\
You are an expert SQL debugger. Analyze the failed SQL query and its error, then produce a structured correction plan.

Failed SQL:
{failed_sql}

Error details (JSON):
{error_log_json}

Error category: {error_category}
Severity: {severity}
Correction strategy hint: {strategy_hint}
Prompt guidance: {prompt_hint}

Relevant schema:
{schema_context}

Fuzzy name suggestions (for name-related errors):
{fuzzy_suggestions}

Original user question: {user_query}

Respond with ONLY a single valid JSON object with exactly these keys:
  "error_category"    : string — confirmed error category name
  "root_cause"        : string — precise root cause description
  "correction_steps"  : list of strings — ordered steps to fix the issue
  "affected_clauses"  : list of strings — SQL clauses to modify (e.g., ["FROM", "WHERE"])
  "suggested_changes" : dict — specific replacements or fixes
  "confidence"        : string — "high" if regex-matched, "medium" if LLM-classified\
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_candidate_pool(state: AgentState) -> list:
    """Build a list of candidate names from relevant_tables + their column names."""
    relevant_tables = state.get("relevant_tables") or []
    schema = state.get("schema") or {}
    candidates = list(relevant_tables)
    for table in relevant_tables:
        table_info = schema.get(table, {})
        cols = table_info.get("columns", [])
        candidates.extend(c.get("name", "") for c in cols if c.get("name"))
    return candidates


def _extract_unrecognized_name(error_message: str) -> str:
    """Extract the unrecognized identifier name from an error message."""
    # Try quoted name first (single or double quotes)
    match = re.search(r"['\"]([^'\"]+)['\"]", error_message)
    if match:
        return match.group(1)
    # Fall back: take everything after the last colon + space
    if ": " in error_message:
        return error_message.rsplit(": ", 1)[-1].strip()
    return ""


# Error categories that mean "the query needs a table or column the selection lacks".
RESELECT_CATEGORIES = frozenset({"missing_table", "missing_column"})
RESELECT_ACTION = "reselect"
_MAX_REQUIRED_TABLES = 5


def _should_reselect(state: AgentState, category_id: str) -> bool:
    """Go back to table selection: only for a missing table/column, only once per query,
    and only when the tables came from the schema catalog."""
    return (
        category_id in RESELECT_CATEGORIES
        and not state.get("reselect_count")
        and bool(state.get("schema_context"))
        and bool(state.get("connection_id"))
    )


def _all_tables(state: AgentState) -> dict:
    """{table: [column names]} for the whole database (registry), {} when unavailable."""
    try:
        from database.registry import get_registry

        schema = get_registry().get_schema(state.get("tenant_id"), state.get("connection_id"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("correction_plan_node: full schema unavailable (%s)", type(exc).__name__)
        return {}
    tables = {}
    for name, info in (schema or {}).items():
        cols = (info or {}).get("columns") or [] if isinstance(info, dict) else []
        tables[str(name)] = [str(c.get("name")) for c in cols
                             if isinstance(c, dict) and c.get("name")]
    return tables


def _required_tables(category_id: str, unrecognized: str, tables: dict) -> list:
    """Real table names the next selection must include, derived from the error."""
    from utils.error_parser import get_fuzzy_matches

    name = (unrecognized or "").strip().strip('`"[]')
    if not name or not tables:
        return []
    by_lower = {t.lower(): t for t in tables}
    found: list = []
    parts = [p.strip('`"[] ') for p in name.split(".") if p.strip('`"[] ')]
    if category_id == "missing_table":
        # "schema.table" -> the last part names the table.
        target = parts[-1] if parts else name
        if target.lower() in by_lower:
            found.append(by_lower[target.lower()])
        found.extend(get_fuzzy_matches(target, list(tables)))
    else:
        # "alias.column" or "table.column": a real table qualifier, then owners of the column.
        if len(parts) > 1 and parts[-2].lower() in by_lower:
            found.append(by_lower[parts[-2].lower()])
        column = (parts[-1] if parts else name).lower()
        found.extend(t for t, cols in tables.items()
                     if any(c.lower() == column for c in cols))
    return [t for t in dict.fromkeys(found) if t in tables][:_MAX_REQUIRED_TABLES]


async def _reselect_update(state: AgentState, category: dict, confidence: str) -> dict:
    """State update that sends the run back to the schema linker (see graph/conditions.py).

    No LLM call: the diagnosis is the taxonomy match. The failed attempt is recorded in
    sql_history and counted against the retry limit, exactly as correction_sql_node does.
    """
    import asyncio

    error_log = state.get("error_log") or {}
    message = error_log.get("message", "") if isinstance(error_log, dict) else str(error_log)
    category_id = category.get("id", "unknown")
    unrecognized = _extract_unrecognized_name(message)
    tables = await asyncio.to_thread(_all_tables, state)
    required = _required_tables(category_id, unrecognized, tables)

    retry_count = state.get("retry_count") or 0
    history = list(state.get("sql_history") or [])
    history.append({
        "sql": state.get("generated_sql", "") or "",
        "error": error_log,
        "attempt_num": retry_count,
    })
    kind = "table" if category_id == "missing_table" else "column"
    plan = {
        "error_category": category_id,
        "root_cause": (f"The query used a {kind} ({unrecognized or 'unknown'}) that is not "
                       "in the tables selected for the question."),
        "correction_steps": ["Select the tables again, including the ones the error points "
                             "to, and write the query from the new selection."],
        "affected_clauses": [],
        "suggested_changes": {"required_tables": required},
        "confidence": confidence,
        "fuzzy_suggestions": required,
        "severity": category.get("severity", "recoverable"),
        "strategy": "reselect_tables",
        "prompt_hint": category.get("prompt_hint", ""),
        "action": RESELECT_ACTION,
    }
    logger.info("correction_plan_node: %s -> selecting tables again (required=%s)",
                category_id, required)
    return {
        "correction_plan": plan,
        "reselect_count": 1,
        "required_tables": required,
        "sql_history": history,
        "retry_count": retry_count + 1,
        "error_log": None,  # cleared like correction_sql_node does, so routing starts fresh
    }


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def correction_plan_node(state: AgentState) -> dict:
    """Diagnose SQL execution errors using error taxonomy and (for non-transient errors) LLM."""
    from utils.error_parser import _load_taxonomy, classify_error, get_fuzzy_matches  # lazy import

    error_log = state.get("error_log") or {}

    # Classify the error against the taxonomy
    taxonomy = _load_taxonomy()
    category, confidence = classify_error(error_log, taxonomy)

    # --- EARLY RETURN for transient errors (no LLM call) ---
    if category.get("severity") == "transient":
        logger.info(
            "correction_plan_node: transient error %s — passthrough plan",
            category["id"],
        )
        return {
            "correction_plan": {
                "error_category": category["id"],
                "root_cause": "Transient error — retry same SQL unchanged",
                "correction_steps": ["retry_unchanged"],
                "affected_clauses": [],
                "suggested_changes": {},
                "confidence": confidence,
                "fuzzy_suggestions": [],
                "severity": "transient",
                "strategy": category.get("strategy", "retry_unchanged"),
                "prompt_hint": category.get("prompt_hint", ""),
            }
        }

    # --- Missing table/column with a catalog: select the tables again (once, no LLM) ---
    if isinstance(error_log, dict) and _should_reselect(state, category.get("id", "")):
        return await _reselect_update(state, category, confidence)

    # --- Build fuzzy suggestions for name-related errors ---
    name_related = category.get("id", "") in ("missing_table", "missing_column", "ambiguous_column")
    fuzzy_suggestions: list = []
    if name_related:
        candidates = _build_candidate_pool(state)
        error_message = error_log.get("message", "") if isinstance(error_log, dict) else str(error_log)
        unrecognized = _extract_unrecognized_name(error_message)
        if unrecognized and candidates:
            fuzzy_suggestions = get_fuzzy_matches(unrecognized, candidates)

    # --- Format schema context ---
    schema_context = schema_prompt(state)

    # --- Build prompt ---
    error_message = error_log.get("message", "") if isinstance(error_log, dict) else str(error_log)
    error_log_json = json.dumps(error_log, indent=2) if isinstance(error_log, dict) else str(error_log)
    failed_sql = state.get("generated_sql", "") or ""
    user_query = state.get("user_query", "") or ""

    system_content = _CORRECTION_PLAN_PROMPT.format(
        failed_sql=failed_sql,
        error_log_json=error_log_json,
        error_category=category.get("id", "unknown"),
        severity=category.get("severity", "recoverable"),
        strategy_hint=category.get("strategy", "general_fix"),
        prompt_hint=category.get("prompt_hint", ""),
        schema_context=schema_context,
        fuzzy_suggestions=fuzzy_suggestions,
        user_query=user_query,
    )

    # --- LLM call (lazy imports) ---
    from langchain_core.messages import HumanMessage, SystemMessage
    from llm.fallback import get_llm

    llm = get_llm(node="correction_plan", state=state)
    messages = [
        SystemMessage(content=system_content),
        HumanMessage(content=f"Diagnose this SQL error: {error_message}"),
    ]

    response = await llm.ainvoke(messages)
    raw = response.content

    # Strip markdown fences and parse JSON
    cleaned = re.sub(r"```(?:json)?\s*", "", raw, flags=re.IGNORECASE).replace("```", "").strip()

    try:
        plan = json.loads(cleaned)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "correction_plan_node: JSON parse failed (%s) — returning safe fallback plan", exc
        )
        plan = {
            "error_category": category.get("id", "unknown"),
            "root_cause": f"Parse error: {error_message}",
            "correction_steps": ["Review and manually fix the SQL query"],
            "affected_clauses": [],
            "suggested_changes": {},
            "confidence": "low",
        }

    # Merge taxonomy metadata into parsed plan (ensure all required keys are present)
    plan["error_category"] = plan.get("error_category") or category.get("id", "unknown")
    plan["fuzzy_suggestions"] = fuzzy_suggestions
    plan["severity"] = category.get("severity", "recoverable")
    plan["strategy"] = plan.get("strategy") or category.get("strategy", "general_fix")
    plan["prompt_hint"] = plan.get("prompt_hint") or category.get("prompt_hint", "")

    logger.info(
        "correction_plan_node: category=%s severity=%s confidence=%s steps=%d",
        plan.get("error_category"),
        plan.get("severity"),
        confidence,
        len(plan.get("correction_steps", [])),
    )

    return {"correction_plan": plan}
