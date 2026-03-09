# Phase 1: Foundation & Core Infrastructure - Research

**Researched:** 2026-03-09
**Domain:** Python project scaffolding, multi-dialect database connectivity, schema introspection, connection pooling
**Confidence:** HIGH

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**1. Project Structure Organization**
- Separate top-level directories (no `src/` wrapper)
- Nested `database/connectors/` for dialect-specific connectors
- Mixed file naming: short names for classes (`manager.py`), descriptive for utilities (`schema_utils.py`)
- Configuration files live in `/config` directory
- Structure:
  ```
  text-2-sql-agentic-pipeline/
  ├── agents/
  ├── database/
  │   ├── connectors/
  │   ├── manager.py
  │   └── schema_utils.py
  ├── graph/
  ├── llm/
  ├── vector/
  ├── evaluation/
  ├── memory/
  ├── utils/
  ├── streamlit_app/
  ├── tests/
  ├── config/
  │   ├── config.yaml
  │   └── error-taxonomy.json
  ├── data/
  │   └── chinook.db
  ├── .env.example
  ├── pyproject.toml
  └── README.md
  ```

**2. Dependency Management**
- Use `pyproject.toml` with PEP 621 format
- Compatible ranges (e.g., `langchain>=0.2.0,<0.3.0`) — not pinned to exact versions
- Dev/test dependencies in `[project.optional-dependencies]` dev group
- MySQL and PostgreSQL drivers as optional extras
- Python minimum: 3.10+
- Installation patterns: `pip install -e .` / `pip install -e ".[dev]"` / `pip install -e ".[dev,mysql,postgresql]"`

**3. DatabaseManager Design**
- Global singleton connection pool per database type
- Lazy initialization on first use, thread-safe with `threading.Lock()`
- Schema cached in-memory on first extraction, no TTL — manual `refresh_schema()` only
- Schema includes: tables, columns, types, PKs, FKs, 2 sample rows per table
- Error handling: exponential backoff retry (3 attempts: 1s, 2s, 4s) for transient errors
- Retry only on: ConnectionError, TimeoutError, OperationalError
- Immediate fail (no retry) for: auth errors, permission errors, syntax errors

**4. Configuration Management**
- Flat `.env` file with prefixed keys (e.g., `DB_TYPE`, `GROQ_API_KEY`)
- Sensible defaults: DuckDB + chinook.db when nothing specified
- Best-effort startup with visible warnings (not hard failures)
- Warning display: Python logger + Streamlit sidebar orange badge (later phases)
- `.env` in `.gitignore`, `.env.example` committed to git
- No cloud secrets for Phase 1 (deferred)

### Claude's Discretion
None documented — all gray areas resolved in CONTEXT.md.

### Deferred Ideas (OUT OF SCOPE)
- Cloud secrets management (AWS Secrets Manager, Azure Key Vault)
- Advanced connection pooling (per-query limits, pool monitoring)
- Configuration UI (web-based settings panel)
- Multi-environment configs (dev/staging/prod separation)
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| INFRA-001 | Python virtual environment with all required dependencies (`requirements.txt` / `pyproject.toml`) | pyproject.toml PEP 621 patterns, verified current library versions, optional dependency groups |
| INFRA-002 | Clean modular project structure with directories, config files, sample DB, `.env.example` | Directory scaffolding patterns, config.yaml skeleton, error-taxonomy.json skeleton |
| DB-001 | DatabaseManager supporting DuckDB, MySQL, PostgreSQL, SQLite with connection pooling, schema introspection, 2-5 sample rows | Per-dialect introspection queries, thread-safe pooling patterns, DuckDB cursor-per-thread requirement |
</phase_requirements>

---

## Summary

Phase 1 establishes the Python project scaffolding and multi-dialect database layer from a greenfield state. The work is primarily organizational (directory structure, pyproject.toml, .env.example) plus one substantive implementation: the `DatabaseManager` class with dialect-aware connection pooling and schema introspection.

