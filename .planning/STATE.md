---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
current_phase: 5
status: unknown
last_updated: "2026-03-15T09:08:25.638Z"
progress:
  total_phases: 12
  completed_phases: 4
  total_plans: 12
  completed_plans: 11
  percent: 92
---

# Project State: Text-to-SQL Agentic Pipeline

**Last Updated:** 2026-03-15
**Milestone:** v1.0 - Production-Ready Multi-Agent Text-to-SQL System
**Current Phase:** 5

---

## Current Status

**Progress:** [█████████░] 92%

**Active Work:** Phase 5 Plan 01 complete — SQL safety scanner (scan_sql, audit_blocked_query), executor_node (safety gate, LIMIT injection, 60s timeout, structured errors), AgentState expanded to 17 fields; 39 new tests; 134 total tests green

**Blockers:** None

---

## Recent Activity

### 2026-03-15: Phase 5 Plan 01 Complete (SQL Safety Scanner and Executor Node)
- database/safety.py: scan_sql() strips string literals + block/line comments before keyword extraction (prevents false positives on updated_at, delete_flag, string literals with DROP/INSERT); audit_blocked_query() structured WARNING log
- config/safety_config.yaml: allowed [SELECT, WITH], blocked 9 DDL/DML types, execution timeout 60s, max_rows 1000
- agents/nodes/executor.py: null checks (missing_sql, no_connection), safety scan gate, LIMIT 1000 injection, ThreadPoolExecutor timeout (60s), structured error dicts (error_type, message, dialect, failed_sql, hint)
- graph/state.py: AgentState 14→17 fields: sql_explanation, execution_metadata, approval_status added
- config/config.yaml: query_timeout 30→60, safety section (enabled: true), hitl section
- Auto-fix: updated tests/graph/test_state.py EXPECTED_FIELDS (14→17) after AgentState expansion (Rule 1)
- Full test suite: 134 passed, 0 failed (95 prior + 39 new SAFETY-001/DB-002/DB-003/AGENT-005)
- DB-002, DB-003, SAFETY-001, AGENT-005 requirements satisfied

### 2026-03-15: Phase 4 Plan 02 Complete (Query Planner and SQL Generator Agent Nodes)
- agents/nodes/query_planner.py: _PLANNER_PROMPT constant (LLM-003), 9-key JSON plan, _DEFAULT_PLAN fallback, markdown fence stripping
- agents/nodes/sql_generator.py: _GENERATOR_PROMPT constant (LLM-003), _DIALECT_REMINDERS (postgres/mysql/sqlite/duckdb), _validate_sql, SQL:/EXPLANATION: output split
- tests/agents/test_query_planner.py: 5 AGENT-003 unit tests
- tests/agents/test_sql_generator.py: 7 AGENT-004 unit tests
- Auto-fix: none needed — sys.modules injection pattern from Plan 01 reused cleanly
- Full test suite: 95 passed, 0 failed (83 prior + 12 new AGENT-003/AGENT-004/LLM-003)
- AGENT-003, AGENT-004, LLM-003 requirements satisfied

### 2026-03-15: Phase 4 Plan 01 Complete (Gatekeeper and Schema Linker Agent Nodes)
- graph/state.py: added resolved_query field (14 fields total)
- graph/conditions.py: route_after_gatekeeper handles 4 categories (sql/follow_up->schema_linker, conversational/ambiguous->formatter)
- agents/nodes/gatekeeper.py: _GATEKEEPER_PROMPT constant (LLM-003), 4-category ChatGroq classification, follow-up rewrite, destructive NL block, db_manager guard
- agents/nodes/schema_linker.py: lazy get_retriever(), resolved_query preference, full-schema fallback
- tests/agents/: __init__.py, conftest.py (make_agent_state), test_gatekeeper.py (7 tests), test_schema_linker.py (3 tests)
- Auto-fix: sys.modules injection + importlib.reload() for mocking lazy-imported langchain_groq (same pattern as chromadb/pinecone in Phase 3)
- Full test suite: 83 passed, 0 failed (73 prior + 10 new AGENT-001/AGENT-002/LLM-003)
- AGENT-001, AGENT-002, LLM-003 requirements satisfied

