---
phase: 01-foundation-core-infrastructure
plan: 01
subsystem: infra
tags: [python, setuptools, pytest, duckdb, sqlite, pyyaml, langgraph, langchain, pinecone, groq, ragas]

# Dependency graph
requires: []
provides:
  - Python package installable via pip install -e ".[dev]"
  - Directory scaffold: agents/, database/connectors/, graph/, llm/, vector/, evaluation/, memory/, utils/, streamlit_app/
  - pyproject.toml with PEP 621 format and dev/mysql/postgresql optional extras
  - config/config.yaml with database, llm, retry, app defaults
  - config/error-taxonomy.json skeleton (categories filled Phase 6)
  - .env.example with 17 environment variable keys
  - data/chinook.db: Chinook SQLite sample database (11 tables, ~984 KB)
  - tests/test_structure.py: 6 structural verification tests (INFRA-002)
  - tests/database/test_manager.py: 11 xfail stubs defining DB-001 contract
  - tests/conftest.py: chinook_db_path, mock_pg_conn, mock_mysql_conn fixtures
affects:
  - 01-02 (DatabaseManager implementation needs package structure and chinook.db)
  - 01-03 (connectors need database/ package)
  - all subsequent phases (import from agents, graph, llm, vector, etc.)

# Tech tracking
tech-stack:
  added:
    - duckdb>=1.1.0,<2.0.0
    - python-dotenv>=1.0.0,<2.0.0
    - tenacity>=9.0.0,<10.0.0
    - pyyaml>=6.0.0,<7.0.0
    - langchain>=0.2.0,<0.4.0
    - langgraph>=0.1.0,<0.4.0
    - streamlit>=1.30.0,<2.0.0
    - pinecone-client>=3.0.0,<5.0.0
    - groq>=0.4.0,<2.0.0
    - openai>=1.0.0,<2.0.0
    - ragas>=0.1.0,<0.3.0
    - pytest>=9.0.0,<10.0.0 (dev)
    - pytest-mock>=3.14.0,<4.0.0 (dev)
    - ruff>=0.8.0,<1.0.0 (dev)
  patterns:
    - PEP 621 pyproject.toml with compatible-range version specifiers
    - Optional extras for database drivers (mysql, postgresql) to avoid import errors
    - xfail stubs to define API contracts before implementation
    - conftest.py session-scoped fixture for shared test database path

key-files:
  created:
    - pyproject.toml
    - .env.example
    - .gitignore
    - config/config.yaml
    - config/error-taxonomy.json
    - agents/__init__.py
    - database/__init__.py
    - database/connectors/__init__.py
    - graph/__init__.py
    - llm/__init__.py
    - vector/__init__.py
    - evaluation/__init__.py
    - memory/__init__.py
    - utils/__init__.py
    - streamlit_app/__init__.py
    - data/.gitkeep
    - data/chinook.db
    - tests/__init__.py
    - tests/conftest.py
    - tests/database/__init__.py
    - tests/database/test_manager.py
    - tests/test_structure.py
  modified: []

key-decisions:
  - "Used setuptools.build_meta instead of setuptools.backends.legacy — the legacy backend does not exist in setuptools 82.0.1 (Python 3.14 environment)"
  - "Pinned compatible-range deps with wider upper bounds on fast-moving libs (langchain, langgraph, pinecone, groq, ragas)"
  - "Downloaded official Chinook_Sqlite.sqlite from lerocha/chinook-database GitHub (11 tables, 984 KB)"
  - "All database driver __init__.py files are empty — no optional imports at module load to avoid ImportError when mysql/postgresql extras not installed"

patterns-established:
  - "xfail stubs: define test API contract before implementation arrives in next plans"
  - "Optional extras pattern: mysql and postgresql deps in separate extras groups, never imported at top level"
  - "Config-first: config/config.yaml for app defaults, .env.example for secrets"

requirements-completed: [INFRA-001, INFRA-002]

# Metrics
duration: 10min
completed: 2026-03-09
---

# Phase 1 Plan 01: Foundation — Project Scaffold and Test Infrastructure Summary

**Python package scaffold with PEP 621 pyproject.toml, 11-package directory tree, Chinook SQLite database (11 tables), and pytest infrastructure (6 structure tests passing, 11 xfail DB-001 stubs)**

## Performance

- **Duration:** ~10 min
- **Started:** 2026-03-09T18:24:28Z
- **Completed:** 2026-03-09T18:34:23Z
- **Tasks:** 2
- **Files created:** 23

## Accomplishments