The most important technical finding is that **DuckDB's threading model is fundamentally different from other databases**: a single DuckDB file-based database can only have one writer process, and each Python thread must obtain its own cursor via `.cursor()` on the shared parent connection rather than sharing a connection object directly. This means the "global singleton pool" pattern for DuckDB must be a single persistent connection with per-thread cursor dispatch, not a pool of independent connections.

For MySQL and PostgreSQL, `psycopg2.pool.ThreadedConnectionPool` and `mysql.connector.pooling.MySQLConnectionPool` provide battle-tested thread-safe pools. SQLite uses Python's built-in `sqlite3` module with `check_same_thread=False` and explicit locking, since SQLite similarly restricts multi-process writes.

**Primary recommendation:** Implement `DatabaseManager` as a per-dialect factory that delegates to dialect-specific connector classes in `database/connectors/`. Use `tenacity` for retry logic (cleaner than manual loops). Use `python-dotenv` for `.env` loading. Fetch the Chinook SQLite file directly from the official GitHub release.

---

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| duckdb | >=1.1.0,<2.0.0 | DuckDB in-process OLAP database | Latest stable is 1.5.0 (March 2026); 1.1+ required for stable INFORMATION_SCHEMA support |
| psycopg2-binary | >=2.9.0,<3.0.0 | PostgreSQL driver | Industry standard, thread-safe pool built-in, binary wheel avoids libpq compile dependency |
| mysql-connector-python | >=9.0.0,<10.0.0 | MySQL driver | Official Oracle driver, has built-in connection pooling via `MySQLConnectionPool`, latest 9.6.0 |
| python-dotenv | >=1.0.0,<2.0.0 | Load `.env` file into `os.environ` | De-facto standard, latest 1.2.2 (March 2026) |
| tenacity | >=9.0.0,<10.0.0 | Retry with exponential backoff | Cleaner than manual loops, supports `retry_if_exception_type`, latest 9.1.4 |
| pyyaml | >=6.0.0,<7.0.0 | Load `config/config.yaml` | Standard YAML library for Python config files |

### Supporting (Dev / Test)
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pytest | >=9.0.0,<10.0.0 | Test framework | All unit and integration tests |
| pytest-mock | >=3.14.0,<4.0.0 | Mock fixtures for pytest | Mocking DB connections in unit tests without live databases |
| ruff | >=0.8.0,<1.0.0 | Fast linter + formatter | Replace black+flake8 in one tool; enforces code quality |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| psycopg2-binary | psycopg3 (psycopg) | psycopg3 is the new major version but psycopg2 is more widely deployed; psycopg2-binary avoids compile step |
| mysql-connector-python | PyMySQL | PyMySQL is pure Python (slower) but no C extension; mysql-connector-python is official Oracle driver |
| tenacity | manual retry loops | tenacity is composable, testable, and has built-in jitter; hand-rolled loops get error-prone quickly |
| ruff | black + flake8 + isort | ruff replaces all three at 10-100x speed, now the community standard |

**Installation:**
```bash
# Basic install (runtime only)
pip install -e .

# With dev tools
pip install -e ".[dev]"

# With MySQL support
pip install -e ".[mysql]"

# With PostgreSQL support
pip install -e ".[postgresql]"

# Full development setup
pip install -e ".[dev,mysql,postgresql]"
```

---

## Architecture Patterns

### Recommended Project Structure
```
text-2-sql-agentic-pipeline/
├── agents/                     # LangGraph agent nodes (Phase 2+)
├── database/
│   ├── __init__.py
│   ├── manager.py              # DatabaseManager class (main entry point)
│   ├── schema_utils.py         # Schema extraction helpers
│   └── connectors/
│       ├── __init__.py
│       ├── base.py             # BaseConnector abstract class
│       ├── duckdb_connector.py
│       ├── sqlite_connector.py
│       ├── postgresql_connector.py
│       └── mysql_connector.py
├── graph/                      # LangGraph state/graph (Phase 2+)
├── llm/                        # LLM provider clients (Phase 2+)
├── vector/                     # Pinecone integration (Phase 2+)
├── evaluation/                 # Ragas integration (Phase 3+)
├── memory/                     # Session management (Phase 2+)
├── utils/                      # Shared utilities
│   └── __init__.py
├── streamlit_app/              # Streamlit UI (Phase 3+)
├── tests/
│   ├── conftest.py             # Shared fixtures
│   └── database/
│       ├── __init__.py
│       └── test_manager.py
├── config/
│   ├── config.yaml
│   └── error-taxonomy.json
├── data/
│   └── chinook.db              # Chinook SQLite sample database (~1MB)
├── .env                        # Local secrets (git-ignored)
├── .env.example                # Template committed to git
├── .gitignore
├── pyproject.toml
└── README.md
```

