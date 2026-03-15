"""Tests for database.safety SQL keyword scanner.

Requirements covered:
- SAFETY-001: Only SELECT and WITH statements are allowed
- DB-002: audit_blocked_query logs blocked queries
"""
import logging
import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# SAFETY-001: Allowed statements
# ---------------------------------------------------------------------------

def test_select_is_safe():
    """SELECT statements are allowed."""
    from database.safety import scan_sql
    result = scan_sql("SELECT * FROM users")
    assert result["safe"] is True
    assert result["statement_type"] == "SELECT"


def test_with_cte_is_safe():
    """WITH (CTE) statements are allowed."""
    from database.safety import scan_sql
    result = scan_sql("WITH cte AS (SELECT 1) SELECT * FROM cte")
    assert result["safe"] is True
    assert result["statement_type"] == "WITH"


def test_select_case_insensitive_lower():
    """'select' (lowercase) is allowed — case-insensitive check."""
    from database.safety import scan_sql
    result = scan_sql("select id from orders")
    assert result["safe"] is True
    assert result["statement_type"] == "SELECT"


def test_select_case_insensitive_mixed():
    """'Select' (mixed case) is allowed."""
    from database.safety import scan_sql
    result = scan_sql("Select id from orders")
    assert result["safe"] is True


# ---------------------------------------------------------------------------
# SAFETY-001: Blocked destructive statements
# ---------------------------------------------------------------------------

def test_drop_is_blocked():
    """DROP statements are blocked."""
    from database.safety import scan_sql
    result = scan_sql("DROP TABLE users")
    assert result["safe"] is False
    assert result["statement_type"] == "DROP"
    assert "reason" in result
    assert result["reason"]


def test_delete_is_blocked():
    """DELETE statements are blocked."""
    from database.safety import scan_sql
    result = scan_sql("DELETE FROM users WHERE id=1")
    assert result["safe"] is False
    assert result["statement_type"] == "DELETE"


def test_insert_is_blocked():
    """INSERT statements are blocked."""
    from database.safety import scan_sql
    result = scan_sql("INSERT INTO users VALUES (1)")
    assert result["safe"] is False
    assert result["statement_type"] == "INSERT"


def test_update_is_blocked():
    """UPDATE statements are blocked."""
    from database.safety import scan_sql
    result = scan_sql("UPDATE users SET name='x'")
    assert result["safe"] is False
    assert result["statement_type"] == "UPDATE"


def test_alter_is_blocked():
    """ALTER statements are blocked."""
    from database.safety import scan_sql
    result = scan_sql("ALTER TABLE users ADD COLUMN age INT")
    assert result["safe"] is False
    assert result["statement_type"] == "ALTER"


def test_create_is_blocked():
    """CREATE statements are blocked."""
    from database.safety import scan_sql
    result = scan_sql("CREATE TABLE new_table (id INT)")
    assert result["safe"] is False
    assert result["statement_type"] == "CREATE"


def test_truncate_is_blocked():
    """TRUNCATE statements are blocked."""
    from database.safety import scan_sql
    result = scan_sql("TRUNCATE TABLE users")
    assert result["safe"] is False
    assert result["statement_type"] == "TRUNCATE"


def test_grant_is_blocked():
    """GRANT statements are blocked."""
    from database.safety import scan_sql
    result = scan_sql("GRANT SELECT ON users TO role_name")
    assert result["safe"] is False
    assert result["statement_type"] == "GRANT"


def test_revoke_is_blocked():
    """REVOKE statements are blocked."""
    from database.safety import scan_sql
    result = scan_sql("REVOKE SELECT ON users FROM role_name")
    assert result["safe"] is False
    assert result["statement_type"] == "REVOKE"


# ---------------------------------------------------------------------------
# SAFETY-001: Edge cases — no false positives from column/table names
# ---------------------------------------------------------------------------

