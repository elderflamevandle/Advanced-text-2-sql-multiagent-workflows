"""Row-limit handling of the executor: sqlglot-based LIMIT injection,
truncation flag, config lookup and scanner signature tolerance."""
import asyncio
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
import sqlglot

from agents.nodes.executor import _get_max_rows, _inject_limit, _scan, executor_node
from database.dialects import to_sqlglot_dialect
from database.manager import DatabaseManager
from tests.agents.conftest import make_agent_state

DIALECTS = ["sqlite", "duckdb", "postgresql", "mysql"]
SAFE = {"safe": True, "statement_type": "SELECT", "reason": None}


def _outer_limit(sql: str, db_type: str) -> int | None:
    root = sqlglot.parse_one(sql, read=to_sqlglot_dialect(db_type))
    node = root.args.get("limit")
    return int(node.expression.this) if node is not None else None


def _run_node(sql, manager, db_type="sqlite", max_rows=1000):
    state = make_agent_state()
    state["generated_sql"] = sql
    state["db_type"] = db_type
    with (
        patch("database.manager.get_active_manager", return_value=manager),
        patch("database.safety.scan_sql", return_value=SAFE),
        patch("agents.nodes.executor._get_max_rows", return_value=max_rows),
        patch("agents.nodes.executor._get_timeout", return_value=10),
    ):
        return asyncio.run(executor_node(state))


# ---------------------------------------------------------------------------
# _inject_limit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("db_type", DIALECTS)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t",
        "SELECT a FROM (SELECT a FROM t LIMIT 5) AS x",
        "SELECT a FROM t WHERE a IN (SELECT b FROM u ORDER BY b LIMIT 3)",
        "WITH c AS (SELECT a FROM t LIMIT 2) SELECT a FROM c",
        "SELECT a FROM t UNION SELECT a FROM u",
        "SELECT a FROM t UNION ALL SELECT a FROM u ORDER BY a",
        "SELECT 'limit 5' AS s FROM t",
        "SELECT a AS \"limit 9\" FROM t WHERE note = 'please LIMIT 10 rows'",
        "SELECT a FROM t;",
        "SELECT a FROM t ;  \n",
        "SELECT a FROM t -- trailing comment",
        "SELECT a FROM t; -- limit 3",
        "select a from t /* LIMIT 1 */",
    ],
)
def test_limit_added_to_outermost_query(sql, db_type):
    result = _inject_limit(sql, 1000, db_type)
    assert _outer_limit(result, db_type) == 1000
    assert len(sqlglot.parse(result, read=to_sqlglot_dialect(db_type))) == 1
    assert not result.rstrip().endswith(";")


@pytest.mark.parametrize("db_type", DIALECTS)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t LIMIT 5000",
        "SELECT a FROM t ORDER BY a DESC LIMIT 1001",
        "SELECT a FROM t LIMIT 5000 OFFSET 20",
        "WITH c AS (SELECT a FROM t LIMIT 2) SELECT a FROM c LIMIT 99999",
        "SELECT a FROM t UNION ALL SELECT a FROM u LIMIT 5000",
        "SELECT a FROM (SELECT a FROM t LIMIT 5) AS x LIMIT 2000;",
    ],
)
def test_larger_limit_is_capped(sql, db_type):
    result = _inject_limit(sql, 1000, db_type)
    assert _outer_limit(result, db_type) == 1000
    if "OFFSET" in sql:
        assert "OFFSET 20" in result
    if "LIMIT 5)" in sql or "LIMIT 2)" in sql:  # inner limits are untouched
        assert "LIMIT 5)" in result or "LIMIT 2)" in result


@pytest.mark.parametrize("db_type", DIALECTS)
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t LIMIT 5",
        "SELECT a FROM t LIMIT 1000",
        "SELECT a FROM t ORDER BY a LIMIT 10 OFFSET 30",
        "WITH c AS (SELECT a FROM t) SELECT a FROM c LIMIT 7",
        "SELECT a FROM t UNION SELECT a FROM u LIMIT 50",
    ],
)
def test_smaller_limit_is_kept_verbatim(sql, db_type):
    assert _inject_limit(sql, 1000, db_type) == sql


def test_mysql_offset_comma_form_is_capped():
    result = _inject_limit("SELECT a FROM t LIMIT 10, 5000", 1000, "mysql")
    assert _outer_limit(result, "mysql") == 1000 and "10" in result