### Pattern 1: Connector Abstraction (BaseConnector)
**What:** Abstract base class defines the contract; each dialect implements it.
**When to use:** Every time a new database type is added; ensures the DatabaseManager never needs dialect-specific code.
**Example:**
```python
# database/connectors/base.py
from abc import ABC, abstractmethod
from typing import Any

class BaseConnector(ABC):
    """Abstract interface all dialect connectors must implement."""

    @abstractmethod
    def connect(self) -> Any:
        """Return a live connection object."""

    @abstractmethod
    def get_schema(self, connection: Any) -> dict:
        """Return schema dict: {table: {columns, pks, fks, sample_rows}}."""

    @abstractmethod
    def execute_query(self, connection: Any, sql: str) -> list[dict]:
        """Execute a read-only SQL query, return list of row dicts."""

    @abstractmethod
    def close(self, connection: Any) -> None:
        """Release the connection back to pool or close it."""
```

### Pattern 2: Global Singleton Pool per Dialect
**What:** One `ConnectionPoolManager` owns a dict of pools keyed by db_type. Lazy init on first use. Thread-safe via `threading.Lock`.
**When to use:** All `DatabaseManager.get_connection()` calls route through here.
**Example:**
```python
# database/manager.py
import threading
from database.connectors.base import BaseConnector

class ConnectionPoolManager:
    _pools: dict = {}
    _lock = threading.Lock()

    @classmethod
    def get_pool(cls, db_type: str, connector: BaseConnector):
        if db_type not in cls._pools:
            with cls._lock:
                # Double-checked locking
                if db_type not in cls._pools:
                    cls._pools[db_type] = connector.create_pool()
        return cls._pools[db_type]
```

### Pattern 3: DuckDB Thread-Safe Cursor Dispatch
**What:** DuckDB uses a single parent connection; each thread calls `.cursor()` to get an isolated cursor.
**When to use:** Any time multiple threads access the same DuckDB file.

**CRITICAL LIMITATION:** DuckDB file-based databases allow only ONE writer process. Within a single Python process, use cursor copies per thread:
```python
# database/connectors/duckdb_connector.py
import duckdb
import threading

class DuckDBConnector(BaseConnector):
    _connection = None
    _local = threading.local()

    def connect(self, db_path: str) -> duckdb.DuckDBPyConnection:
        if self._connection is None:
            self._connection = duckdb.connect(db_path)
        # Each thread gets its own cursor
        if not hasattr(self._local, 'cursor'):
            self._local.cursor = self._connection.cursor()
        return self._local.cursor
```

### Pattern 4: Schema Introspection Per Dialect
**What:** Each connector fetches schema via dialect-appropriate SQL.

**DuckDB / PostgreSQL (INFORMATION_SCHEMA — standard SQL):**
```sql
-- Tables
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'main' AND table_type = 'BASE TABLE';

-- Columns
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = :table_name;

-- Primary Keys
SELECT kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
  ON tc.constraint_name = kcu.constraint_name
WHERE tc.table_name = :table_name AND tc.constraint_type = 'PRIMARY KEY';

-- Foreign Keys
SELECT kcu.column_name, ccu.table_name AS referenced_table,
       ccu.column_name AS referenced_column
FROM information_schema.referential_constraints rc
JOIN information_schema.key_column_usage kcu
  ON rc.constraint_name = kcu.constraint_name
JOIN information_schema.constraint_column_usage ccu
  ON rc.unique_constraint_name = ccu.constraint_name
WHERE kcu.table_name = :table_name;
```

