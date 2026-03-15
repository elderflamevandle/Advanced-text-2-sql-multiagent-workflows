# Roadmap: Text-to-SQL Agentic Pipeline

**Milestone:** v1.0 - Production-Ready Multi-Agent Text-to-SQL System
**Status:** Planning
**Target:** 12 phases, ~3-4 weeks of development

---

## Phase Overview

| Phase | Name | Status | Requirements |
|-------|------|--------|--------------|
| 1 | 3/3 | Complete   | 2026-03-09 |
| 2 | 3/3 | Complete   | 2026-03-15 |
| 3 | Vector Schema Retrieval (Pinecone) | Not Started | VECTOR-001, VECTOR-002, VECTOR-003 |
| 4 | Specialized Agent Nodes | Not Started | AGENT-001, AGENT-002, AGENT-003, AGENT-004 |
| 5 | Execution & Safety Layer | Not Started | DB-002, DB-003, SAFETY-001 |
| 6 | Error Correction Loop | Not Started | ERROR-001, ERROR-002, ERROR-003 |
| 7 | LLM Integration & Fallback | Not Started | LLM-001, LLM-002, LLM-003 |
| 8 | Streamlit Frontend | Not Started | UI-001, UI-002, UI-003, UI-004 |
| 9 | Memory & History | Not Started | MEMORY-001, MEMORY-002, SESSION-001 |
| 10 | Testing & Quality | Not Started | TEST-001, TEST-002, TEST-003 |
| 11 | Evaluation (Ragas) | Not Started | EVAL-001, EVAL-002 |
| 12 | Deployment & Documentation | Not Started | INFRA-003, DOC-001, DOC-002 |

---

## Phase 1: Foundation & Core Infrastructure

**Goal:** Establish Python environment, project structure, and basic database connectivity

**Duration:** 1-2 days

**Requirements Delivered:**
- INFRA-001: Python Environment Setup
- INFRA-002: Project Structure
- DB-001: DatabaseManager Core

**Success Criteria:**
- ✓ Virtual environment with all dependencies installed
- ✓ Clean modular directory structure (`/agents`, `/database`, `/ui`, `/tests`, etc.)
- ✓ DatabaseManager connects to DuckDB, MySQL, PostgreSQL, SQLite
- ✓ Schema introspection fetches tables, columns, types, PKs, FKs
- ✓ Sample database (Chinook) included and accessible
- ✓ `.env.example` created for secrets management
- ✓ Basic smoke test: connect to sample DB and fetch schema

**Plans:** 3/3 plans complete

Plans:
- [ ] 01-01-PLAN.md — Project scaffold: pyproject.toml, directory structure, config skeletons, Chinook DB, test infrastructure
- [ ] 01-02-PLAN.md — BaseConnector abstraction + DuckDB/SQLite connectors + DatabaseManager facade
- [ ] 01-03-PLAN.md — PostgreSQL/MySQL connectors + tenacity retry + complete DB-001 test suite

**Dependencies:** None (foundational phase)

**Risks:**
- Package version conflicts
- Database driver installation issues on Windows
- Connection pooling complexity

---

## Phase 2: AgentState & LangGraph Skeleton

**Goal:** Define state schema and build basic LangGraph workflow with placeholder nodes

**Duration:** 1-2 days

**Requirements Delivered:**
- GRAPH-001: AgentState Definition
- GRAPH-002: StateGraph Compilation
- GRAPH-003: Session Isolation

**Success Criteria:**
- ✓ AgentState TypedDict with all required fields
- ✓ LangGraph graph compiles successfully
- ✓ 10 placeholder nodes connected (gatekeeper → schema_linker → query_planner → sql_generator → executor → correction_plan → correction_sql → formatter → evaluator)
- ✓ Conditional edges for error correction loop
- ✓ MemorySaver with SQLite checkpointing
- ✓ Thread-based session isolation working
- ✓ Simple end-to-end test: pass query through graph (nodes return mock data)

