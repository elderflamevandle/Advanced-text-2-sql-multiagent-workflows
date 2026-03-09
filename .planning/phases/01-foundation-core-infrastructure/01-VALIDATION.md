---
phase: 1
slug: foundation-core-infrastructure
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-09
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.x |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` — Wave 0 installs |
| **Quick run command** | `pytest tests/database/ -x -q` |
| **Full suite command** | `pytest tests/ -v --tb=short` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/database/ -x -q`
- **After every plan wave:** Run `pytest tests/ -v --tb=short`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| setup-env | 01 | 0 | INFRA-001 | smoke | `python -c "import duckdb; import dotenv; import tenacity"` | ❌ W0 | ⬜ pending |
| project-structure | 01 | 0 | INFRA-002 | smoke | `pytest tests/test_structure.py -x -q` | ❌ W0 | ⬜ pending |
| env-example | 01 | 0 | INFRA-002 | smoke | `pytest tests/test_structure.py::test_env_example -x` | ❌ W0 | ⬜ pending |
| chinook-db | 01 | 0 | INFRA-002 | smoke | `pytest tests/test_structure.py::test_chinook_db -x` | ❌ W0 | ⬜ pending |
| duckdb-connector | 02 | 1 | DB-001 | unit | `pytest tests/database/test_manager.py::test_duckdb_connection -x` | ❌ W0 | ⬜ pending |
| sqlite-connector | 02 | 1 | DB-001 | unit | `pytest tests/database/test_manager.py::test_sqlite_connection -x` | ❌ W0 | ⬜ pending |
| schema-introspect | 02 | 1 | DB-001 | unit | `pytest tests/database/test_manager.py::test_schema_introspection -x` | ❌ W0 | ⬜ pending |
| schema-pks | 02 | 1 | DB-001 | unit | `pytest tests/database/test_manager.py::test_schema_primary_keys -x` | ❌ W0 | ⬜ pending |
| schema-fks | 02 | 1 | DB-001 | unit | `pytest tests/database/test_manager.py::test_schema_foreign_keys -x` | ❌ W0 | ⬜ pending |
| schema-samples | 02 | 1 | DB-001 | unit | `pytest tests/database/test_manager.py::test_schema_sample_rows -x` | ❌ W0 | ⬜ pending |
| schema-cache | 02 | 1 | DB-001 | unit | `pytest tests/database/test_manager.py::test_schema_caching -x` | ❌ W0 | ⬜ pending |
| schema-refresh | 02 | 1 | DB-001 | unit | `pytest tests/database/test_manager.py::test_schema_refresh -x` | ❌ W0 | ⬜ pending |
| retry-logic | 02 | 1 | DB-001 | unit | `pytest tests/database/test_manager.py::test_connection_retry -x` | ❌ W0 | ⬜ pending |
| pg-connector | 02 | 1 | DB-001 | unit | `pytest tests/database/test_manager.py::test_postgresql_mock -x` | ❌ W0 | ⬜ pending |
| mysql-connector | 02 | 1 | DB-001 | unit | `pytest tests/database/test_manager.py::test_mysql_mock -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/__init__.py` — makes tests a package
- [ ] `tests/conftest.py` — shared fixtures (chinook_db_path, mock_pg_conn, mock_mysql_conn)
- [ ] `tests/database/__init__.py` — database test package
- [ ] `tests/database/test_manager.py` — DB-001 test stubs (all functions defined, marked xfail until impl)
- [ ] `tests/test_structure.py` — INFRA-002 directory/file existence checks
- [ ] `pyproject.toml` `[tool.pytest.ini_options]` section — `testpaths = ["tests"]`, `addopts = "-v"`
- [ ] Framework install verified: `pip install -e ".[dev]"` (installs pytest + pytest-mock + ruff)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live MySQL connection | DB-001 | Requires running MySQL instance | Start MySQL, set .env, run `pytest tests/database/test_manager.py::test_mysql_live -x` |
| Live PostgreSQL connection | DB-001 | Requires running PostgreSQL instance | Start PG, set .env, run `pytest tests/database/test_manager.py::test_pg_live -x` |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
