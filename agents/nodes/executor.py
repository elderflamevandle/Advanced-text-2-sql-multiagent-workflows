"""Executor node: safety scan, row limit, cancellable timeout, structured errors.

Exports:
    executor_node(state) -> dict — async LangGraph node that executes SQL safely.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp

from database.dialects import to_sqlglot_dialect
from graph.state import AgentState

logger = logging.getLogger(__name__)

DEFAULT_MAX_ROWS = 1000

# Shared worker pool for blocking database calls, so the event loop stays free.
# Kept below the PostgreSQL pool size (10) so workers can always get a connection.
_QUERY_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="sql-exec")

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


def _get_max_rows() -> int:
    """Return execution.max_rows from config/safety_config.yaml (default 1000)."""
    try:
        config_path = Path(__file__).parent.parent.parent / "config" / "safety_config.yaml"
        with open(config_path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        value = int(cfg.get("execution", {}).get("max_rows", DEFAULT_MAX_ROWS))
        return value if value > 0 else DEFAULT_MAX_ROWS
    except Exception:
        return DEFAULT_MAX_ROWS


# ---------------------------------------------------------------------------
# LIMIT injection helper
# ---------------------------------------------------------------------------

def _parse_single(sql: str, dialect: str | None) -> exp.Expression | None:
    """Parse ``sql`` and return its root node, or None unless it is exactly one statement."""
    try:
        statements = [
            node
            for node in sqlglot.parse(sql, read=dialect)
            if node is not None and not isinstance(node, exp.Semicolon)
        ]
    except Exception:
        return None
    return statements[0] if len(statements) == 1 else None


def _literal_limit(root: exp.Expression) -> int | None:
    """Row count of the outermost LIMIT / FETCH FIRST if it is a plain integer literal."""
    node = root.args.get("limit")
    if node is None:
        return None
    if node.args.get("limit_options") and node.args["limit_options"].args.get("percent"):
        return None
    count = node.args.get("count") if isinstance(node, exp.Fetch) else node.expression
    if isinstance(count, exp.Literal) and not count.is_string:
        try:
            return int(count.this)
        except (TypeError, ValueError):
            return None
    return None


def _wrap_with_limit(sql: str, max_rows: int) -> str:
    """Last resort: bound the query from the outside without touching its text."""
    body = sql.strip().rstrip(";").rstrip()
    return f"SELECT * FROM (\n{body}\n) AS _limited LIMIT {max_rows}"


def _inject_limit(sql: str, max_rows: int = DEFAULT_MAX_ROWS, dialect: str | None = None) -> str:
    """Make sure the OUTERMOST query returns at most ``max_rows`` rows.

    ``dialect`` is the project's db_type ("sqlite", "duckdb", "postgresql", "mysql").
    The statement is parsed with sqlglot, so a LIMIT inside a subquery or CTE,
    or the word "limit" inside a string literal, is not mistaken for an outer limit.

    * no outer limit            -> ``LIMIT max_rows`` is added
    * literal limit > max_rows  -> lowered to ``max_rows``
    * literal limit <= max_rows -> SQL returned unchanged
    * ``LIMIT ALL``             -> replaced by ``LIMIT max_rows``
    * non-literal limit, or a statement that is not a query -> unchanged
      (the connector still stops reading after max_rows rows)
    * SQL that sqlglot cannot parse -> wrapped in ``SELECT * FROM (...) LIMIT max_rows``

    The original text is preserved whenever possible; it is only re-rendered
    by sqlglot when an existing limit has to be lowered.
    """
    sg_dialect = to_sqlglot_dialect(dialect)
    root = _parse_single(sql, sg_dialect)
    if root is None:
        return _wrap_with_limit(sql, max_rows)
    if not isinstance(root, exp.Query):
        return sql

    limit_node = root.args.get("limit")
    has_limit = limit_node is not None
    current = _literal_limit(root)
    limit_all = (
        isinstance(limit_node, exp.Limit)
        and isinstance(limit_node.expression, exp.Var)
        and limit_node.expression.name.upper() == "ALL"
    )
    if has_limit and not limit_all and (current is None or current <= max_rows):
        return sql

    if not has_limit:
        # Cheapest change: append to the original text, then verify by re-parsing.
        appended = f"{sql.rstrip().rstrip(';').rstrip()}\nLIMIT {max_rows}"
        check = _parse_single(appended, sg_dialect)
        if (
            check is not None
            and isinstance(check, exp.Query)
            and _literal_limit(check) == max_rows
        ):
            return appended

    # Lower an existing limit (or add one where appending text was not valid).
    try:
        if isinstance(root.args.get("limit"), exp.Fetch):
            root.args["limit"].set("count", exp.Literal.number(max_rows))
            rewritten = root.sql(dialect=sg_dialect)
        else:
            rewritten = root.limit(max_rows, copy=True).sql(dialect=sg_dialect)
        check = _parse_single(rewritten, sg_dialect)
        if check is not None and _literal_limit(check) == max_rows:
            return rewritten
    except Exception:  # noqa: BLE001
        logger.debug("executor: could not rewrite LIMIT via sqlglot", exc_info=True)
    return _wrap_with_limit(sql, max_rows)


def _scan(sql: str, db_type: str) -> dict:
    """Run the safety scanner, passing the dialect when the scanner accepts it."""
    from database.safety import scan_sql  # noqa: PLC0415

    try:
        return scan_sql(sql, dialect=db_type)
    except TypeError:
        # Older scan_sql(sql) signature without a dialect parameter.
        return scan_sql(sql)


# ---------------------------------------------------------------------------
# Error classification helper
# ---------------------------------------------------------------------------

def _classify_error(exc: Exception) -> str:
    """Map an exception type to an error_type string."""
    exc_type = type(exc).__name__.lower()
    if "timeout" in exc_type or "querycanceled" in exc_type:
        return "timeout"
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

def _resolve_tenant_manager(tenant_id: str | None, connection_id: str):
    """Return the tenant's DatabaseManager, or None when it cannot be resolved.

    A missing connection and one owned by another tenant are indistinguishable
    (both ``ConnectionNotFound``), and every failure maps to the same
    ``no_connection`` error so nothing about other tenants leaks to the caller.
    """
    from database.registry import ConnectionNotFound, get_registry  # noqa: PLC0415

    try:
        return get_registry().get_manager(tenant_id, connection_id)
    except ConnectionNotFound:
        logger.warning("executor_node: connection not found for this tenant")
    except Exception as exc:  # noqa: BLE001 - never surface credential/driver details
        logger.error("executor_node: could not open tenant connection (%s)",
                     type(exc).__name__)
    return None


async def executor_node(state: AgentState) -> dict:
    """Execute SQL safely via DatabaseManager and capture results.

    Performs:
    1. Null checks (missing sql, missing db_manager)
    2. Safety scan via database.safety.scan_sql
    3. Row limit on the outermost query (execution.max_rows, default 1000)
    4. Execution in a worker thread under asyncio.wait_for; on timeout the
       query is cancelled in the database and the node returns immediately
    5. Returns structured results or error dicts

    Returns dict with keys:
        db_results: list[dict] | None
        error_log: dict | None
        execution_metadata: dict | None
            {"execution_time_ms", "row_count", "truncated"}; ``truncated`` is
            True when the query had more than max_rows rows.
    """
    logger.info("executor_node called for query: %s", state.get("user_query", ""))

    sql: str | None = state.get("generated_sql")
    db_type: str = state.get("db_type", "unknown")
    connection_id = state.get("connection_id")
    if connection_id:
        # Service path: the tenant's own database, resolved through the registry.
        db_manager = await asyncio.to_thread(
            _resolve_tenant_manager, state.get("tenant_id"), connection_id
        )
    else:
        # Legacy single-user path (Streamlit sidebar).
        from database.manager import get_active_manager
        db_manager = get_active_manager()

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
    from database.safety import audit_blocked_query  # noqa: PLC0415

    scan_result = _scan(sql, db_type)
    if not scan_result["safe"]:
        reason = scan_result.get("reason") or "Blocked statement type"
        audit_blocked_query(sql=sql, reason=reason, user_query=state.get("user_query", ""))
        logger.warning(
            "executor_node: blocked SQL statement type=%s", scan_result["statement_type"]
        )
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
    # 3. Row limit. One row more than max_rows is requested so that a
    #    truncated result can be told apart from one that fits exactly.
    # ------------------------------------------------------------------
    max_rows = _get_max_rows()
    sql_to_run = _inject_limit(sql, max_rows + 1, db_type)

    # ------------------------------------------------------------------
    # 4. Timed execution off the event loop
    # ------------------------------------------------------------------
    timeout_seconds = _get_timeout()
    start_mono = time.monotonic()

    def _timeout_result() -> dict:
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
                    f"{db_type.upper()} error: Query exceeded {timeout_seconds:g}s timeout."
                ),
                dialect=db_type,
                failed_sql=sql_to_run,
                hint="Try adding LIMIT or simplifying the query.",
            ),
            "execution_metadata": None,
        }

    # DatabaseManager.execute_query cancels the statement in the database when
    # `timeout` elapses; wait_for makes the node return even if a driver
    # cannot be cancelled.
    call = functools.partial(
        db_manager.execute_query, sql_to_run, timeout=timeout_seconds, max_rows=max_rows
    )
    try:
        loop = asyncio.get_running_loop()
        rows = await asyncio.wait_for(
            loop.run_in_executor(_QUERY_EXECUTOR, call), timeout=timeout_seconds
        )
    except (asyncio.TimeoutError, TimeoutError):
        return _timeout_result()
    except Exception as exc:  # noqa: BLE001
        error_type = _classify_error(exc)
        if error_type == "timeout":
            return _timeout_result()
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
    truncated = bool(getattr(rows, "truncated", False))
    rows = list(rows)  # plain list for LangGraph state / checkpointing
    if len(rows) > max_rows:  # manager without max_rows support
        rows = rows[:max_rows]
        truncated = True
    row_count = len(rows)
    logger.info(
        "executor_node: executed successfully — %d rows in %dms (truncated=%s)",
        row_count,
        elapsed_ms,
        truncated,
    )

    return {
        "db_results": rows,
        "error_log": None,
        "execution_metadata": {
            "execution_time_ms": elapsed_ms,
            "row_count": row_count,
            "truncated": truncated,
        },
    }