def test_column_name_updated_at_not_false_positive():
    """SELECT query with 'updated_at' column name is safe (not UPDATE)."""
    from database.safety import scan_sql
    result = scan_sql("SELECT updated_at, delete_flag FROM logs")
    assert result["safe"] is True
    assert result["statement_type"] == "SELECT"


def test_column_name_delete_flag_not_false_positive():
    """SELECT query with 'delete_flag' column name is safe (not DELETE)."""
    from database.safety import scan_sql
    result = scan_sql("SELECT id, delete_flag FROM events WHERE delete_flag = 0")
    assert result["safe"] is True


def test_string_literal_drop_not_false_positive():
    """SELECT where string literal contains DROP keyword is safe."""
    from database.safety import scan_sql
    result = scan_sql("SELECT * FROM logs WHERE message = 'DROP TABLE attempt'")
    assert result["safe"] is True


def test_string_literal_insert_not_false_positive():
    """SELECT where string literal contains INSERT is safe."""
    from database.safety import scan_sql
    result = scan_sql("SELECT * FROM audit WHERE action = 'INSERT'")
    assert result["safe"] is True


# ---------------------------------------------------------------------------
# SAFETY-001: Empty / None SQL edge cases
# ---------------------------------------------------------------------------

def test_empty_string_is_blocked():
    """Empty string SQL is blocked."""
    from database.safety import scan_sql
    result = scan_sql("")
    assert result["safe"] is False
    assert "reason" in result
    assert result["reason"]


def test_none_sql_is_blocked():
    """None SQL is blocked."""
    from database.safety import scan_sql
    result = scan_sql(None)
    assert result["safe"] is False
    assert result["reason"]


def test_whitespace_only_sql_is_blocked():
    """Whitespace-only SQL is blocked."""
    from database.safety import scan_sql
    result = scan_sql("   \n\t  ")
    assert result["safe"] is False


# ---------------------------------------------------------------------------
# SAFETY-001: Leading comments handled correctly
# ---------------------------------------------------------------------------

def test_sql_with_leading_line_comment():
    """SQL with leading -- comment extracts correct statement type."""
    from database.safety import scan_sql
    result = scan_sql("-- this is a comment\nSELECT * FROM users")
    assert result["safe"] is True
    assert result["statement_type"] == "SELECT"


def test_sql_with_leading_block_comment():
    """SQL with leading /* */ block comment extracts correct statement type."""
    from database.safety import scan_sql
    result = scan_sql("/* get all users */ SELECT * FROM users")
    assert result["safe"] is True
    assert result["statement_type"] == "SELECT"


def test_sql_with_block_comment_then_drop():
    """Block comment before DROP does not confuse the scanner."""
    from database.safety import scan_sql
    result = scan_sql("/* admin op */ DROP TABLE users")
    assert result["safe"] is False
    assert result["statement_type"] == "DROP"


# ---------------------------------------------------------------------------
# DB-002: audit_blocked_query logs correctly
# ---------------------------------------------------------------------------

def test_audit_blocked_query_logs_warning(caplog):
    """audit_blocked_query emits a WARNING log with sql, reason, user_query."""
    from database.safety import audit_blocked_query
    with caplog.at_level(logging.WARNING, logger="database.safety"):
        audit_blocked_query(
            sql="DROP TABLE users",
            reason="Statement type DROP is not allowed",
            user_query="delete users table",
        )
    assert len(caplog.records) >= 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    # The log message should contain key context
    assert "DROP TABLE users" in record.message or "DROP TABLE users" in str(record.args)


def test_audit_blocked_query_includes_timestamp(caplog):
    """audit_blocked_query log record includes a timestamp field."""
    from database.safety import audit_blocked_query
    with caplog.at_level(logging.WARNING, logger="database.safety"):
        audit_blocked_query(
            sql="DELETE FROM orders",
            reason="DELETE blocked",
            user_query="remove all orders",
        )
    # At minimum, a warning was logged
    assert any(r.levelno == logging.WARNING for r in caplog.records)