**SQLite (PRAGMA — no INFORMATION_SCHEMA for FKs):**
```sql
-- Tables
SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';

-- Columns + PKs (cid, name, type, notnull, dflt_value, pk)
PRAGMA table_info(:table_name);

-- Foreign Keys (id, seq, table, from, to, on_update, on_delete, match)
PRAGMA foreign_key_list(:table_name);
```

**MySQL (INFORMATION_SCHEMA — note: schema = DATABASE()):**
```sql
SELECT column_name, data_type, is_nullable, column_key
FROM information_schema.columns
WHERE table_schema = DATABASE() AND table_name = :table_name;

SELECT kcu.column_name, kcu.referenced_table_name, kcu.referenced_column_name
FROM information_schema.key_column_usage kcu
JOIN information_schema.table_constraints tc
  ON kcu.constraint_name = tc.constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND kcu.table_schema = DATABASE()
  AND kcu.table_name = :table_name;
```

### Pattern 5: Tenacity Retry Decorator
**What:** Wraps connection and query methods; retries only transient errors.
```python
# database/connectors/base.py
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type, before_sleep_log
)
import logging

logger = logging.getLogger(__name__)

RETRY_EXCEPTIONS = (ConnectionError, TimeoutError, OSError)

def connection_retry(func):
    """Decorator: 3 attempts, 1s→2s→4s backoff, transient errors only."""
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        retry=retry_if_exception_type(RETRY_EXCEPTIONS),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True
    )(func)
```

### Pattern 6: Schema Cache with Manual Invalidation
**What:** `DatabaseManager` holds a `_schema_cache` dict. Populated on first call to `get_schema()`. Cleared by `refresh_schema()`.
```python
class DatabaseManager:
    _schema_cache: dict | None = None

    def get_schema(self) -> dict:
        if self._schema_cache is None:
            self._schema_cache = self._connector.get_schema(self._get_connection())
        return self._schema_cache

    def refresh_schema(self) -> dict:
        self._schema_cache = None
        return self.get_schema()
```

### Anti-Patterns to Avoid
- **Sharing a DuckDB connection object directly across threads:** DuckDB connections are not thread-safe. Always use `.cursor()` per thread. Sharing the connection itself causes data corruption.
- **Using psycopg2.pool.SimpleConnectionPool in multi-threaded code:** Use `ThreadedConnectionPool` instead. `SimpleConnectionPool` has no thread protection.
- **Importing database drivers at module load time:** Import inside the connector class `__init__` or `connect()` to avoid `ImportError` when optional extras are not installed.
- **Hard-failing on missing optional config:** The project decision requires best-effort startup with warnings. Use `os.getenv("DB_TYPE", "duckdb")` defaults everywhere.
- **Querying `sqlite_sequence` or `sqlite_stat*` tables during schema extraction:** These are internal SQLite tables and should be filtered out.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Retry with backoff | Custom while-loop with time.sleep | `tenacity` | Handles jitter, per-exception filtering, logging, async variants, and composable stop conditions |
| Env file loading | Manual file parsing | `python-dotenv` | Handles quotes, comments, multi-line values, override semantics, `find_dotenv()` traversal |
| Thread-safe PostgreSQL pool | Custom `threading.Lock` + list of connections | `psycopg2.pool.ThreadedConnectionPool` | Tested, handles getconn/putconn lifecycle, minconn/maxconn, closeall |
| Thread-safe MySQL pool | Custom lock + connection list | `mysql.connector.pooling.MySQLConnectionPool` | Official Oracle implementation; handles pool_size, pool_reset_session |
| YAML config loading | Manual string parsing | `pyyaml` | Standard; handles anchors, aliases, multi-document files |

**Key insight:** The database pooling space has many subtle race conditions (double-acquire, connection leaks on exception, pool exhaustion deadlocks). Library solutions have years of production testing behind them.

---

## Common Pitfalls