def test_postgres_fetch_first_and_limit_all():
    capped = _inject_limit("SELECT a FROM t FETCH FIRST 5000 ROWS ONLY", 1000, "postgresql")
    assert "1000" in capped and "5000" not in capped
    kept = "SELECT a FROM t FETCH FIRST 5 ROWS ONLY"
    assert _inject_limit(kept, 1000, "postgresql") == kept
    assert _outer_limit(_inject_limit("SELECT a FROM t LIMIT ALL", 1000, "postgresql"),
                        "postgresql") == 1000


def test_non_literal_limit_and_non_queries_are_left_alone():
    assert _inject_limit("SELECT a FROM t LIMIT $1", 1000, "postgresql") == \
        "SELECT a FROM t LIMIT $1"
    assert _inject_limit("PRAGMA table_info('t')", 1000, "sqlite") == "PRAGMA table_info('t')"


def test_unparseable_sql_is_wrapped_not_appended():
    sql = "SELECT a FROM t WHERE ((( LIMIT 5"
    result = _inject_limit(sql, 1000, "sqlite")
    assert result.startswith("SELECT * FROM (") and result.endswith("LIMIT 1000")
    assert sql in result


def test_unknown_dialect_uses_generic_parser():
    assert _outer_limit(_inject_limit("SELECT a FROM t", 10, "unknown"), "unknown") == 10
    assert _outer_limit(_inject_limit("SELECT a FROM t", 10, None), None) == 10


def test_old_regex_bugs_end_to_end(tmp_path):
    """Subquery LIMIT and a 'limit' literal no longer switch the outer limit off."""
    path = tmp_path / "n.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE n (i INTEGER)")
    conn.executemany("INSERT INTO n VALUES (?)", [(i,) for i in range(40)])
    conn.commit()
    conn.close()
    mgr = DatabaseManager(db_type="sqlite", db_path=str(path))

    for sql in (
        "SELECT a.i FROM n a, (SELECT i FROM n LIMIT 5) b",        # 200 rows
        "SELECT a.i, 'limit 5' AS s FROM n a, n b",                 # 1600 rows
        "WITH c AS (SELECT i FROM n LIMIT 30) SELECT * FROM c UNION ALL SELECT i FROM n",
    ):
        result = _run_node(sql, mgr, max_rows=25)
        assert result["error_log"] is None, result["error_log"]
        assert len(result["db_results"]) == 25
        assert result["execution_metadata"]["row_count"] == 25
        assert result["execution_metadata"]["truncated"] is True
        assert type(result["db_results"]) is list

    exact = _run_node("SELECT i FROM n LIMIT 25", mgr, max_rows=25)
    assert exact["execution_metadata"] == {
        "execution_time_ms": exact["execution_metadata"]["execution_time_ms"],
        "row_count": 25,
        "truncated": False,
    }
    small = _run_node("SELECT i FROM n", mgr, max_rows=100)
    assert small["execution_metadata"]["row_count"] == 40
    assert small["execution_metadata"]["truncated"] is False


def test_node_truncates_when_manager_returns_plain_list():
    mgr = MagicMock()
    mgr.execute_query.return_value = [{"i": i} for i in range(8)]
    result = _run_node("SELECT i FROM n", mgr, max_rows=5)
    assert len(result["db_results"]) == 5
    assert result["execution_metadata"]["truncated"] is True
    args, kwargs = mgr.execute_query.call_args
    assert args[0].endswith("LIMIT 6") and kwargs == {"timeout": 10, "max_rows": 5}


# ---------------------------------------------------------------------------
# Config + scanner signature
# ---------------------------------------------------------------------------

def test_max_rows_read_from_safety_config():
    assert _get_max_rows() == 1000  # config/safety_config.yaml execution.max_rows
    with patch("agents.nodes.executor.yaml.safe_load", return_value={"execution": {}}):
        assert _get_max_rows() == 1000
    custom = {"execution": {"max_rows": 50}}
    with patch("agents.nodes.executor.yaml.safe_load", return_value=custom):
        assert _get_max_rows() == 50
    with patch("builtins.open", side_effect=OSError):
        assert _get_max_rows() == 1000


def test_scan_passes_dialect_and_tolerates_old_signature():
    new_style = MagicMock(return_value=SAFE)
    with patch("database.safety.scan_sql", new_style):
        assert _scan("SELECT 1", "duckdb") == SAFE
    new_style.assert_called_once_with("SELECT 1", dialect="duckdb")

    calls = []

    def old_style(sql):
        calls.append(sql)
        return SAFE

    with patch("database.safety.scan_sql", old_style):
        assert _scan("SELECT 1", "duckdb") == SAFE
    assert calls == ["SELECT 1"]
