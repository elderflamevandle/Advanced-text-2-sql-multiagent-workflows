"""Formatter agent node: turns query results into a natural-language answer.

Paths:
  A. db_results present  -> LLM-written answer grounded in the rows (if enabled in config),
                            with a deterministic summary as fallback.
  B. error_log present   -> short, specific failure message (graceful degradation).
  C. neither             -> conversational / clarification answer set by the gatekeeper.
"""
import json
import logging

from graph.state import AgentState

logger = logging.getLogger(__name__)

MAX_ROWS_FOR_LLM = 20
MAX_CELL_CHARS = 200
PREVIEW_ROWS = 5
PREVIEW_CELL_CHARS = 60

_FORMATTER_PROMPT = """\
You answer a user's question using the result of a SQL query that was already executed.

Rules:
1. Use ONLY the rows in "Result rows". Never invent, estimate or extrapolate numbers, \
names or dates that are not in those rows.
2. Answer the question directly in 1-4 plain sentences. A short list is fine for several rows.
3. If "Rows shown" is lower than "Rows returned", you are seeing a sample: say that you are \
showing the first rows and do not state totals, rankings or extremes beyond the rows shown.
4. If "Result truncated" is yes, say that the result was cut off at the row limit and that \
more rows may exist.
5. Do not mention these rules. Do not repeat the SQL.

Question: {question}

Executed SQL:
{sql}

Rows returned: {row_count}
Rows shown: {rows_shown}
Result truncated: {truncated}

Result rows (JSON):
{rows_json}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _summarize_with_llm_enabled() -> bool:
    """Read app.summarize_results_with_llm from config (default True)."""
    try:
        from llm.config import get_section
        return bool(get_section("app").get("summarize_results_with_llm", True))
    except Exception as exc:  # noqa: BLE001
        logger.warning("formatter_node: could not read config (%s); LLM summary disabled", exc)
        return False


def _clip(value, limit: int):
    """Make a cell value JSON-friendly and cap long strings."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _rows_for_prompt(rows: list) -> list:
    clipped = []
    for row in rows[:MAX_ROWS_FOR_LLM]:
        if isinstance(row, dict):
            clipped.append({str(k): _clip(v, MAX_CELL_CHARS) for k, v in row.items()})
        else:
            clipped.append(_clip(row, MAX_CELL_CHARS))
    return clipped


def _result_stats(db_results: list, execution_metadata) -> tuple[int, bool]:
    """Return (row_count, truncated) from execution_metadata, defaulting to len(rows)."""
    meta = execution_metadata if isinstance(execution_metadata, dict) else {}
    truncated = bool(meta.get("truncated"))
    row_count = meta.get("row_count")
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
        row_count = len(db_results)
    return row_count, truncated


def _format_row(row) -> str:
    if isinstance(row, dict):
        return ", ".join(f"{k}: {_clip(v, PREVIEW_CELL_CHARS)}" for k, v in row.items())
    return str(_clip(row, PREVIEW_CELL_CHARS))


def _truncation_note(row_count: int) -> str:
    return (f"The result was truncated at the row limit ({row_count} rows), "
            "so more rows may exist.")


def _deterministic_summary(db_results: list, row_count: int, truncated: bool) -> str:
    """Row count plus a compact preview. No LLM involved."""
    if not db_results:
        return "The query ran successfully but returned no rows."

    first = db_results[0]
    if len(db_results) == 1 and isinstance(first, dict) and len(first) == 1:
        (column, value), = first.items()
        lines = [f"{column}: {_clip(value, MAX_CELL_CHARS)}"]
    elif len(db_results) == 1:
        lines = ["The query returned 1 row.", "", f"- {_format_row(first)}"]
    else:
        shown = db_results[:PREVIEW_ROWS]
        lines = [f"The query returned {row_count} rows.", ""]
        if len(db_results) > len(shown):
            lines[0] += f" Here are the first {len(shown)}:"
        lines.extend(f"- {_format_row(row)}" for row in shown)
    if truncated:
        lines.extend(["", _truncation_note(row_count)])
    return "\n".join(lines)