**Deliverables:**
- `graph/state.py` - AgentState definition
- `graph/builder.py` - StateGraph construction
- `graph/conditions.py` - conditional routing functions
- `agents/` - placeholder node functions
- `tests/graph/test_state.py` - state validation tests
- `tests/graph/test_graph.py` - graph compilation test

**Plans:** 3/3 plans complete

Plans:
- [x] 02-01-PLAN.md — AgentState TypedDict + routing conditions + GRAPH-001 test suite (Wave 1)
- [x] 02-02-PLAN.md — 9 async placeholder node files + agents/nodes/__init__.py re-exports (Wave 1)
- [ ] 02-03-PLAN.md — graph/builder.py compiled_graph singleton + GRAPH-002/GRAPH-003 integration tests (Wave 2)

**Dependencies:** Phase 1 (project structure)

**Risks:**
- State schema evolution during development
- Conditional routing logic complexity
- Checkpoint storage performance

---

## Phase 3: Vector Schema Retrieval (Pinecone)

**Goal:** Implement semantic schema retrieval using Pinecone for large database support

**Duration:** 2-3 days

**Requirements Delivered:**
- VECTOR-001: Pinecone Integration
- VECTOR-002: Schema Embedding
- VECTOR-003: Two-Stage Retrieval

**Success Criteria:**
- ✓ Pinecone serverless index created and configured
- ✓ Schema embeddings generated (table + column level)
- ✓ Batch embedding completes in <5s for 100-table schema
- ✓ Semantic retrieval returns top-k relevant tables (<500ms)
- ✓ Relationship expansion via foreign keys (1-hop graph traversal)
- ✓ JOIN hint generation for retrieved tables
- ✓ Query embedding cache (LRU, 10K entries)
- ✓ Test: retrieve relevant tables for "total sales by region" query

**Deliverables:**
- `vector/embeddings.py` - embedding generation
- `vector/retriever.py` - Pinecone retrieval logic
- `vector/schema_graph.py` - FK relationship graph
- `config/pinecone_config.yaml` - index configuration
- `tests/vector/test_retrieval.py` - retrieval accuracy tests

**Plans:** 2 plans

Plans:
- [ ] 03-01-PLAN.md — pyproject.toml dependency fix + EmbeddingGenerator + SchemaGraph + text builders + test scaffold (Wave 1)
- [ ] 03-02-PLAN.md — BaseRetriever ABC + PineconeRetriever + ChromaRetriever + factory + retrieval tests (Wave 2)

**Dependencies:** Phase 1 (DatabaseManager for schema extraction)

**Risks:**
- Pinecone API rate limits
- Embedding cost for large schemas
- Retrieval accuracy for ambiguous queries

---

## Phase 4: Specialized Agent Nodes

**Goal:** Implement core agent logic for gatekeeper, schema linking, query planning, and SQL generation

**Duration:** 3-4 days

**Requirements Delivered:**
- AGENT-001: Gatekeeper Node
- AGENT-002: Schema Linker Node
- AGENT-003: Query Planner Node
- AGENT-004: SQL Generator Node

**Success Criteria:**
- ✓ Gatekeeper validates queries, detects conversational vs SQL requests
- ✓ Schema Linker integrates Pinecone retrieval into graph
- ✓ Query Planner generates Chain-of-Thought execution plans
- ✓ SQL Generator translates plans to dialect-specific SQL
- ✓ Prompts optimized for each agent role
- ✓ Test: "Show total sales by region" → valid PostgreSQL SELECT query

**Deliverables:**
- `agents/gatekeeper.py` - query validation
- `agents/schema_linker.py` - schema retrieval integration
- `agents/query_planner.py` - CoT planning
- `agents/sql_generator.py` - SQL generation
- `utils/prompts/` - agent-specific prompts
- `tests/agents/` - unit tests for each agent

**Dependencies:**
- Phase 2 (graph structure)
- Phase 3 (Pinecone retrieval)

**Risks:**
- Prompt engineering iterations
- Handling edge cases (ambiguous queries)
- SQL syntax errors for complex queries

---

## Phase 5: Execution & Safety Layer

