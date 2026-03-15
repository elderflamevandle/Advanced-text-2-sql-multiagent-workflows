"""SQL safety scanner: statement-level validation with audit logging.

Exports:
    scan_sql(sql) -> dict  — classify SQL and check against allowed list
    audit_blocked_query(sql, reason, user_query) -> None  — log blocked queries
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config loading (lazy, cached at module level)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_config() -> dict:
    """Load safety_config.yaml once and cache it."""
    config_path = Path(__file__).parent.parent / "config" / "safety_config.yaml"
    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _get_allowed_statements() -> list[str]:
    cfg = _load_config()
    return [s.upper() for s in cfg.get("safety", {}).get("allowed_statements", ["SELECT", "WITH"])]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Matches single-quoted strings: '...' (handles escaped quotes via non-greedy)
_SINGLE_QUOTED = re.compile(r"'(?:[^'\\]|\\.)*'")
# Matches double-quoted identifiers: "..."  — not a string literal, but strip to avoid
# spurious keyword matches inside column aliases
_DOUBLE_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')
# Matches backtick-quoted identifiers (MySQL)
_BACKTICK_QUOTED = re.compile(r'`(?:[^`\\]|\\.)*`')
# Matches -- line comments
_LINE_COMMENT = re.compile(r"--[^\n]*")
# Matches /* */ block comments
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_literals_and_comments(sql: str) -> str:
    """Remove string literals, quoted identifiers, and SQL comments.

    This prevents keywords that appear inside string values or identifier names
    from triggering false positives.
    """
    # Remove block comments first (may span lines)
    sql = _BLOCK_COMMENT.sub(" ", sql)
    # Remove line comments
    sql = _LINE_COMMENT.sub(" ", sql)
    # Remove string literals (single-quoted)
    sql = _SINGLE_QUOTED.sub(" ", sql)
    # Remove double-quoted identifiers
    sql = _DOUBLE_QUOTED.sub(" ", sql)
    # Remove backtick-quoted identifiers
    sql = _BACKTICK_QUOTED.sub(" ", sql)
    return sql


def _extract_first_keyword(sql: str) -> str | None:
    """Extract the first SQL keyword token from pre-processed SQL.

    Returns the keyword in UPPER CASE, or None if no token found.
    """
    # Split on whitespace and punctuation; take the first non-empty token
    tokens = re.split(r"[\s;,()\[\]]+", sql.strip())
    for token in tokens:
        token = token.strip()
        if token:
            return token.upper()
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_sql(sql: str | None) -> dict:
    """Scan a SQL string and return a safety verdict.

    Args:
        sql: The SQL string to scan. May be None or empty.

    Returns:
        dict with keys:
            - safe (bool): True if the statement is allowed.
            - statement_type (str): The detected statement type (e.g. "SELECT").
            - reason (str | None): Human-readable reason if not safe.
    """
    # --- Edge case: empty / None ---
    if not sql or not sql.strip():
        return {
            "safe": False,
            "statement_type": "EMPTY",
            "reason": "No SQL provided",
        }

    # Strip literals and comments to avoid false positives
    cleaned = _strip_literals_and_comments(sql)

    keyword = _extract_first_keyword(cleaned)

    if keyword is None:
        return {
            "safe": False,
            "statement_type": "EMPTY",
            "reason": "No SQL statement found after stripping literals and comments",
        }

    allowed = _get_allowed_statements()

    if keyword in allowed:
        return {
            "safe": True,
            "statement_type": keyword,
            "reason": None,
        }

    return {
        "safe": False,
        "statement_type": keyword,
        "reason": (
            f"Statement type {keyword} is not allowed. "
            f"Only {', '.join(allowed)} statements are permitted."
        ),
    }


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