### 2026-03-14: Phase 3 Plan 02 Complete (BaseRetriever with Pinecone and ChromaDB Backends)
- vector/retriever.py: BaseRetriever ABC, PineconeRetriever (two-stage retrieval, serverless index), ChromaRetriever (PersistentClient, sanitized collection names), get_retriever() factory
- config/pinecone_config.yaml: index_name text2sql-schema, dimension 1024, cosine metric, aws us-east-1
- vector/__init__.py: re-exports BaseRetriever, get_retriever, EmbeddingGenerator, SchemaGraph, text builders
- tests/vector/test_retrieval.py: 14 unit tests, all backends fully mocked (sys.modules injection for uninstalled optional deps)
- Auto-fix: chromadb mock via sys.modules injection (not installed as package — patch("chromadb.X") fails with ModuleNotFoundError)
- Full test suite: 73 passed, 0 failed (59 prior + 14 new VECTOR-001/VECTOR-003)
- VECTOR-001 and VECTOR-003 requirements satisfied

### 2026-03-14: Phase 3 Plan 01 Complete (Embedding Generation and FK Schema Graph)
- vector/embeddings.py: EmbeddingGenerator (lazy BGE model, lru_cache query embeds, batched doc embeds), build_table_text, build_column_text
- vector/schema_graph.py: SchemaGraph (FK adjacency dict, expand_tables 1-hop forward, generate_join_hints)
- pyproject.toml: pinecone-client removed from core deps; vector extras group added (pinecone>=5.1.0, sentence-transformers>=5.0.0, chromadb>=0.5.0)
- tests/vector/: conftest.py (3 Chinook fixtures) + test_embeddings.py (10 tests) + test_schema_graph.py (8 tests)
- Full test suite: 59 passed, 0 failed (41 prior + 18 new VECTOR-002/003)
- VECTOR-002 and VECTOR-003 requirements satisfied

### 2026-03-14: Phase 2 Plan 01 Complete (AgentState TypedDict and Routing Functions)
- graph/state.py: AgentState TypedDict with 13 fields, Annotated[list, add_messages] reducer confirmed correct
- graph/conditions.py: route_after_gatekeeper (SQL/conversational routing) and route_after_executor (retry/exhausted logic)
- tests/graph/__init__.py: package marker created
- tests/graph/conftest.py: make_initial_state() helper returning complete AgentState dict
- tests/graph/test_state.py: 5 GRAPH-001 unit tests — all 5 passing
- Full test suite: 36 passed, 0 failed (31 Phase 1 + 5 new GRAPH-001)
- GRAPH-001 requirement satisfied

### 2026-03-14: Phase 2 Plan 03 Complete (LangGraph StateGraph Builder)
- graph/builder.py: StateGraph(AgentState) with 9 nodes, 2 conditional edges, correction loop back-edge
- compiled_graph = build_graph() singleton at module level, compiled with MemorySaver() checkpointer
- tests/graph/test_graph.py: 5 integration tests — GRAPH-002 (compile, traversal, Mermaid) + GRAPH-003 (session isolation)
- Full test suite: 41 passed, 0 failed (36 prior + 5 new GRAPH-002/GRAPH-003)
- Phase 2 complete: GRAPH-001, GRAPH-002, GRAPH-003 all satisfied

### 2026-03-14: Phase 2 Plan 02 Complete (LangGraph Node Placeholders)
- Created agents/nodes/ directory with 9 async placeholder node functions
- Each node: async def, accepts AgentState, returns {}, uses logging.getLogger(__name__)
- Created agents/nodes/__init__.py re-export module with __all__ for all 9 nodes
- graph/state.py was pre-existing from Plan 01 partial execution (61e080c)
- All 31 tests still passing — zero regressions
- GRAPH-002 requirement satisfied