- Installable Python package (`pip install -e ".[dev]"`) with 11 core dependencies and dev/mysql/postgresql optional extras
- Full directory scaffold (9 packages) with docstring `__init__.py` files — no optional driver imports at module level
- Application configuration skeleton (config.yaml, error-taxonomy.json) and secrets template (.env.example with 17 keys)
- Chinook SQLite database downloaded (lerocha/chinook-database, 1,007,616 bytes, 11 tables with full FK schema)
- Test infrastructure: 6 structure tests pass, 11 DB-001 stubs collected as xfail — full suite exits green

## Task Commits

Each task was committed atomically:

1. **Task 1: Project scaffold (dirs, pyproject.toml, .env.example, configs)** - `06fd519` (chore)
2. **Task 2: Chinook DB, test scaffold, build backend fix** - `728c28c` (feat)

**Plan metadata:** (docs commit — created after this SUMMARY)

## Files Created

- `pyproject.toml` - PEP 621 package definition with compatible-range deps
- `.env.example` - 17 environment variable keys (GROQ, OPENAI, PINECONE, DB_*, APP_*)
- `.gitignore` - Covers .env, __pycache__, venv, dist, .pytest_cache, .ruff_cache
- `config/config.yaml` - database/llm/retry/app defaults
- `config/error-taxonomy.json` - Skeleton with empty categories list (filled Phase 6)
- `agents/__init__.py` - Package docstring only
- `database/__init__.py` - Package docstring only
- `database/connectors/__init__.py` - Package docstring only
- `graph/__init__.py` - Package docstring only
- `llm/__init__.py` - Package docstring only
- `vector/__init__.py` - Package docstring only
- `evaluation/__init__.py` - Package docstring only
- `memory/__init__.py` - Package docstring only
- `utils/__init__.py` - Package docstring only
- `streamlit_app/__init__.py` - Package docstring only
- `data/.gitkeep` - Tracks empty data/ directory in git
- `data/chinook.db` - Chinook SQLite (11 tables: Album, Artist, Customer, Employee, Genre, Invoice, InvoiceLine, MediaType, Playlist, PlaylistTrack, Track)
- `tests/__init__.py` - Empty
- `tests/conftest.py` - chinook_db_path, mock_pg_conn, mock_mysql_conn fixtures
- `tests/database/__init__.py` - Empty
- `tests/database/test_manager.py` - 11 xfail stubs for DatabaseManager API contract
- `tests/test_structure.py` - 6 INFRA-002 structural verification tests

## Decisions Made

- Used `setuptools.build_meta` instead of plan's `setuptools.backends.legacy` — the backends.legacy module does not exist in setuptools 82.0.1 (Python 3.14 environment). Standard build_meta is the correct backend for all modern setuptools.
- Used wider upper bounds on fast-moving libraries (langchain `<0.4.0`, langgraph `<0.4.0`, pinecone-client `<5.0.0`, groq `<2.0.0`, ragas `<0.3.0`) per plan guidance.
- All `__init__.py` files contain only docstrings — no optional database driver imports at module level to avoid ImportError when extras are not installed.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Fixed build-backend from setuptools.backends.legacy to setuptools.build_meta**
- **Found during:** Task 2 (pip install -e ".[dev]")
- **Issue:** `setuptools.backends.legacy` does not exist in setuptools 82.0.1 — pip reported `BackendUnavailable: Cannot import 'setuptools.backends.legacy'`
- **Fix:** Changed `build-backend = "setuptools.backends.legacy:build"` to `build-backend = "setuptools.build_meta"` in pyproject.toml
- **Files modified:** `pyproject.toml`
- **Verification:** `pip install -e ".[dev]"` completed successfully; `pytest tests/` exits green
- **Committed in:** `728c28c` (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Build backend correction was essential for package installation. No scope creep.

## Issues Encountered

Pre-existing dependency conflict warnings from other installed packages (`bwa-blog-agent`, `langchain-classic`, `langchain-groq`) appeared during `pip install` — these are conflicts in the user's global Python environment unrelated to this project. Our package installed correctly and all imports succeed.

## User Setup Required

None - no external service configuration required for this foundation plan.

## Next Phase Readiness

- Package installable, directory tree ready — Plans 02 and 03 can implement DatabaseManager and connectors
- xfail stubs in `tests/database/test_manager.py` define exact API contract for DatabaseManager
- `chinook_db_path` fixture ready for connector integration tests
- All required directories exist; no blockers for subsequent plans

## Self-Check: PASSED

- All 23 created files verified to exist on disk
- Both task commits verified in git log (06fd519, 728c28c)
- Full test suite: 6 passed, 11 xfailed, 0 failures

---
*Phase: 01-foundation-core-infrastructure*
*Completed: 2026-03-09*