**Goal:** Safe SQL execution with keyword scanning, read-only access, and dialect handling

**Duration:** 2 days

**Requirements Delivered:**
- DB-002: Read-Only Execution
- DB-003: Dialect-Specific SQL
- SAFETY-001: Destructive Query Prevention

**Success Criteria:**
- ✓ Executor node executes SQL via DatabaseManager
- ✓ Keyword scanner blocks DROP, DELETE, TRUNCATE, UPDATE, INSERT, ALTER, CREATE
- ✓ Read-only database user enforcement
- ✓ Execution timeout (30s default, configurable)
- ✓ Transaction auto-rollback on error
- ✓ Error messages include dialect context
- ✓ Test: Attempt "DROP TABLE users" → blocked before execution

**Deliverables:**
- `agents/executor.py` - execution node
- `database/safety.py` - keyword scanner
- `database/dialect_handler.py` - dialect-specific logic
- `config/safety_config.yaml` - blocked keywords list
- `tests/safety/test_keyword_scanner.py` - safety tests

**Dependencies:**
- Phase 1 (DatabaseManager)
- Phase 4 (SQL Generator)

**Risks:**
- Bypassing keyword scanner with SQL injection
- Timeout handling for long-running queries
- Dialect edge cases

---

## Phase 6: Error Correction Loop

**Goal:** Self-correction mechanism with error taxonomy and retry logic

**Duration:** 2-3 days

**Requirements Delivered:**
- ERROR-001: Error Taxonomy
- ERROR-002: Correction Plan Agent
- ERROR-003: Correction SQL Agent

**Success Criteria:**
- ✓ error-taxonomy.json with 20 SQL error categories
- ✓ Correction Plan agent analyzes errors and generates diagnostics
- ✓ Correction SQL agent modifies SQL based on diagnosis
- ✓ Max 2-3 retry attempts with graceful degradation
- ✓ Schema-aware fuzzy matching for column/table name errors
- ✓ First-retry success rate >60% on common errors
- ✓ Test: Generate query with wrong table name → self-correct → succeed

**Deliverables:**
- `config/error-taxonomy.json` - 20 error categories
- `agents/correction_plan.py` - diagnostic agent
- `agents/correction_sql.py` - correction agent
- `utils/error_parser.py` - PostgreSQL/MySQL error parsers
- `tests/agents/test_correction.py` - correction loop tests

**Dependencies:** Phase 5 (execution failures to correct)

**Risks:**
- Low correction success rate
- Infinite loop prevention
- Overfitting to specific error types

---

## Phase 7: LLM Integration & Fallback

**Goal:** Groq API integration with OpenAI fallback strategy

**Duration:** 1-2 days

**Requirements Delivered:**
- LLM-001: Groq API Client
- LLM-002: OpenAI Fallback
- LLM-003: Token Usage Tracking

**Success Criteria:**
- ✓ Groq client initialized with API key
- ✓ Fallback to OpenAI on Groq failures or complex queries
- ✓ Configurable model selection (Llama-3 70B, Mixtral, gpt-4o-mini)
- ✓ Token usage tracking per query
- ✓ Cost calculation dashboard
- ✓ Retry logic for transient API failures
- ✓ Test: Groq timeout → automatic OpenAI fallback → success

**Deliverables:**
- `llm/groq_client.py` - Groq API client
- `llm/openai_client.py` - OpenAI client
- `llm/fallback.py` - fallback orchestration
- `llm/usage_tracker.py` - token/cost tracking
- `tests/llm/test_fallback.py` - fallback logic tests

**Dependencies:** Phase 4 (agent nodes using LLMs)

**Risks:**
- API rate limits
- Cost overruns on complex queries
- Model capability differences (Groq vs OpenAI)

---

## Phase 8: Streamlit Frontend

**Goal:** Interactive web UI with chat interface, debugging panel, and configuration

**Duration:** 3-4 days

**Requirements Delivered:**
- UI-001: Configuration Sidebar
- UI-002: Chat Interface
- UI-003: Debugging Panel
- UI-004: Dynamic Visualizations

