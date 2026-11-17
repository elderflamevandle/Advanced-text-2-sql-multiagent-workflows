"""SQL safety scanner: AST-level read-only validation with audit logging.

The scanner parses SQL with sqlglot and allows exactly one read-only query. It fails
closed: SQL that cannot be parsed is reported as unsafe.

Exports:
    scan_sql(sql, dialect=None) -> dict  — verdict for a SQL string
    audit_blocked_query(sql, reason, user_query) -> None  — log blocked queries
"""
from __future__ import annotations

import fnmatch
import logging
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp
from sqlglot.tokens import TokenType

from database.dialects import to_sqlglot_dialect

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Built-in defaults (used when a key is missing from safety_config.yaml)
# ---------------------------------------------------------------------------

_DEFAULT_ALLOWED_STATEMENTS = ["SELECT", "WITH"]

_DEFAULT_BLOCKED_FUNCTIONS = [
    # postgres
    "pg_sleep*", "pg_read_*", "pg_ls_*", "pg_stat_file", "pg_terminate_backend",
    "pg_cancel_backend", "pg_reload_conf", "pg_advisory_*", "pg_try_advisory_*",
    "lo_*", "dblink*", "set_config", "setval", "nextval", "query_to_xml*",
    # mysql
    "load_file", "sleep", "benchmark", "get_lock", "release_lock",
    # sqlite
    "load_extension", "readfile", "writefile",
    # duckdb
    "read_*", "*_scan", "glob", "parquet_*", "getenv", "query", "query_table",
]

_DEFAULT_FILE_EXTENSIONS = [
    "csv", "tsv", "txt", "parquet", "json", "jsonl", "ndjson", "xlsx", "db", "duckdb",
    "sqlite", "gz", "zst", "arrow",
]

# ---------------------------------------------------------------------------
# Config loading (lazy, cached at module level)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_config() -> dict:
    """Load safety_config.yaml once and cache it."""
    config_path = Path(__file__).parent.parent / "config" / "safety_config.yaml"
    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _safety_cfg() -> dict:
    return _load_config().get("safety") or {}


def _get_allowed_statements() -> list[str]:
    allowed = _safety_cfg().get("allowed_statements", _DEFAULT_ALLOWED_STATEMENTS)
    return [str(s).upper() for s in allowed]


def _get_blocked_function_patterns() -> list[str]:
    """Return the function denylist as lower-case glob patterns (all dialect groups merged)."""
    raw = _safety_cfg().get("blocked_functions")
    if raw is None:
        raw = _DEFAULT_BLOCKED_FUNCTIONS
    if isinstance(raw, dict):
        merged: list = []
        for group in raw.values():
            merged.extend(group or [])
        raw = merged
    return sorted({str(p).strip().lower() for p in raw if str(p).strip()})


def _flag(name: str, default: bool = True) -> bool:
    return bool(_safety_cfg().get(name, default))


# ---------------------------------------------------------------------------
# AST classification
# ---------------------------------------------------------------------------


def _exp_types(*names: str) -> tuple[type, ...]:
    """Resolve sqlglot expression classes by name, skipping ones this version lacks."""
    return tuple(t for t in (getattr(exp, n, None) for n in names) if isinstance(t, type))


# Node types that must never appear anywhere inside an allowed query.
_FORBIDDEN_NODES: tuple[type, ...] = _exp_types(
    "DML", "DDL", "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
    "AlterTable", "TruncateTable", "Command", "Into", "Copy", "Pragma", "Attach", "Detach",
    "Set", "Grant", "Revoke", "Use", "Commit", "Rollback", "Transaction", "Kill", "Analyze",
    "Comment", "Describe", "Show", "Summarize", "Install", "LoadData", "Export", "Refresh",
    "Cache", "Uncache", "Declare", "Execute", "Return", "Put", "Get",
)
_LOCK_NODES: tuple[type, ...] = _exp_types("Lock")
_SET_OPERATIONS: tuple[type, ...] = _exp_types("SetOperation", "Union", "Intersect", "Except")
_WRAPPERS: tuple[type, ...] = _exp_types("Subquery", "Paren")
_SEMICOLON: tuple[type, ...] = _exp_types("Semicolon")

_TYPE_NAMES = {"TruncateTable": "TRUNCATE", "AlterTable": "ALTER", "LoadData": "LOAD"}

