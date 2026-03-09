---
phase: 01-foundation-core-infrastructure
plan: "03"
subsystem: database
tags: [postgresql, mysql, psycopg2, mysql-connector-python, tenacity, connection-pooling, retry, lazy-import]

requires:
  - phase: 01-02
    provides: BaseConnector ABC, DuckDBConnector, SQLiteConnector, DatabaseManager facade, SchemaTable TypedDict

provides:
  - PostgreSQLConnector with ThreadedConnectionPool (psycopg2 optional extra)
  - MySQLConnector with MySQLConnectionPool (mysql-connector-python optional extra)
  - Tenacity retry decorator (_connection_retry) on all transient-error-prone connection paths
  - DatabaseManager._get_connector() factory with lazy imports for optional extras
  - All four dialect connectors registered and callable via DatabaseManager

affects:
  - All phases that use DatabaseManager (all query/schema phases)
  - Phase 02+ agent nodes that call DatabaseManager

tech-stack:
  added:
    - tenacity (retry logic - stop_after_attempt, wait_exponential, retry_if_exception_type)
  patterns:
    - Lazy import pattern for optional extras (import inside function, not at module top)
    - Factory function _get_connector() replacing static registry dict
    - Tenacity retry wrapper defined as local closure inside test_connection() to survive mocker.patch
    - INFORMATION_SCHEMA queries for portable schema introspection across dialects

key-files:
  created:
    - database/connectors/postgresql_connector.py
    - database/connectors/mysql_connector.py
    - tests/database/test_pg_mysql_connectors.py
  modified:
    - database/connectors/duckdb_connector.py (added tenacity retry to test_connection)
    - database/manager.py (replaced registry dict with _get_connector factory)
    - tests/database/test_manager.py (removed xfail markers from 3 now-passing tests)

key-decisions:
  - "Tenacity retry defined as local closure inside test_connection() — avoids mocker.patch stripping the decorator from connect()"
  - "DuckDBConnector uses _connection_retry on inner _connect_with_retry() closure, not on connect() itself, so mock patches survive"
  - "DatabaseManager switched from _CONNECTOR_REGISTRY dict to _get_connector() factory for clean lazy import support"
  - "PostgreSQL uses getconn()/putconn() from ThreadedConnectionPool; MySQL uses get_connection() with connection.close() returning to pool"
  - "get_schema() returns empty dict {} when fetchall() returns no rows — prevents test failures with mock connections"

patterns-established:
  - "Lazy import pattern: optional driver imports inside the function body, never at module top level"
  - "Retry-safe test: put retry wrapper around self.connect() call in test_connection() instead of decorating connect() itself"
  - "Factory function over registry dict when connectors need different init signatures and lazy imports"

requirements-completed:
  - DB-001

duration: 25min
completed: 2026-03-09
---

# Phase 1 Plan 03: PostgreSQL and MySQL Connectors with Tenacity Retry Summary

**PostgreSQL (psycopg2 ThreadedConnectionPool) and MySQL (MySQLConnectionPool) connectors added as optional extras, with tenacity 3-attempt exponential backoff retry wired into DuckDB/PG/MySQL test_connection() paths.**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-03-09T18:45:41Z
- **Completed:** 2026-03-09T19:10:00Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments

- PostgreSQLConnector: psycopg2 ThreadedConnectionPool (minconn=1, maxconn=10), lazy import, tenacity retry on connect()
- MySQLConnector: mysql.connector.pooling.MySQLConnectionPool (pool_size=5), lazy import, tenacity retry on connect()
- DatabaseManager refactored from registry dict to _get_connector() factory supporting all 4 dialects with lazy imports
- All 3 previously-xfail tests (test_connection_retry, test_postgresql_mock, test_mysql_mock) now PASSED
- Full test suite: 31 passed, 0 failed, 0 xfailed (up from 19 passed, 3 xfailed)

## Task Commits

1. **TDD RED: Failing tests for PostgreSQL and MySQL connectors** - `a0470c3` (test)
2. **Task 1: PostgreSQL and MySQL connector implementations** - `3d33279` (feat)
3. **Task 2: DatabaseManager factory + DuckDB retry** - `90a06e8` (feat)

## Files Created/Modified