### 2026-03-09: Phase 1 Plan 03 Complete (PostgreSQL + MySQL Connectors)
- PostgreSQLConnector: psycopg2 ThreadedConnectionPool (minconn=1, maxconn=10), lazy import inside connect()
- MySQLConnector: mysql.connector.pooling.MySQLConnectionPool (pool_size=5), lazy import inside connect()
- Tenacity retry decorator: 3 attempts, exponential backoff (1s/2s/4s) on ConnectionError/TimeoutError/OSError
- DatabaseManager refactored: _CONNECTOR_REGISTRY dict replaced with _get_connector() factory for lazy imports
- All 3 previously-xfail tests now PASSED: test_connection_retry, test_postgresql_mock, test_mysql_mock
- Full test suite: 31 passed, 0 failed (up from 19 passed, 3 xfailed)

### 2026-03-09: Phase 1 Plan 02 Complete (Database Connectors)
- Implemented BaseConnector ABC (5 abstract methods), SchemaTable/ColumnInfo/FKInfo TypedDicts
- DuckDBConnector: thread-safe cursor-per-thread via threading.local, INFORMATION_SCHEMA + PRAGMA FK fallback
- SQLiteConnector: PRAGMA table_info + foreign_key_list introspection, PRAGMA foreign_keys = ON enforcement
- DatabaseManager facade: factory dispatch on db_type, schema caching, refresh_schema()
- 8 previously-xfail DuckDB/SQLite tests now pass; test suite: 19 passed, 3 xfailed, 0 failures

### 2026-03-09: Phase 1 Plan 01 Complete (Foundation Scaffold)
- Created pyproject.toml (PEP 621, 11 core deps, dev/mysql/postgresql extras)
- Created full directory scaffold (9 packages with __init__.py files)
- Created config/config.yaml and config/error-taxonomy.json skeleton
- Created .env.example with 17 environment variable keys
- Downloaded Chinook SQLite DB (11 tables, ~984 KB) to data/chinook.db
- Created test infrastructure: 6 structural tests (INFRA-002), 11 xfail DB-001 stubs
- Auto-fix: switched build-backend to setuptools.build_meta (backends.legacy unavailable)
- pip install -e ".[dev]" succeeds; pytest tests/ — 6 passed, 11 xfailed, 0 failures

### 2025-03-08: Project Initialization Complete
- Created PROJECT.md with comprehensive vision and architecture
- Configured workflow (YOLO mode, balanced profile)
- Research phase completed (4 agents investigated LangGraph patterns, text-to-SQL SOTA, vector retrieval, error correction)
- Generated REQUIREMENTS.md with V1/V2 scoping
- Created ROADMAP.md with 12-phase breakdown
- Project ready for development

**Research Findings Summary:**
1. **LangGraph Patterns** - Agent loops with 2-3 max retries, state management, human-in-the-loop, session isolation
2. **Text-to-SQL SOTA** - SQL-of-Thought decomposition, schema linking (80-95% reduction), 85-87% benchmark targets
3. **Vector Schema Retrieval** - Pinecone two-stage retrieval, <500ms target, caching strategies
4. **Error Correction** - 20-category taxonomy, 65% first-retry success, Ragas metrics

---

## Key Decisions

### Phase 5 Plan 01 (2026-03-15)
- Strip string literals and block/line comments before SQL keyword extraction — prevents false positives on column names like `updated_at` or string values containing `DROP`; ordering matters: block comments first, then line comments, then quoted strings
- error_log is a structured dict (not plain string) — enables route_after_executor and correction loop to inspect error_type programmatically without string parsing; dict is truthy so existing routing still works
- ThreadPoolExecutor(max_workers=1) with future.result(timeout=N) for query timeout — cleaner than asyncio.wait_for with synchronous execute_query API; avoids event loop nesting issues
- _get_timeout() is a plain function (not cached constant) — allows tests to patch via patch('agents.nodes.executor._get_timeout', return_value=0.1) for fast timeout tests
- LIMIT injection via simple regex search — no AST needed; appends to outermost query only, correct for SELECT and WITH (CTE) statements