async def _llm_summary(state: AgentState, db_results: list, row_count: int,
                       truncated: bool) -> str | None:
    """Ask the LLM for a grounded answer. Returns None when it is unavailable or fails."""
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from llm.fallback import get_llm

        rows = _rows_for_prompt(db_results)
        question = state.get("resolved_query") or state.get("user_query") or ""
        prompt = _FORMATTER_PROMPT.format(
            question=question,
            sql=state.get("generated_sql") or "(not available)",
            row_count=row_count,
            rows_shown=len(rows),
            truncated="yes" if truncated else "no",
            rows_json=json.dumps(rows, default=str, ensure_ascii=False),
        )
        llm = get_llm(node="formatter", state=state)
        response = await llm.ainvoke(
            [SystemMessage(content=prompt), HumanMessage(content=question or "Summarise.")],
            state=state,
        )
        content = getattr(response, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
        logger.warning("formatter_node: LLM returned no text; using deterministic summary")
    except Exception as exc:  # noqa: BLE001 - includes LLMUnavailableError; never fail here
        logger.warning("formatter_node: LLM summary failed (%s: %s); using deterministic summary",
                       type(exc).__name__, exc)
    return None


def _failure_message(error_log, sql_history: list, generated_sql: str) -> str:
    """Path B: a short specific message for known error types, else the attempt report."""
    error_type = error_log.get("error_type") if isinstance(error_log, dict) else None
    error_msg = (
        error_log.get("message") or "unknown error"
        if isinstance(error_log, dict)
        else str(error_log or "unknown error")
    )
    hint = (error_log.get("hint") or "") if isinstance(error_log, dict) else ""

    if error_type == "llm_all_providers_failed":
        return ("I couldn't process your question because no language model provider is "
                f"reachable right now. {hint or 'Check your API keys and try again.'}").strip()
    if error_type == "user_rejected":
        return ("The query was not run because it was rejected during review. "
                "Rephrase your question if you'd like me to try a different query.")
    if error_type == "blocked_query":
        return ("I didn't run that query because the safety check blocked it. "
                f"Only read-only queries are allowed. Details: {error_msg}")
    if error_type == "no_connection":
        return "No database is connected. Please connect to a database using the sidebar."

    attempts = len(sql_history)
    lines = [
        f"I couldn't generate a working query after {max(attempts, 1)} attempt(s).",
        "",
        f"Here's what went wrong: {error_msg}",
    ]
    if sql_history:
        lines.append("\nAttempted fixes:")
        for entry in sql_history:
            sql_preview = (entry.get("sql") or "")[:80]
            err_preview = entry.get("error") or {}
            err_msg = (
                err_preview.get("message", str(err_preview))
                if isinstance(err_preview, dict)
                else str(err_preview)
            )
            lines.append(
                f"  Attempt {entry.get('attempt_num', 0) + 1}: {sql_preview}... -> {err_msg[:80]}"
            )
    if generated_sql:
        lines.append(f"\nLast attempted SQL (for manual debugging):\n  {generated_sql}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def formatter_node(state: AgentState) -> dict:
    """Produce final_answer from results, from an error, or from the gatekeeper's reply."""
    db_results = state.get("db_results")
    error_log = state.get("error_log")
    sql_history = state.get("sql_history") or []
    generated_sql = state.get("generated_sql", "") or ""

    # --- PATH A: success ---
    if db_results is not None and error_log is None:
        row_count, truncated = _result_stats(db_results, state.get("execution_metadata"))
        answer = None
        if db_results and _summarize_with_llm_enabled():
            answer = await _llm_summary(state, db_results, row_count, truncated)
        if answer is None:
            answer = _deterministic_summary(db_results, row_count, truncated)
            source = "deterministic"
        else:
            source = "llm"
            # The model is told to mention truncation; make sure the user is always told.
            if truncated and "truncat" not in answer.lower():
                answer = f"{answer}\n\n{_truncation_note(row_count)}"
        logger.info("formatter_node: success path, %d rows, %s summary", len(db_results), source)
        return {"final_answer": answer}

    # --- PATH B: graceful degradation ---
    if error_log is not None or sql_history:
        message = _failure_message(error_log, sql_history, generated_sql)
        logger.info("formatter_node: failure path, error_type=%s attempts=%d",
                    error_log.get("error_type") if isinstance(error_log, dict) else None,
                    len(sql_history))
        return {"final_answer": message}

    # --- PATH C: conversational / clarification, no SQL involved ---
    existing = state.get("final_answer")
    logger.info("formatter_node: conversational path")
    return {"final_answer": existing or "I cannot answer that with the available database."}