**Success Criteria:**
- ✓ Configuration sidebar: API keys, DB credentials, DB type selector
- ✓ Chat interface with message history
- ✓ Conversational follow-up support
- ✓ Debugging panel shows: CoT plan, SQL output, retry logs, errors
- ✓ "Edit & Rerun SQL" button for manual override
- ✓ Ragas confidence score display
- ✓ Auto-generated charts for time-series/categorical data
- ✓ Token/cost usage tracker
- ✓ Human-in-the-loop approval toggle
- ✓ Test: Complete query flow through UI end-to-end

**Deliverables:**
- `streamlit_app/app.py` - main Streamlit application
- `streamlit_app/components/sidebar.py` - configuration UI
- `streamlit_app/components/chat.py` - chat interface
- `streamlit_app/components/debug_panel.py` - debugging display
- `streamlit_app/components/charts.py` - visualization logic
- `streamlit_app/styles/` - custom CSS

**Dependencies:**
- Phase 2 (graph for execution)
- Phase 7 (LLM integration)

**Risks:**
- Streamlit session state management
- Real-time updates for long-running queries
- Chart generation for complex data types

---

## Phase 9: Memory & History

**Goal:** Session management, query history storage, and analytics

**Duration:** 2 days

**Requirements Delivered:**
- MEMORY-001: Short-Term Memory
- MEMORY-002: Long-Term Memory
- SESSION-001: Session Isolation