### Phase 4 Plan 02 (2026-03-15)
- sys.modules injection + importlib.reload() pattern reused for query_planner and sql_generator lazy ChatGroq mocking
- Single LLM call with SQL:/EXPLANATION: output format — re.split on EXPLANATION: label cleanly separates SQL from explanation
- _validate_sql strips fences before SELECT/WITH prefix check — fence presence would fail the prefix assertion otherwise
- _DEFAULT_PLAN copy returned via dict() — prevents cross-test mutation of module-level default

### Phase 4 Plan 01 (2026-03-15)
- sys.modules injection + importlib.reload() for gatekeeper tests — lazy-imported ChatGroq inside function body is never bound at module level; patch() fails; sys.modules injection forces re-import to pick up mock
- Destructive NL safety check runs before LLM call — saves tokens and prevents any LLM-assisted workaround; patterns include delete/drop/truncate/remove all/destroy/erase
- route_after_gatekeeper: follow_up → schema_linker — rewritten follow-up queries need schema lookup before SQL generation, same path as sql
- resolved_query field added after user_query in AgentState — stores standalone rewrite for follow-up queries; never overwrites user_query for audit trail
- schema_linker uses `resolved_query or user_query` — single `or` expression handles both None and empty string cleanly

### Phase 3 Plan 02 (2026-03-14)
- Lazy import pinecone/chromadb inside __init__ — keeps optional extras from causing ImportError at package import time
- schema_cache stored on instance during embed_schema — cleaner than passing schema to every retrieve_tables call
- ChromaRetriever._collection_name sanitizes colons to underscores — ChromaDB rejects colons in collection names
- sys.modules injection for chromadb/pinecone mocks — both are uninstalled optional extras; patch.dict(sys.modules) is the correct approach
- get_retriever() factory uses os.getenv (not os.environ[]) — avoids KeyError when PINECONE_API_KEY absent

### Phase 3 Plan 01 (2026-03-14)
- pinecone-client removed from core deps; pinecone>=5.1.0 placed in vector optional extras only — avoids ImportError when vector deps not installed
- EmbeddingGenerator lazy-loads SentenceTransformer inside _get_model() — follows project lazy-import pattern for optional extras
- lru_cache on embed_query_cached requires __hash__=id(self) and __eq__=is — Python requirement for caching on instance methods
- SchemaGraph expand_tables uses forward-only FK direction — avoids context explosion from high-fan-in tables like Customer
- embed_documents uses show_progress_bar=False with manual INFO logging — consistent with project logger pattern

### Phase 2 Plan 01 (2026-03-14)
- add_messages imported from langgraph.graph.message (canonical path in 0.3.34, not langchain_core)
- db_manager typed as Optional[object] — holds DatabaseManager at runtime, None in tests; None is JSON-serializable
- make_initial_state is a plain function (not pytest fixture) to avoid fixture scoping issues when imported across test modules
- routing functions are synchronous def (not async def) as required by LangGraph add_conditional_edges
- route_after_executor uses retry_count < 2 threshold so count=2 maps to retries-exhausted formatter path

### Phase 2 Plan 03 (2026-03-14)
- compiled_graph = build_graph() at module level — single import builds the singleton; avoids re-instantiating MemorySaver per call
- MemorySaver() passed at compile time (not invoke time) — required for thread_id session isolation via LangGraph checkpointer protocol
- asyncio.run() in sync pytest tests — no extra async testing dependencies needed; keeps test deps minimal
- Path map dict keys must match routing function return strings exactly: "schema_linker", "formatter", "correction_plan"
- Correction loop (correction_sql -> executor) is deliberate cyclic back-edge; LangGraph handles cycles safely when max_retries bounded in state logic

