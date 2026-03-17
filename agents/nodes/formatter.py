"""Formatter agent node — converts database results to natural language, with graceful degradation."""
import logging

from graph.state import AgentState

logger = logging.getLogger(__name__)


async def formatter_node(state: AgentState) -> dict:
    """Convert database results to natural language answer, or format correction failure."""
    db_results = state.get("db_results")
    error_log = state.get("error_log")
    sql_history = state.get("sql_history") or []
    generated_sql = state.get("generated_sql", "") or ""

    # --- PATH A: Success path — db_results is present ---
    if db_results is not None:
        row_count = len(db_results)
        if row_count == 0:
            answer = "The query returned no results."
        elif row_count == 1:
            answer = f"Result: {db_results[0]}"
        else:
            answer = f"Found {row_count} results. First row: {db_results[0]}"
        logger.info("formatter_node: success path — %d rows", row_count)
        return {"final_answer": answer}

    # --- PATH B: Graceful degradation — correction loop exhausted ---
    if error_log is not None or sql_history:
        attempts = len(sql_history)
        error_msg = (
            error_log["message"]
            if isinstance(error_log, dict)
            else str(error_log or "unknown error")
        )
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
        message = "\n".join(lines)
        logger.info("formatter_node: graceful degradation — %d attempt(s) exhausted", attempts)
        return {"final_answer": message}

    # --- PATH C: Conversational / rejection path — no SQL involved ---
    existing = state.get("final_answer")
    logger.info("formatter_node: conversational/rejection path")
    return {"final_answer": existing or "I cannot answer that with the available database."}