### Pitfall 1: DuckDB "Only One Writer Process" Trap
**What goes wrong:** Two processes (e.g., a background schema refresh and the main app) both try to open the same `.duckdb` file in read-write mode. The second process hangs or raises `IOException: Could not set lock on file`.
**Why it happens:** DuckDB uses OS-level file locking to enforce single-writer access.
**How to avoid:** For Phase 1 (single process), this is not a concern. Open the file once in the main process. For any future multi-process use, open secondary processes with `read_only=True`.
**Warning signs:** `duckdb.IOException` containing "lock" in the message.

### Pitfall 2: SQLite Foreign Keys Off by Default
**What goes wrong:** Schema introspection via `PRAGMA foreign_key_list` returns the correct FK data, but FK constraints are NOT enforced at runtime because SQLite disables them by default.
**Why it happens:** SQLite maintains backward compatibility by defaulting `PRAGMA foreign_keys = OFF`.
**How to avoid:** In `SQLiteConnector.connect()`, always execute `PRAGMA foreign_keys = ON` immediately after opening the connection. Note: this must be done per connection.
**Warning signs:** `PRAGMA foreign_key_list(table)` returns data, but FK violations succeed silently.

### Pitfall 3: mysql-connector-python vs PyMySQL Confusion
**What goes wrong:** Code that works with `mysql-connector-python` fails with PyMySQL (or vice versa) because the connection parameter names differ (`host` vs `host`, but `db` vs `database`, `passwd` vs `password`).
**Why it happens:** Two competing MySQL drivers with similar but not identical APIs.
**How to avoid:** Use `mysql-connector-python` consistently. Never mix drivers. Document the choice in the connector's docstring.
**Warning signs:** `TypeError: __init__() got an unexpected keyword argument 'db'` or `'passwd'`.