### Phase 2 Plan 02 (2026-03-14)
- All node functions are `async def` — LangGraph uses `ainvoke` for graph execution; synchronous nodes would block the event loop
- All nodes return `{}` (empty dict, not None) — returning None raises TypeError in LangGraph's state merge step
- Nodes import only from `graph.state` — circular import prevented by never importing from `graph.builder`

### Phase 1 Plan 03 (2026-03-09)
- Tenacity retry placed inside test_connection() as local closure — mocker.patch replaces class-level attributes, stripping decorators on connect() itself; wrapping self.connect() inside a locally-decorated function survives the patch
- DatabaseManager switched from _CONNECTOR_REGISTRY dict to _get_connector() factory — enables lazy imports per dialect without top-level ImportError
- PostgreSQL uses getconn()/putconn() from ThreadedConnectionPool; MySQL uses get_connection() with connection.close() returning connection to pool automatically
- get_schema() returns {} (not raises) when fetchall() returns no rows — required for mock tests that don't set up full cursor side effects

### Phase 1 Plan 02 (2026-03-09)
- DuckDB PRAGMA foreign_key_list() used as FK fallback — INFORMATION_SCHEMA FK data is incomplete for SQLite-attached files
- Removed global pytestmark xfail from test_manager.py; per-test xfail marks on Plan-03 tests only
- DatabaseManager uses lazy _CONNECTOR_REGISTRY; PostgreSQL/MySQL connectors added in Plan 03 via conditional import

### Phase 1 Plan 01 (2026-03-09)
- Used `setuptools.build_meta` instead of `setuptools.backends.legacy` — backends.legacy unavailable in setuptools 82.0.1 on Python 3.14
- Optional database extras (mysql, postgresql) kept in separate extras groups — no top-level imports in __init__.py files
- Downloaded official Chinook_Sqlite.sqlite from lerocha/chinook-database (11 tables with FK schema)

### Architecture
- LangGraph multi-agent orchestration (10 specialized nodes)
- Pinecone for semantic schema retrieval (handles 100+ table databases)
- Groq API primary, OpenAI fallback
- Streamlit frontend with interactive debugging panel
- Human-in-the-loop approval gate (configurable)

### Technology Stack
- **Core:** langchain, langgraph, streamlit
- **Databases:** duckdb, mysql-connector-python, psycopg2, sqlite3
- **Vector:** pinecone-client
- **LLMs:** groq, openai
- **Evaluation:** ragas
- **Testing:** pytest, tenacity

### Development Approach
- 12 phases over 3-4 weeks
- Each phase 1-3 days duration
- Unit tests for all agent nodes
- Golden dataset (100+ test cases)
- Docker-first deployment

---

## Open Issues

None currently.

---

## Session Continuity

**Last Session:** 2026-03-15T09:06:50Z

**Resume Point:** Completed 05-execution-safety-layer 05-01-PLAN.md

**Next Steps:**
1. Phase 5 Plan 01 complete — DB-002, DB-003, SAFETY-001, AGENT-005 satisfied
2. Proceed to Phase 5 Plan 02 or next phase (correction loop / formatter)

**Context for Next Session:**
- database/safety.py: scan_sql() (literal/comment stripping, keyword extraction), audit_blocked_query()
- agents/nodes/executor.py: null checks, safety gate, LIMIT 1000 injection, ThreadPoolExecutor 60s timeout, structured error dicts
- graph/state.py: AgentState 17 fields — sql_explanation, execution_metadata, approval_status added
- error_log is now a structured dict (error_type, message, dialect, failed_sql, hint) — not plain string
- Test suite: 134 passed, 0 failed, 0 xfailed
- DB-002, DB-003, SAFETY-001, AGENT-005 all satisfied

---

## Notes

- Research agents completed work but encountered tool permission issues preventing file writes to `.planning/research/`
- Research findings captured in this STATE.md for reference
- YOLO mode active - auto-approves most decisions
- Balanced model profile - Opus for planning, Sonnet for execution

---

*State file created: 2025-03-08*
*Phase 1 complete: 2026-03-09*