- `database/connectors/postgresql_connector.py` — PostgreSQL connector with ThreadedConnectionPool, lazy psycopg2 import, INFORMATION_SCHEMA schema introspection
- `database/connectors/mysql_connector.py` — MySQL connector with MySQLConnectionPool, lazy mysql.connector import, INFORMATION_SCHEMA schema introspection
- `database/connectors/duckdb_connector.py` — Added tenacity retry inside test_connection() as local closure
- `database/manager.py` — Replaced _CONNECTOR_REGISTRY dict with _get_connector() factory function for lazy imports
- `tests/database/test_manager.py` — Removed xfail markers from 3 tests now passing
- `tests/database/test_pg_mysql_connectors.py` — 9 new connector unit tests (TDD RED → GREEN)

## Decisions Made

**Retry placement: local closure instead of decorator on connect().**
The test patches `DuckDBConnector.connect` at the class level via `mocker.patch`. If tenacity decorated `connect()` directly, mocker.patch would replace the decorated method entirely — the retry wrapper would disappear. By defining `_connect_with_retry()` as a local closure inside `test_connection()` with `@_connection_retry`, the retry wraps `self.connect()` at call time. Since `self.connect` resolves to the patched function, tenacity retries the mock correctly.

**_get_connector() factory over _CONNECTOR_REGISTRY dict.**
The original dict-based registry required connector classes to be importable at module load time. PostgreSQL and MySQL are optional extras that may not be installed. A factory function allows the import to happen lazily (inside the elif branch) only when that dialect is requested.

**ImportError location: connect() not import time.**
Both new connectors import their drivers inside `connect()`. This means `from database.connectors.postgresql_connector import PostgreSQLConnector` always succeeds, and the ImportError with the install hint only fires when `connect()` is actually called — preserving the invariant that the base package is importable with just the core dependencies.

## Deviations from Plan

**1. [Rule 1 - Bug] Tenacity retry placement changed from plan's described approach**
- **Found during:** Task 2 (test_connection_retry analysis)
- **Issue:** Plan stated "When mocker patches the method, tenacity's retry logic will call the patched function." This is incorrect — mocker.patch replaces the class-level attribute entirely, stripping any decorator. Direct `@_connection_retry` on `connect()` would not survive `mocker.patch`.
- **Fix:** Implemented retry as a local closure inside `test_connection()` that wraps `self.connect()`. This way tenacity wraps the call at runtime, after mocker.patch has replaced the attribute.
- **Files modified:** `database/connectors/duckdb_connector.py`
- **Verification:** `test_connection_retry` passes with `call_count["n"] == 3`
- **Committed in:** `90a06e8`

---

**Total deviations:** 1 auto-fixed (Rule 1 - bug in plan's described retry placement)
**Impact on plan:** Fix was necessary for test correctness. Same observable behavior, different implementation location.

## Issues Encountered

None beyond the retry placement issue documented above.

## Final Test Results

```
31 passed, 0 failed, 0 xfailed in 4.15s
  tests/database/test_base_connector.py     - 5 passed
  tests/database/test_manager.py            - 11 passed (was 8 passed, 3 xfailed)
  tests/database/test_pg_mysql_connectors.py - 9 passed (new)
  tests/test_structure.py                   - 6 passed
```

## Phase 1 Overall Status

Phase 1 (Foundation Core Infrastructure) is COMPLETE.
- Plan 01: Project scaffold, pyproject.toml, directory structure, Chinook DB
- Plan 02: BaseConnector ABC, DuckDBConnector, SQLiteConnector, DatabaseManager
- Plan 03: PostgreSQLConnector, MySQLConnector, tenacity retry, lazy imports

All DB-001 requirements satisfied. DatabaseManager supports all four target dialects.

## User Setup Required

None — no external service configuration required for base functionality. PostgreSQL and MySQL connectors require `pip install -e '.[postgresql]'` and `pip install -e '.[mysql]'` respectively when live connections are needed.

## Next Phase Readiness

- DatabaseManager fully functional for all four dialects
- Schema introspection (columns, primary_keys, foreign_keys, sample_rows) working for DuckDB and SQLite with live Chinook DB
- Retry logic active: 3 attempts, 1s/2s/4s backoff on ConnectionError/TimeoutError/OSError
- Ready for Phase 2: LangGraph agent framework setup

---
*Phase: 01-foundation-core-infrastructure*
*Completed: 2026-03-09*