_EXECUTABLE_COMMENT = re.compile(r"^\s*M?!")
_FIRST_WORD = re.compile(r"[A-Za-z_]+")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _verdict(safe: bool, statement_type: str, reason: str | None = None) -> dict:
    return {"safe": safe, "statement_type": statement_type, "reason": reason}


def _node_type_name(node: exp.Expression) -> str:
    """Human-readable statement type for a blocked node, e.g. DROP / DELETE / EXPLAIN."""
    if isinstance(node, exp.Command):
        match = _FIRST_WORD.search(str(node.this or ""))
        return match.group(0).upper() if match else "COMMAND"
    cls = type(node).__name__
    return _TYPE_NAMES.get(cls, cls.upper())


def _first_keyword(tokens: list) -> str | None:
    for tok in tokens:
        if tok.token_type in (TokenType.L_PAREN, TokenType.SEMICOLON):
            continue
        return str(tok.text).upper()
    return None


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, _WRAPPERS) and isinstance(node.this, exp.Expression):
        node = node.this
    return node


def _matches(name: str, patterns: list[str]) -> bool:
    name = name.lower()
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _blocked_function(root: exp.Expression, tokens: list, patterns: list[str]) -> str | None:
    """Return the name of the first denylisted function called in the query, if any."""
    if not patterns:
        return None
    # 1. AST: covers typed (READ_CSV) and anonymous (pg_sleep) functions.
    for func in root.find_all(exp.Func):
        if isinstance(func, exp.Anonymous):
            names = [func.name]
        else:
            names = list(type(func).sql_names())
        for name in names:
            if name and _matches(name, patterns):
                return name.lower()
    # 2. Tokens: any name directly followed by "(" — independent of how sqlglot types it.
    for tok, nxt in zip(tokens, tokens[1:]):
        if nxt.token_type is not TokenType.L_PAREN:
            continue
        if tok.token_type in (TokenType.STRING, TokenType.NUMBER):
            continue
        text = str(tok.text)
        if text and _matches(text, patterns):
            return text.lower()
    return None


def _file_table(root: exp.Expression) -> str | None:
    """Return a table name that looks like a file path / URL / glob (DuckDB file scans)."""
    extensions = _safety_cfg().get("file_table_extensions", _DEFAULT_FILE_EXTENSIONS) or []
    ext_re = re.compile(
        r"\.(?:" + "|".join(re.escape(str(e)) for e in extensions) + r")$", re.IGNORECASE
    ) if extensions else None
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier) or table.args.get("db"):
            continue
        name = table.name
        if not name:
            continue
        if any(ch in name for ch in ("/", "\\", "*", "?")) or (ext_re and ext_re.search(name)):
            return name
    return None