**Success Criteria:**
- ✓ In-session conversation context maintained (5+ turns)
- ✓ Query history stored: NL question → SQL → results → timestamp
- ✓ Analytics aggregation: common patterns, accuracy trends
- ✓ Thread-based session isolation (concurrent users don't interfere)
- ✓ History export to CSV/JSON
- ✓ Analytics dashboard in Streamlit
- ✓ Test: Run 10 queries, view history, export to CSV

**Deliverables:**
- `memory/session.py` - session management
- `memory/history.py` - query history storage
- `memory/analytics.py` - analytics aggregation
- `streamlit_app/components/history.py` - history UI
- `streamlit_app/components/analytics.py` - analytics dashboard

**Dependencies:**
- Phase 2 (MemorySaver)
- Phase 8 (Streamlit for UI)

**Risks:**
- Storage growth for long conversations
- Privacy concerns for query history
- Analytics performance with large history

---

## Phase 10: Testing & Quality

**Goal:** Comprehensive test suite with unit tests, integration tests, and golden dataset

**Duration:** 2-3 days

**Requirements Delivered:**
- TEST-001: Unit Tests
- TEST-002: Integration Tests
- TEST-003: Golden Dataset

**Success Criteria:**
- ✓ 80%+ unit test coverage for all agent nodes
- ✓ Integration tests for each database backend (DuckDB, MySQL, PostgreSQL, SQLite)
- ✓ Golden dataset: 100+ test cases (simple 20%, joins 25%, aggregations 20%, subqueries 15%, complex 20%)
- ✓ Difficulty levels: easy 30%, medium 50%, hard 20%
- ✓ Automated test suite runs in CI
- ✓ Test: Full test suite passes without failures

**Deliverables:**
- `tests/agents/` - unit tests for each agent (10+ files)
- `tests/integration/` - end-to-end tests per database
- `tests/data/golden_dataset.json` - 100+ test cases
- `tests/conftest.py` - pytest fixtures
- `.github/workflows/test.yml` - CI configuration

**Dependencies:** All prior phases (testing requires full system)

**Risks:**
- Maintaining golden dataset accuracy
- Flaky tests due to LLM non-determinism
- Test execution time

---

## Phase 11: Evaluation (Ragas)

**Goal:** Accuracy scoring using Ragas framework

**Duration:** 1-2 days

**Requirements Delivered:**
- EVAL-001: Ragas Integration
- EVAL-002: Metrics Dashboard

**Success Criteria:**
- ✓ Ragas evaluation node integrated into graph
- ✓ Execution accuracy metric (datacompy comparison)
- ✓ SQL semantic equivalence metric (LLM-based)
- ✓ Confidence score (0.0-1.0) returned per query
- ✓ Evaluation overhead <500ms for simple queries
- ✓ Metrics dashboard in Streamlit
- ✓ Test: Run golden dataset through evaluation → accuracy scores

**Deliverables:**
- `evaluation/evaluator.py` - Ragas integration
- `evaluation/metrics.py` - custom metrics
- `streamlit_app/components/evaluation.py` - metrics UI
- `tests/evaluation/test_ragas.py` - evaluation tests

**Dependencies:**
- Phase 8 (Streamlit for display)
- Phase 10 (golden dataset for evaluation)

**Risks:**
- Ragas API changes
- Evaluation cost (LLM calls for semantic equivalence)
- False negatives (multiple valid SQL answers)

---

## Phase 12: Deployment & Documentation

**Goal:** Docker deployment and comprehensive documentation

**Duration:** 2 days

**Requirements Delivered:**
- INFRA-003: Docker Deployment
- DOC-001: README & Quickstart
- DOC-002: API Documentation

**Success Criteria:**
- ✓ Dockerfile builds successfully
- ✓ docker-compose.yml with app + database containers
- ✓ Environment variables via `.env`
- ✓ Container exposes Streamlit on port 8501
- ✓ Health check endpoint responds
- ✓ README with quickstart guide (5-minute setup)
- ✓ Database connection guide for each backend
- ✓ API key setup guide
- ✓ Deployment instructions (local, Streamlit Cloud, Render)
- ✓ Troubleshooting guide
- ✓ Test: Deploy to Streamlit Community Cloud → functional

**Deliverables:**
- `Dockerfile` - production-ready container
- `docker-compose.yml` - multi-container setup
- `.dockerignore` - exclude unnecessary files
- `README.md` - comprehensive documentation
- `docs/` - additional guides (database setup, deployment, troubleshooting)
- `CONTRIBUTING.md` - contribution guidelines

**Dependencies:** All prior phases (deployment requires complete system)

**Risks:**
- Docker image size
- Secrets management in containers
- Platform-specific deployment issues

---

## Milestone Completion Criteria

**Functional Requirements:**
- ✓ Accepts natural language queries via Streamlit UI
- ✓ Generates accurate SQL for DuckDB, MySQL, PostgreSQL, SQLite
- ✓ Handles complex queries (multi-table joins, aggregations, subqueries, CTEs, window functions)
- ✓ Self-corrects errors with >60% first-retry success
- ✓ Human-in-the-loop approval for query execution
- ✓ Displays results with natural language explanations
- ✓ Auto-generates visualizations for appropriate data
- ✓ Tracks query history and analytics
- ✓ Evaluates accuracy using Ragas

**Non-Functional Requirements:**
- ✓ <3s response time for simple queries
- ✓ <10s response time for complex queries
- ✓ Zero destructive queries executed
- ✓ 85%+ accuracy on standard benchmarks
- ✓ 80%+ unit test coverage
- ✓ Docker deployment functional
- ✓ Documentation complete

**Ready for:**
- Production use with sample databases
- User feedback collection
- V2 feature planning

---

## Deferred to V2

- Multi-user authentication and authorization
- Query result caching layer
- Advanced visualizations (custom dashboards, BI-style reports)
- Support for NoSQL databases
- Fine-tuned LLM models for domain-specific SQL
- Automated schema documentation generation
- Natural language query suggestions/autocomplete
- Scheduled/automated query execution
- Export to Excel/CSV/PDF
- Mobile-responsive UI
- Multi-tenant isolation with user workspaces
- Query performance optimization suggestions
- Data lineage tracking
- Integration with dbt
- Support for streaming databases
- Voice input
- Slack/Teams integration
- Cost estimation for expensive queries
- Automatic index recommendations

---

*Roadmap created: 2025-03-08*
*Phase 1 planned: 2026-03-09*
*Phase 2 planned: 2026-03-09*
*Phase 3 planned: 2026-03-14*
*Milestone: v1.0*
*Estimated duration: 3-4 weeks*
