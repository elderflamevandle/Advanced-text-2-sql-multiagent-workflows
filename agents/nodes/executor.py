"""Executor node: safety scan, LIMIT injection, timeout, structured errors.

Exports:
    executor_node(state) -> dict — async LangGraph node that executes SQL safely.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any

import yaml

from graph.state import AgentState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_main_config() -> dict:
    """Load config/config.yaml (not cached — tests may patch)."""
    config_path = Path(__file__).parent.parent.parent / "config" / "config.yaml"
    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _get_timeout() -> float:
    """Return the configured query timeout in seconds."""
    try:
        cfg = _load_main_config()
        return float(cfg.get("database", {}).get("query_timeout", 60))
    except Exception:
        return 60.0


# ---------------------------------------------------------------------------
# LIMIT injection helper
# ---------------------------------------------------------------------------

# Matches a top-level LIMIT clause — case-insensitive, preceded by digit/whitespace
_LIMIT_PATTERN = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)


def _inject_limit(sql: str, max_rows: int = 1000) -> str:
    """Append LIMIT max_rows to sql if it has no LIMIT clause at the outermost level.

    Strategy: check for the presence of any LIMIT token (case-insensitive).
    This is simple and avoids complex AST parsing — good enough for the safety
    layer since we only allow SELECT and WITH.
    """
    if _LIMIT_PATTERN.search(sql):
        return sql  # already has a LIMIT
    return f"{sql.rstrip().rstrip(';')}\nLIMIT {max_rows}"


# ---------------------------------------------------------------------------
# Error classification helper
# ---------------------------------------------------------------------------

def _classify_error(exc: Exception) -> str:
    """Map an exception type to an error_type string."""
    exc_type = type(exc).__name__.lower()
    if "operational" in exc_type:
        return "syntax_error"
    if "programming" in exc_type:
        return "column_not_found"
    if "integrity" in exc_type:
        return "constraint_violation"
    if "interface" in exc_type or "notsupp" in exc_type:
        return "not_supported"
    return "unknown"


def _generate_hint(exc: Exception) -> str:
    """Generate a user-friendly hint based on the exception."""
    msg = str(exc).lower()
    if "no such table" in msg or "does not exist" in msg or "table not found" in msg:
        return "Check that the table name exists and is spelled correctly."
    if "no such column" in msg or "column" in msg and "not found" in msg:
        return "Check that the column name exists in the specified table."
    if "syntax" in msg:
        return "Check the SQL syntax, especially around keywords and punctuation."
    return "Review the SQL statement for errors."


# ---------------------------------------------------------------------------
# Structured error builder
# ---------------------------------------------------------------------------

def _build_error(
    error_type: str,
    message: str,
    dialect: str,
    failed_sql: str | None = None,
    hint: str = "",
) -> dict:
    return {
        "error_type": error_type,
        "message": message,
        "dialect": dialect,
        "failed_sql": failed_sql,
        "hint": hint,
    }


# ---------------------------------------------------------------------------
# Executor node
# ---------------------------------------------------------------------------

async def executor_node(state: AgentState) -> dict:
    """Execute SQL safely via DatabaseManager and capture results.

    Performs:
    1. Null checks (missing sql, missing db_manager)
    2. Safety scan via database.safety.scan_sql
    3. LIMIT injection (appends LIMIT 1000 if absent)
    4. Timed execution via ThreadPoolExecutor
    5. Returns structured results or error dicts

    Returns dict with keys:
        db_results: list[dict] | None
        error_log: dict | None
        execution_metadata: dict | None
    """
    logger.info("executor_node called for query: %s", state.get("user_query", ""))

    sql: str | None = state.get("generated_sql")
    db_manager = state.get("db_manager")
    db_type: str = state.get("db_type", "unknown")

    # ------------------------------------------------------------------
    # 1. Null checks
    # ------------------------------------------------------------------
    if not sql or not sql.strip():
        logger.warning("executor_node: no generated_sql in state")
        return {
            "db_results": None,
            "error_log": _build_error(
                error_type="missing_sql",
                message=f"{db_type.upper()} error: No SQL provided for execution.",
                dialect=db_type,
                failed_sql=sql,
                hint="Ensure the sql_generator_node has run and produced a valid SQL statement.",
            ),
            "execution_metadata": None,
        }

    if db_manager is None:
        logger.warning("executor_node: db_manager is None")
        return {
            "db_results": None,
            "error_log": _build_error(
                error_type="no_connection",
                message=f"{db_type.upper()} error: No database connection available.",
                dialect=db_type,
                failed_sql=sql,
                hint="Ensure a DatabaseManager instance is set on the state before execution.",
            ),
            "execution_metadata": None,
        }

    # ------------------------------------------------------------------
    # 2. Safety scan
    # ------------------------------------------------------------------
    from database.safety import scan_sql, audit_blocked_query  # noqa: PLC0415

    scan_result = scan_sql(sql)
    if not scan_result["safe"]:
        reason = scan_result.get("reason", "Blocked statement type")
        audit_blocked_query(sql=sql, reason=reason, user_query=state.get("user_query", ""))
        logger.warning("executor_node: blocked SQL statement type=%s", scan_result["statement_type"])
        return {
            "db_results": None,
            "error_log": _build_error(
                error_type="blocked_query",
                message=(
                    f"{db_type.upper()} error: SQL statement type "
                    f"{scan_result['statement_type']} is not allowed. "
                    "Only SELECT and WITH (CTE) statements may be executed."
                ),
                dialect=db_type,
                failed_sql=sql,
                hint="Rephrase as a SELECT query.",
            ),
            "execution_metadata": None,
        }

    # ------------------------------------------------------------------
    # 3. LIMIT injection
    # ------------------------------------------------------------------
    sql_to_run = _inject_limit(sql)

    # ------------------------------------------------------------------
    # 4. Timed execution
    # ------------------------------------------------------------------
    timeout_seconds = _get_timeout()
    start_mono = time.monotonic()

    def _run_query() -> list[dict]:
        return db_manager.execute_query(sql_to_run)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run_query)
            rows: list[dict] = future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        elapsed_ms = int((time.monotonic() - start_mono) * 1000)
        logger.error(
            "executor_node: query timed out after %.1fs — sql=%r",
            timeout_seconds,
            sql_to_run[:200],
        )
        return {
            "db_results": None,
            "error_log": _build_error(
                error_type="timeout",
                message=(
                    f"{db_type.upper()} error: Query exceeded {timeout_seconds:.0f}s timeout."
                ),
                dialect=db_type,
                failed_sql=sql_to_run,
                hint="Try adding LIMIT or simplifying the query.",
            ),
            "execution_metadata": None,
        }
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - start_mono) * 1000)
        error_type = _classify_error(exc)
        hint = _generate_hint(exc)
        logger.error(
            "executor_node: DB error %s — %s — sql=%r",
            error_type,
            exc,
            sql_to_run[:200],
        )
        return {
            "db_results": None,
            "error_log": _build_error(
                error_type=error_type,
                message=f"{db_type.upper()} error: {exc}",
                dialect=db_type,
                failed_sql=sql_to_run,
                hint=hint,
            ),
            "execution_metadata": None,
        }

    # ------------------------------------------------------------------
    # 5. Success
    # ------------------------------------------------------------------
    elapsed_ms = int((time.monotonic() - start_mono) * 1000)
    row_count = len(rows)
    logger.info(
        "executor_node: executed successfully — %d rows in %dms",
        row_count,
        elapsed_ms,
    )

    return {
        "db_results": rows,
        "error_log": None,
        "execution_metadata": {
            "execution_time_ms": elapsed_ms,
            "row_count": row_count,
        },
    }