### Pitfall 4: Optional Driver Import at Module Top Level
**What goes wrong:** If `psycopg2` is not installed (user didn't install `.[postgresql]` extra), importing `database/connectors/postgresql_connector.py` at the top of `database/__init__.py` raises `ModuleNotFoundError` immediately, breaking even DuckDB-only usage.
**Why it happens:** Python evaluates all `import` statements at module load time.
**How to avoid:** Import optional drivers inside the connector class method that uses them:
```python
def connect(self):
    try:
        import psycopg2
    except ImportError:
        raise ImportError(
            "PostgreSQL support requires: pip install -e '.[postgresql]'"
        )
```
**Warning signs:** `ModuleNotFoundError: No module named 'psycopg2'` on startup even when only DuckDB is configured.

### Pitfall 5: Schema Cache Poisoning after Reconnect
**What goes wrong:** After a connection failure and successful reconnect, the stale `_schema_cache` is returned without re-fetching. If the reconnect went to a different database (e.g., after config change), the schema is wrong.
**Why it happens:** Cache is populated once on first `get_schema()` call and never refreshed automatically.
**How to avoid:** Call `refresh_schema()` after any reconnect or `db_type` change. Document this behavior clearly in docstrings.
**Warning signs:** Schema returns tables from wrong database after config change.

### Pitfall 6: pyproject.toml Compatible Ranges Broken on Major Versions
**What goes wrong:** `langchain>=0.2.0,<0.3.0` worked when written but a newer install with `langchain==0.2.16` breaks because the LangChain 0.2.x → 0.3.x migration removed APIs.
**Why it happens:** Some projects use semver loosely; a "compatible range" may still span breaking changes.
**How to avoid:** For Phase 1, pin the major+minor range. Test installs in CI. Accept that ranges require periodic maintenance.
**Warning signs:** `ImportError: cannot import name 'X' from 'langchain'` after upgrading.

---

## Code Examples

Verified patterns from official sources:

### DuckDB Connection and Schema Query
```python
# Source: https://duckdb.org/docs/stable/clients/python/dbapi
import duckdb

con = duckdb.connect("data/chinook.db")

# List all user tables
tables = con.execute(
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = 'main' AND table_type = 'BASE TABLE'"
).fetchall()

# Fetch sample rows (2 per table)
for (table_name,) in tables:
    rows = con.execute(f"SELECT * FROM {table_name} LIMIT 2").fetchall()
```

### DuckDB Thread-Safe Pattern
```python
# Source: https://duckdb.org/docs/stable/guides/python/multiple_threads
import duckdb
import threading

parent_con = duckdb.connect("data/chinook.db")

def worker(shared_con):
    thread_con = shared_con.cursor()  # Thread-local cursor
    result = thread_con.execute("SELECT * FROM Artist LIMIT 5").fetchall()
    return result

threads = [threading.Thread(target=worker, args=(parent_con,)) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
```

### SQLite Schema Introspection via PRAGMA
```python
# Source: https://sqlite.org/pragma.html
import sqlite3

con = sqlite3.connect("data/chinook.db")
con.execute("PRAGMA foreign_keys = ON")

# Get tables
tables = con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
).fetchall()

# Get columns and PKs for a table
cols = con.execute("PRAGMA table_info(Artist)").fetchall()
# Returns: (cid, name, type, notnull, dflt_value, pk)

# Get foreign keys
fks = con.execute("PRAGMA foreign_key_list(Track)").fetchall()
# Returns: (id, seq, table, from, to, on_update, on_delete, match)
```

### psycopg2 Thread-Safe Connection Pool
```python
# Source: https://www.psycopg.org/docs/pool.html
import psycopg2.pool

pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    host="localhost",
    port=5432,
    database="mydb",
    user="readonly_user",
    password="..."
)

conn = pool.getconn()
try:
    cursor = conn.cursor()
    cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
    tables = cursor.fetchall()
finally:
    pool.putconn(conn)
```

### python-dotenv Load Pattern
```python
# Source: https://github.com/theskumar/python-dotenv
from dotenv import load_dotenv
import os

# Load .env, do not override already-set environment variables
load_dotenv(dotenv_path=".env", override=False)

DB_TYPE = os.getenv("DB_TYPE", "duckdb")
DB_PATH = os.getenv("DB_PATH", "data/chinook.db")
```

### Tenacity Retry for Database Connection
```python
# Source: https://tenacity.readthedocs.io/
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import logging

logger = logging.getLogger(__name__)

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
    reraise=True
)
def get_connection(connector):
    return connector.connect()
```

### pyproject.toml Structure
```toml
[project]
name = "text2sql-agentic-pipeline"
version = "1.0.0"
requires-python = ">=3.10"
dependencies = [
    "duckdb>=1.1.0,<2.0.0",
    "python-dotenv>=1.0.0,<2.0.0",
    "tenacity>=9.0.0,<10.0.0",
    "pyyaml>=6.0.0,<7.0.0",
    "langchain>=0.2.0,<0.3.0",
    "langgraph>=0.1.0,<0.2.0",
    "streamlit>=1.30.0,<2.0.0",
    "pinecone-client>=3.0.0,<4.0.0",
    "groq>=0.4.0,<1.0.0",
    "openai>=1.0.0,<2.0.0",
    "ragas>=0.1.0,<0.2.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=9.0.0,<10.0.0",
    "pytest-mock>=3.14.0,<4.0.0",
    "ruff>=0.8.0,<1.0.0",
]
mysql = ["mysql-connector-python>=9.0.0,<10.0.0"]
postgresql = ["psycopg2-binary>=2.9.0,<3.0.0"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends.legacy:build"

[tool.setuptools.packages.find]
where = ["."]
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `setup.py` + `requirements.txt` | `pyproject.toml` (PEP 621) | 2021-2023 | Single file for metadata + deps + build config |
| `black` + `flake8` + `isort` | `ruff` (replaces all three) | 2023-2024 | 10-100x faster; one config section in pyproject.toml |
| Manual retry loops | `tenacity` library | 2019+ | Composable, testable, supports async, has jitter |
| `requirements.txt` only | `pyproject.toml` + optional extras | 2022-present | Separates runtime vs dev vs dialect-specific deps |
| DuckDB 0.x (unstable API) | DuckDB 1.x LTS (stable API) | June 2024 (1.0.0 release) | 1.x guarantees API stability across minor versions |
| `pytest` 7.x | `pytest` 9.x | 2025 | New assertion introspection, dropped Python 3.9 |

**Deprecated/outdated:**
- `setup.py` + `setup.cfg`: Replaced by `pyproject.toml`; still works but new projects should not use it
- `flake8` + `black` as separate tools: `ruff` covers both with one config block in `pyproject.toml`
- `duckdb < 1.0`: Pre-1.0 API had frequent breaking changes; always use 1.x for new projects
- `psycopg2` (non-binary): Requires system `libpq-dev` to compile; `psycopg2-binary` bundles the shared library and avoids this build dependency for dev/prototyping use

---

## Open Questions

1. **LangGraph / LangChain version compatibility with Python 3.10+**
   - What we know: The CONTEXT.md specifies `langchain>=0.2.0,<0.3.0` and `langgraph>=0.1.0,<0.2.0`
   - What's unclear: LangChain releases frequently; 0.2.x may now be outdated relative to current 0.3.x. The pinned upper bound `<0.3.0` may prevent installing any current version if LangChain has already moved to 0.3+.
   - Recommendation: The planner should treat version ranges as starting points and verify current PyPI versions for langchain/langgraph at task execution time. The bounds in CONTEXT.md are illustrative of the pattern, not hard-pinned values.

2. **Chinook database format for DuckDB**
   - What we know: `data/chinook.db` should be the SQLite file (1.02 MB from lerocha/chinook-database GitHub). DuckDB can read SQLite files directly via the `sqlite` extension.
   - What's unclear: Whether the plan should store a native `.duckdb` file OR load the SQLite file into DuckDB at startup.
   - Recommendation: Store `chinook.db` as the SQLite file. For DuckDB testing, use `duckdb.connect(":memory:")` and `ATTACH 'data/chinook.db' (TYPE SQLITE)` to read it. This avoids maintaining two separate binary files.

3. **mysql-connector-python 9.x API changes from 8.x**
   - What we know: CONTEXT.md referenced `mysql-connector-python>=8.0.0,<9.0.0` but the latest is 9.6.0.
   - What's unclear: Whether 9.x has breaking API changes from 8.x (connection pool API, cursor behavior).
   - Recommendation: Use `>=9.0.0,<10.0.0`. The MySQL Connector/Python 9.x series is the current Oracle-supported version. If 9.x breaks something at execution time, fall back to `>=8.3.0,<10.0.0`.

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.x |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` section — Wave 0 creation |
| Quick run command | `pytest tests/database/ -x -q` |
| Full suite command | `pytest tests/ -v --tb=short` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| INFRA-001 | All packages importable without errors | smoke | `python -c "import duckdb; import dotenv; import tenacity; import yaml"` | ❌ Wave 0 |
| INFRA-001 | pyproject.toml installs with `pip install -e .` | smoke | `pip install -e . --dry-run` | ❌ Wave 0 |
| INFRA-002 | All required directories exist | smoke | `pytest tests/test_structure.py -x -q` | ❌ Wave 0 |
| INFRA-002 | `.env.example` exists and has required keys | smoke | `pytest tests/test_structure.py::test_env_example -x` | ❌ Wave 0 |
| INFRA-002 | `data/chinook.db` exists and is a valid SQLite file | smoke | `pytest tests/test_structure.py::test_chinook_db -x` | ❌ Wave 0 |
| DB-001 | DuckDB connection succeeds to chinook.db | unit | `pytest tests/database/test_manager.py::test_duckdb_connection -x` | ❌ Wave 0 |
| DB-001 | SQLite connection succeeds to chinook.db | unit | `pytest tests/database/test_manager.py::test_sqlite_connection -x` | ❌ Wave 0 |
| DB-001 | Schema introspection returns Artist table | unit | `pytest tests/database/test_manager.py::test_schema_introspection -x` | ❌ Wave 0 |
| DB-001 | Schema includes PKs for Artist table | unit | `pytest tests/database/test_manager.py::test_schema_primary_keys -x` | ❌ Wave 0 |
| DB-001 | Schema includes FKs for Track table | unit | `pytest tests/database/test_manager.py::test_schema_foreign_keys -x` | ❌ Wave 0 |
| DB-001 | Sample rows (2) included in schema output | unit | `pytest tests/database/test_manager.py::test_schema_sample_rows -x` | ❌ Wave 0 |
| DB-001 | Schema cache returns same object on second call | unit | `pytest tests/database/test_manager.py::test_schema_caching -x` | ❌ Wave 0 |
| DB-001 | refresh_schema() clears and re-fetches cache | unit | `pytest tests/database/test_manager.py::test_schema_refresh -x` | ❌ Wave 0 |
| DB-001 | Retry logic fires on ConnectionError (mocked) | unit | `pytest tests/database/test_manager.py::test_connection_retry -x` | ❌ Wave 0 |
| DB-001 | PostgreSQL connector mock returns expected schema | unit | `pytest tests/database/test_manager.py::test_postgresql_mock -x` | ❌ Wave 0 |
| DB-001 | MySQL connector mock returns expected schema | unit | `pytest tests/database/test_manager.py::test_mysql_mock -x` | ❌ Wave 0 |

**Note on MySQL/PostgreSQL tests:** Phase 1 tests for these dialects use `pytest-mock` to mock the connection — no live MySQL/PostgreSQL instances required for the test suite to pass.

### Sampling Rate
- **Per task commit:** `pytest tests/database/ -x -q` (fast, focused)
- **Per wave merge:** `pytest tests/ -v --tb=short`
- **Phase gate:** Full suite green before marking Phase 1 complete

### Wave 0 Gaps
All test files are missing (greenfield project). Wave 0 must create:

- [ ] `tests/__init__.py` — makes tests a package
- [ ] `tests/conftest.py` — shared fixtures (chinook_db_path, mock_pg_conn, mock_mysql_conn)
- [ ] `tests/database/__init__.py`
- [ ] `tests/database/test_manager.py` — covers DB-001 (see table above)
- [ ] `tests/test_structure.py` — covers INFRA-002 directory/file existence checks
- [ ] `pyproject.toml` `[tool.pytest.ini_options]` section — `testpaths = ["tests"]`, `addopts = "-v"`
- [ ] Framework install: `pip install -e ".[dev]"` — installs pytest + pytest-mock + ruff

---

## Sources

### Primary (HIGH confidence)
- DuckDB official docs (duckdb.org/docs/stable) — DB-API connection, INFORMATION_SCHEMA, concurrency model, multiple threads guide
- PyPI package pages (pypi.org) — verified current versions: duckdb 1.5.0, psycopg2-binary 2.9.11, mysql-connector-python 9.6.0, python-dotenv 1.2.2, tenacity 9.1.4, pytest 9.0.2
- SQLite official docs (sqlite.org/pragma.html) — PRAGMA table_info, PRAGMA foreign_key_list
- psycopg2 official docs (psycopg.org/docs/pool.html) — ThreadedConnectionPool API
- Python Packaging User Guide (packaging.python.org) — pyproject.toml PEP 621 format, optional-dependencies

### Secondary (MEDIUM confidence)
- lerocha/chinook-database GitHub (github.com/lerocha/chinook-database) — Chinook_Sqlite.sqlite is 1.02 MB, available at master branch; 11 tables including Artist, Album, Track, Invoice, Customer
- DuckDB concurrency docs (duckdb.org/docs/stable/connect/concurrency) — single-writer limitation confirmed

### Tertiary (LOW confidence)
- WebSearch community sources for MySQL connector 9.x API changes from 8.x — not independently verified against official Oracle docs; treat as needing confirmation at implementation time

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all versions verified against current PyPI as of 2026-03-09
- Architecture: HIGH — DuckDB threading model from official docs; pooling patterns from official psycopg2 and MySQL connector docs
- Pitfalls: HIGH for DuckDB/SQLite limitations (official docs), MEDIUM for mysql-connector version API differences (WebSearch only)

**Research date:** 2026-03-09
**Valid until:** 2026-06-09 (90 days — stable libraries; re-check langchain/langgraph versions which move faster)