def _executable_comment(root: exp.Expression) -> bool:
    for node in root.walk():
        for comment in node.comments or []:
            if _EXECUTABLE_COMMENT.match(comment):
                return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scan_sql(sql: str | None, dialect: str | None = None) -> dict:
    """Scan a SQL string and return a safety verdict.

    The SQL is safe only if it parses as exactly one read-only query: a SELECT,
    a WITH-wrapped SELECT, a set operation (UNION / INTERSECT / EXCEPT) or a
    parenthesised subquery, containing no data-modifying, DDL, locking, session or
    command node anywhere in its tree and no denylisted function call.

    Args:
        sql: The SQL string to scan. May be None or empty.
        dialect: The project's db_type ("sqlite", "duckdb", "postgresql", "mysql", ...).
            Unknown or missing values fall back to sqlglot's generic dialect.

    Returns:
        dict with keys:
            - safe (bool): True if the statement is allowed.
            - statement_type (str): "SELECT" / "WITH" for allowed queries; otherwise the
              offending type, e.g. "DROP", "DELETE", "MULTIPLE", "INTO", "LOCK",
              "FUNCTION", "FILE_ACCESS", "UNPARSEABLE", "EMPTY".
            - reason (str | None): Human-readable reason if not safe.
    """
    if not isinstance(sql, str) or not sql.strip():
        return _verdict(False, "EMPTY", "No SQL provided")

    glot_dialect = to_sqlglot_dialect(dialect)

    # --- Parse (fail closed) ---
    try:
        tokens = sqlglot.tokenize(sql, read=glot_dialect)
        parsed = sqlglot.parse(sql, read=glot_dialect)
    except Exception as exc:  # noqa: BLE001 - any parser failure means "not safe"
        detail = " ".join(_ANSI_ESCAPE.sub("", str(exc)).split())[:200]
        return _verdict(
            False,
            "UNPARSEABLE",
            f"SQL could not be parsed as {glot_dialect or 'generic'} SQL, so it cannot be "
            f"verified as read-only: {detail}",
        )

    statements = [s for s in parsed if s is not None and not isinstance(s, _SEMICOLON)]
    if not statements:
        return _verdict(False, "EMPTY", "No SQL statement found (only comments or whitespace)")
    if len(statements) > 1:
        return _verdict(
            False,
            "MULTIPLE",
            f"Multiple statements are not allowed ({len(statements)} found). "
            "Submit exactly one SELECT query.",
        )

    allowed = _get_allowed_statements()
    allowed_text = ", ".join(allowed)
    root = statements[0]
    query = _unwrap(root)

    # --- Root must be a read-only query ---
    if not isinstance(query, (exp.Select, *_SET_OPERATIONS)):
        if isinstance(root, _FORBIDDEN_NODES):
            stype = _node_type_name(root)
        else:
            stype = _first_keyword(tokens) or type(root).__name__.upper()
        return _verdict(
            False,
            stype,
            f"Statement type {stype} is not allowed. "
            f"Only {allowed_text} statements are permitted.",
        )

    has_with = any(isinstance(n, exp.With) for n in root.walk())
    statement_type = "WITH" if query.args.get("with_") or query.args.get("with") else "SELECT"

    if "SELECT" not in allowed:
        return _verdict(False, statement_type, "SELECT statements are disabled by configuration.")
    if has_with and "WITH" not in allowed:
        return _verdict(False, "WITH", "WITH (CTE) statements are disabled by configuration.")

    # --- Walk the whole tree ---
    block_locks = _flag("block_locking_reads")
    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            stype = _node_type_name(node)
            if stype == "INTO":
                reason = (
                    "SELECT ... INTO is not allowed because it writes to a table, "
                    "variable or file."
                )
            else:
                reason = (
                    f"{stype} is not allowed inside a query. "
                    f"Only read-only {allowed_text} statements are permitted."
                )
            return _verdict(False, stype, reason)
        if block_locks and isinstance(node, _LOCK_NODES):
            return _verdict(
                False,
                "LOCK",
                "Locking reads (FOR UPDATE / FOR SHARE / LOCK IN SHARE MODE) are not allowed.",
            )
    if block_locks and any(t.token_type is TokenType.FOR for t in tokens) and re.search(
        r"\bFOR\s+(NO\s+KEY\s+UPDATE|KEY\s+SHARE|UPDATE|SHARE)\b",
        " ".join(str(t.text) for t in tokens if t.token_type is not TokenType.STRING),
        re.IGNORECASE,
    ):
        return _verdict(
            False,
            "LOCK",
            "Locking reads (FOR UPDATE / FOR SHARE / LOCK IN SHARE MODE) are not allowed.",
        )

    if _flag("block_executable_comments") and _executable_comment(root):
        return _verdict(
            False,
            "EXECUTABLE_COMMENT",
            "MySQL executable comments (/*! ... */) are not allowed.",
        )

    func = _blocked_function(root, tokens, _get_blocked_function_patterns())
    if func:
        return _verdict(False, "FUNCTION", f"Function {func}() is not allowed in queries.")

    if _flag("block_file_tables") and glot_dialect in (None, "duckdb"):
        file_name = _file_table(root)
        if file_name:
            return _verdict(
                False,
                "FILE_ACCESS",
                f"Reading files or URLs as tables is not allowed: {file_name!r}.",
            )

    return _verdict(True, statement_type, None)


def audit_blocked_query(sql: str, reason: str, user_query: str) -> None:
    """Log a blocked SQL query with structured context.

    Args:
        sql: The blocked SQL string.
        reason: Why the query was blocked.
        user_query: The original user question that generated this SQL.
    """
    timestamp = datetime.now(tz=timezone.utc).isoformat()
    logger.warning(
        "Blocked SQL query | sql=%r | reason=%r | user_query=%r | timestamp=%s",
        sql,
        reason,
        user_query,
        timestamp,
    )
