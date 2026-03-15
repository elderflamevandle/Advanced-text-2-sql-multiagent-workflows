# Requirements: Text-to-SQL Agentic Pipeline

## Project Overview

**Core Value**: A multi-agent LangGraph-orchestrated text-to-SQL pipeline that converts natural language questions into accurate, safe SQL queries across multiple database types with self-correction, human oversight, and comprehensive evaluation.

**Target Users**: Data analysts, business users, and developers who need to query databases using natural language without SQL expertise.

**Success Criteria**:
- 85%+ accuracy on standard text-to-SQL benchmarks (Spider, WikiSQL)
- <3s response time for simple queries, <10s for complex queries
- 60%+ self-correction success rate on first retry
- Zero destructive queries executed in production
- 80%+ unit test coverage across all agent nodes

---

## V1 Requirements (Must-Have)

### Category: Core Infrastructure (INFRA)

**INFRA-001: Python Environment Setup**
- **Description**: Initialize Python virtual environment with all required dependencies
- **Acceptance Criteria**:
  - `requirements.txt` includes: langchain, langgraph, streamlit, duckdb, mysql-connector-python, psycopg2, ragas, pinecone-client, groq
  - Virtual environment activates successfully
  - All packages install without conflicts
  - Python 3.9+ compatibility verified
- **Priority**: Critical (Blocker for all other work)

**INFRA-002: Project Structure**
- **Description**: Establish clean, modular project structure
- **Acceptance Criteria**:
  - Directories: `/agents`, `/nodes`, `/database`, `/ui`, `/config`, `/tests`, `/evaluation`
  - Configuration files: `config.yaml`, `error-taxonomy.json`
  - Sample database included (Chinook or similar)
  - `.env.example` for secrets management
- **Priority**: Critical

**INFRA-003: Docker Deployment**
- **Description**: Containerized deployment with Docker Compose
- **Acceptance Criteria**:
  - `Dockerfile` builds successfully
  - `docker-compose.yml` includes app + optional database containers
  - Environment variables configurable via `.env`
  - Container exposes Streamlit on port 8501
  - Health check endpoint responds
- **Priority**: High

### Category: Database Layer (DB)

**DB-001: DatabaseManager Core**
- **Description**: Extensible database connection manager
- **Acceptance Criteria**:
  - Supports DuckDB, MySQL, PostgreSQL, SQLite
  - Connection pooling implemented
  - Automatic schema introspection (tables, columns, types, PKs, FKs)
  - Fetches 2-5 sample rows per table
  - Graceful error handling for connection failures
- **Priority**: Critical (Blocker for execution)

**DB-002: Read-Only Execution**
- **Description**: Safe query execution with read-only constraints
- **Acceptance Criteria**:
  - Connects using read-only database user/role
  - Keyword scanner blocks: DROP, DELETE, TRUNCATE, UPDATE, INSERT, ALTER, CREATE
  - Returns error for destructive queries before execution
  - Execution timeout configurable (default 30s)
  - Transaction auto-rollback on error
- **Priority**: Critical (Safety requirement)

**DB-003: Dialect-Specific SQL**
- **Description**: Generate correct SQL syntax for each database type
- **Acceptance Criteria**:
  - Detects database dialect (MySQL, PostgreSQL, DuckDB, SQLite)
  - Applies dialect-specific functions (DATE_FORMAT vs TO_CHAR)
  - Handles LIMIT vs TOP syntax differences
  - String concatenation operators (|| vs CONCAT)
  - Error messages include dialect context
- **Priority**: High

### Category: LangGraph Orchestration (GRAPH)

**GRAPH-001: AgentState Definition**
- **Description**: Comprehensive state dictionary for agent workflow
- **Acceptance Criteria**:
  - Fields: `messages`, `user_query`, `schema`, `relevant_tables`, `query_plan`, `generated_sql`, `db_results`, `error_log`, `retry_count`, `final_answer`, `session_id`, `ragas_score`
  - Type annotations for all fields
  - Default values defined
  - Serializable to JSON
- **Priority**: Critical (Blocker for graph)

**GRAPH-002: StateGraph Compilation**
- **Description**: LangGraph workflow connecting all agent nodes
- **Acceptance Criteria**:
  - 10+ specialized nodes connected via edges
  - Conditional edges for error correction loop
  - Entry point: Gatekeeper node
  - Exit point: Format Answer node
  - Graph compiles without errors
  - Visualizable with `draw_mermaid()`
- **Priority**: Critical

**GRAPH-003: Multi-Session Memory**
- **Description**: Session isolation for concurrent users
- **Acceptance Criteria**:
  - LangGraph `MemorySaver` with thread IDs
  - Each session has unique ID (UUID)
  - Session state persists across requests
  - No cross-contamination between sessions
  - Session timeout after 30 minutes of inactivity
- **Priority**: High

### Category: Agent Nodes (AGENT)

**AGENT-001: Gatekeeper Node**
- **Description**: Query validation and routing
- **Acceptance Criteria**:
  - Validates if query is SQL-eligible
  - Detects conversational/gibberish queries
  - Identifies follow-up questions requiring history
  - Bypasses SQL generation for non-data queries
  - Returns clarification requests for ambiguous queries
- **Priority**: High

**AGENT-002: Schema Linking Node**
- **Description**: Vector-based retrieval of relevant schema
- **Acceptance Criteria**:
  - Pinecone index stores table/column embeddings
  - Semantic search retrieves top-k relevant tables (k=5-10)
  - Embedding model: text-embedding-ada-002 or similar
  - <500ms retrieval latency
  - Fallback to full schema if vector DB unavailable
- **Priority**: High

**AGENT-003: Query Planning Node**
- **Description**: Chain-of-Thought SQL planning
- **Acceptance Criteria**:
  - Explicit breakdown: SELECT, FROM, JOIN, WHERE, GROUP BY, ORDER BY, LIMIT
  - Identifies required joins with reasoning
  - Plans aggregations and window functions
  - Supports CTEs and subqueries
  - Plan stored in `query_plan` field
- **Priority**: Critical

**AGENT-004: SQL Generation Node**
- **Description**: Translates plan to executable SQL
- **Acceptance Criteria**:
  - Uses query plan as input
  - Generates dialect-specific SQL
  - Includes comments explaining complex logic
  - Validates syntax before returning
  - SQL stored in `generated_sql` field
- **Priority**: Critical

**AGENT-005: Execution & Safety Node**
- **Description**: Safe SQL execution with validation
- **Acceptance Criteria**:
  - Scans for destructive keywords
  - Executes via DatabaseManager
  - Captures results in `db_results`
  - Logs errors to `error_log`
  - Returns execution time metadata
- **Priority**: Critical

**AGENT-006: Correction Plan Node**
- **Description**: Diagnoses SQL errors using taxonomy
- **Acceptance Criteria**:
  - Maps error to 20-category taxonomy
  - Common categories: syntax error, missing table, column mismatch, type error, join error, aggregation error
  - Generates correction strategy
  - Only triggered on execution failure
  - Logs correction plan to state
- **Priority**: High

**AGENT-007: Correction SQL Node**
- **Description**: Modifies SQL based on correction plan
- **Acceptance Criteria**:
  - Applies correction strategy to SQL
  - Increments `retry_count`
  - Max 2-3 retry attempts
  - Falls back to error message if max retries exceeded
  - Preserves original SQL in history
- **Priority**: High

**AGENT-008: Format Answer Node**
- **Description**: Natural language response generation
- **Acceptance Criteria**:
  - Converts data results to readable response
  - Handles empty results gracefully
  - Flags time-series/categorical data for visualization
  - Includes summary statistics for large datasets
  - Stored in `final_answer` field
- **Priority**: High

**AGENT-009: Evaluation Node**
- **Description**: Ragas-based quality scoring
- **Acceptance Criteria**:
  - Scores query accuracy (0.0-1.0)
  - Evaluates answer relevance
  - Detects hallucinations
  - Stores score in `ragas_score`
  - Logs evaluation metrics
- **Priority**: Medium

**AGENT-010: Human-in-the-Loop Node**
- **Description**: Optional approval gate before execution
- **Acceptance Criteria**:
  - Pauses graph execution
  - Presents SQL + explanation to user
  - Awaits approve/reject/modify decision
  - Configurable on/off via UI toggle
  - Timeout after 5 minutes (auto-reject)
- **Priority**: Medium

### Category: Vector Retrieval (VECTOR)

**VECTOR-001: Pinecone Integration**
- **Description**: Vector database for schema embeddings
- **Acceptance Criteria**:
  - Pinecone index created with 1536 dimensions (OpenAI embeddings)
  - Namespace per database instance
  - Metadata includes: table_name, column_name, data_type, sample_values
  - Index updates on schema changes
  - Query retrieval <500ms
- **Priority**: High

**VECTOR-002: Schema Embedding Strategy**
- **Description**: Effective embedding of database schema
- **Acceptance Criteria**:
  - Embeddings include: table name, column names, sample values, comments
  - Composite embeddings for table + columns
  - Batch embedding for efficiency
  - Re-embedding triggered by schema changes
  - Fallback to keyword matching if embeddings fail
- **Priority**: Medium

### Category: LLM Integration (LLM)

**LLM-001: Groq API Integration**
- **Description**: Primary LLM provider for generation
- **Acceptance Criteria**:
  - Supports Llama-3 70B, Mixtral models
  - API key configuration via environment
  - Rate limiting handled gracefully
  - Streaming responses for chat interface
  - Token usage tracking
- **Priority**: Critical

**LLM-002: OpenAI Fallback**
- **Description**: Backup LLM provider
- **Acceptance Criteria**:
  - Switches to OpenAI on Groq failure/unavailability
  - Uses gpt-4o-mini for cost efficiency
  - gpt-4o for complex planning tasks (configurable)
  - Seamless provider switching
  - Logs provider used per query
- **Priority**: High

**LLM-003: Specialized Prompts**
- **Description**: Optimized prompts for each agent node
- **Acceptance Criteria**:
  - Schema linker prompt: semantic matching focus
  - Planner prompt: explicit CoT structure
  - Generator prompt: dialect-specific instructions
  - Corrector prompt: error taxonomy reference
  - Prompts stored in `/config/prompts.yaml`
- **Priority**: High

### Category: Frontend (UI)

**UI-001: Configuration Sidebar**
- **Description**: Database and API configuration panel
- **Acceptance Criteria**:
  - Inputs: DB type dropdown (DuckDB/MySQL/PostgreSQL/SQLite)
  - DB credentials: host, port, database, user, password
  - API keys: Groq, OpenAI, Pinecone
  - LLM provider selector with model dropdown
  - Token/cost usage tracker display
  - Save/load configuration profiles
- **Priority**: High

**UI-002: Chat Interface**
- **Description**: Conversational query interface
- **Acceptance Criteria**:
  - Text input for natural language queries
  - Message history display (user + assistant)
  - Session persistence across page refreshes
  - Clear session button
  - Copy response button
  - Loading indicator during query processing
- **Priority**: Critical

**UI-003: Debugging Panel**
- **Description**: SQL transparency and inspection
- **Acceptance Criteria**:
  - Expandable section per response
  - Displays: Query plan, generated SQL, execution results
  - Shows retry logs and error messages
  - "Edit & Rerun SQL" button for manual correction
  - Syntax highlighting for SQL
  - Download results as CSV
- **Priority**: High

**UI-004: Visualization Dashboard**
- **Description**: Auto-generated charts for query results
- **Acceptance Criteria**:
  - Detects time-series data (auto line chart)
  - Detects categorical data (auto bar chart)
  - Streamlit native charting or Plotly
  - User can toggle chart types
  - Handles up to 1000 rows efficiently
- **Priority**: Medium

**UI-005: Query History**
- **Description**: Session and cross-session query log
- **Acceptance Criteria**:
  - Stores: timestamp, query, SQL, results, score
  - Searchable and filterable
  - Re-run previous queries
  - Export history to JSON/CSV
  - Persists to local storage or database
- **Priority**: Medium

### Category: Error Handling (ERROR)

**ERROR-001: Error Taxonomy**
- **Description**: Comprehensive error classification system
- **Acceptance Criteria**:
  - 20-category taxonomy in `error-taxonomy.json`
  - Categories: syntax, missing_table, column_mismatch, type_error, join_error, aggregation_error, permission_denied, timeout, connection_error, etc.
  - Each category has: name, regex patterns, correction strategy
  - Extensible structure for new categories
- **Priority**: High

**ERROR-002: Retry Logic**
- **Description**: Intelligent retry with correction
- **Acceptance Criteria**:
  - Max 2-3 retry attempts configurable
  - Each retry logs: attempt number, error, correction plan
  - Different correction strategies per error type
  - Exponential backoff for connection errors
  - Falls back to error message if max retries exceeded
- **Priority**: High

### Category: Testing (TEST)

**TEST-001: Unit Tests**
- **Description**: Comprehensive unit test coverage
- **Acceptance Criteria**:
  - Tests for each agent node (10+ test files)
  - DatabaseManager tests with mock connections
  - State transition tests
  - Error taxonomy tests
  - 80%+ code coverage
  - Runs in <30s
- **Priority**: High

**TEST-002: Integration Tests**
- **Description**: End-to-end workflow tests
- **Acceptance Criteria**:
  - Tests full graph execution (gatekeeper → answer)
  - Tests error correction loop
  - Tests HITL workflow
  - Uses sample database (Chinook)
  - Validates against expected SQL outputs
- **Priority**: Medium

**TEST-003: Sample Database**
- **Description**: Standard test database
- **Acceptance Criteria**:
  - Chinook database or equivalent
  - Includes: multiple tables, FKs, various data types
  - DuckDB and SQLite versions
  - Sample queries documented
  - <10MB file size
- **Priority**: High

### Category: Evaluation (EVAL)

**EVAL-001: Ragas Integration**
- **Description**: Automated quality evaluation
- **Acceptance Criteria**:
  - Metrics: faithfulness, answer_relevance, context_precision
  - Batch evaluation mode for benchmarks
  - Per-query scoring in production
  - Scores logged to evaluation database
  - Dashboard showing score trends
- **Priority**: Medium

**EVAL-002: Benchmark Testing**
- **Description**: Standard benchmark evaluation
- **Acceptance Criteria**:
  - Spider dev set (200+ queries)
  - WikiSQL subset (100+ queries)
  - Execution accuracy metric
  - Exact match and partial match scoring
  - Results exportable to CSV
- **Priority**: Low (V1.5)

### Category: Safety & Security (SAFE)

**SAFE-001: Destructive Query Prevention**
- **Description**: Block all write/modify operations
- **Acceptance Criteria**:
  - Keyword scanner: DROP, DELETE, TRUNCATE, UPDATE, INSERT, ALTER, CREATE, GRANT, REVOKE
  - Case-insensitive detection
  - Regex patterns for obfuscated variants
  - Returns error before execution
  - Logs blocked queries
- **Priority**: Critical

**SAFE-002: Read-Only Database User**
- **Description**: Principle of least privilege
- **Acceptance Criteria**:
  - Documentation for creating read-only users
  - MySQL: GRANT SELECT only
  - PostgreSQL: GRANT SELECT on schema
  - Verification script for user permissions
  - Fallback error if write attempted
- **Priority**: Critical

**SAFE-003: API Key Security**
- **Description**: Secure secrets management
- **Acceptance Criteria**:
  - Never commit API keys to version control
  - `.env` file for local development
  - Environment variables in Docker
  - Secrets rotation documentation
  - `.env.example` template provided
- **Priority**: High

### Category: Documentation (DOC)

**DOC-001: README**
- **Description**: Comprehensive project documentation
- **Acceptance Criteria**:
  - Installation instructions
  - Quick start guide
  - Architecture diagram
  - Configuration guide
  - Troubleshooting section
- **Priority**: High

**DOC-002: API Documentation**
- **Description**: Agent node and function documentation
- **Acceptance Criteria**:
  - Docstrings for all public functions
  - Type hints throughout
  - State schema documented
  - Prompt templates explained
- **Priority**: Medium

**DOC-003: Deployment Guide**
- **Description**: Docker and cloud deployment instructions
- **Acceptance Criteria**:
  - Docker Compose setup
  - Streamlit Community Cloud guide
  - Render/Railway deployment steps
  - Environment variable configuration
- **Priority**: Medium

---

## V2 Requirements (Should-Have)

### Category: Advanced Features (ADV)

**ADV-001: Advanced Visualizations**
- **Description**: Sophisticated chart types and customization
- **Acceptance Criteria**:
  - Scatter plots, heatmaps, geospatial maps
  - Interactive Plotly dashboards
  - Chart customization options
  - Export charts as images

**ADV-002: Analytics Dashboard**
- **Description**: Query performance and usage analytics
- **Acceptance Criteria**:
  - Query volume trends
  - Error rate tracking
  - Popular queries
  - User engagement metrics

**ADV-003: Query Performance Optimization**
- **Description**: Automatic query optimization suggestions
- **Acceptance Criteria**:
  - Detects missing indexes
  - Suggests query rewrites
  - Execution plan analysis
  - Performance regression alerts

**ADV-004: Multi-Tenant Support**
- **Description**: Isolated environments for multiple organizations
- **Acceptance Criteria**:
  - Tenant-specific databases
  - User authentication per tenant
  - Usage quotas and billing
  - Tenant isolation verification

**ADV-005: Fine-Tuned Models**
- **Description**: Custom-trained models for domain-specific SQL
- **Acceptance Criteria**:
  - Fine-tuning pipeline for GPT-3.5/Llama
  - Domain-specific training data
  - Model performance comparison
  - Deployment of custom models

**ADV-006: Natural Language Explanations**
- **Description**: Explain SQL queries in plain language
- **Acceptance Criteria**:
  - Generates explanation for any SQL query
  - Highlights complex logic
  - Educational mode for learning SQL
  - Explanation quality scoring

**ADV-007: Query Templates**
- **Description**: Saved query patterns for common use cases
- **Acceptance Criteria**:
  - Template library (sales, analytics, reporting)
  - User-created custom templates
  - Template search and categorization
  - One-click template application

---

## Out of Scope

### Explicitly Not Included

**NoSQL Databases**
- Rationale: Focus on SQL-based systems; NoSQL requires different paradigm
- Future consideration: V3 if demand exists

**Mobile UI**
- Rationale: Desktop web interface sufficient for V1
- Future consideration: Progressive Web App for mobile

**Voice Input**
- Rationale: Text input is more precise for technical queries
- Future consideration: Voice-to-text preprocessing

**Slack/Teams Integration**
- Rationale: Standalone web app is sufficient
- Future consideration: V2 chatbot integration

**Advanced BI Features**
- Rationale: Focus on query generation, not full BI suite
- Examples: Scheduled reports, email alerts, collaboration

**Real-Time Query Streaming**
- Rationale: Batch query execution is sufficient
- Future consideration: WebSocket streaming for large results

**Custom Visualization Builder**
- Rationale: Streamlit native charts are sufficient
- Future consideration: Drag-and-drop chart builder

**User Management System**
- Rationale: Single-user or simple multi-session for V1
- Future consideration: Full auth/authz in multi-tenant V2

---

## Requirement Mapping to Implementation Plan

| Requirement Category | Implementation Plan Phase |
|---------------------|---------------------------|
| INFRA-001, INFRA-002 | Phase 1: Environment & Core DB Layer |
| DB-001, DB-002, DB-003 | Phase 1: Environment & Core DB Layer |
| GRAPH-001, GRAPH-002, GRAPH-003 | Phase 2: Agent Nodes & LangGraph Orchestration |
| AGENT-001 to AGENT-010 | Phase 2: Agent Nodes & LangGraph Orchestration |
| VECTOR-001, VECTOR-002 | Phase 2: Agent Nodes & LangGraph Orchestration |
| LLM-001, LLM-002, LLM-003 | Phase 2: Agent Nodes & LangGraph Orchestration |
| ERROR-001, ERROR-002 | Phase 2: Agent Nodes & LangGraph Orchestration |
| UI-001 to UI-005 | Phase 3: Frontend (Streamlit) |
| TEST-001, TEST-002, TEST-003 | Across all phases (continuous) |
| EVAL-001, EVAL-002 | Phase 3: Frontend (Streamlit) |
| SAFE-001, SAFE-002, SAFE-003 | Phase 1 & 2 (security first) |
| DOC-001, DOC-002, DOC-003 | Phase 4: Deployment Strategy |
| INFRA-003 | Phase 4: Deployment Strategy |

---

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| INFRA-001 | Phase 1 | Complete |
| INFRA-002 | Phase 1 | Complete |
| INFRA-003 | Phase 12 | Pending |
| DB-001 | Phase 2 | Complete |
| DB-002 | Phase 5 | Pending |
| DB-003 | Phase 2 | Pending |
| GRAPH-001 | Phase 1 | Complete |
| GRAPH-002 | Phase 2 | Complete |
| GRAPH-003 | Phase 9 | Complete |
| AGENT-001 | Phase 4 | Complete |
| AGENT-002 | Phase 3 | Complete |
| AGENT-003 | Phase 4 | Pending |
| AGENT-004 | Phase 4 | Pending |
| AGENT-005 | Phase 5 | Pending |
| AGENT-006 | Phase 6 | Pending |
| AGENT-007 | Phase 6 | Pending |
| AGENT-008 | Phase 4 | Pending |
| AGENT-009 | Phase 11 | Pending |
| AGENT-010 | Phase 5 | Pending |
| VECTOR-001 | Phase 3 | Complete |
| VECTOR-002 | Phase 3 | Complete |
| LLM-001 | Phase 7 | Pending |
| LLM-002 | Phase 7 | Pending |
| LLM-003 | Phase 4 | Complete |
| UI-001 | Phase 8 | Pending |
| UI-002 | Phase 8 | Pending |
| UI-003 | Phase 8 | Pending |
| UI-004 | Phase 8 | Pending |
| UI-005 | Phase 9 | Pending |
| ERROR-001 | Phase 6 | Pending |
| ERROR-002 | Phase 6 | Pending |
| TEST-001 | Phase 10 | Pending |
| TEST-002 | Phase 10 | Pending |
| TEST-003 | Phase 1 | Pending |
| EVAL-001 | Phase 11 | Pending |
| EVAL-002 | V2 | Deferred |
| SAFE-001 | Phase 5 | Pending |
| SAFE-002 | Phase 2 | Pending |
| SAFE-003 | Phase 1 | Pending |
| DOC-001 | Phase 12 | Pending |
| DOC-002 | Phase 12 | Pending |
| DOC-003 | Phase 12 | Pending |

---

## Success Metrics

**Accuracy**
- Target: 85%+ on Spider dev set
- Measurement: Exact match + execution accuracy

**Performance**
- Simple queries: <3s end-to-end
- Complex queries: <10s end-to-end
- Vector retrieval: <500ms

**Reliability**
- Self-correction success: 60%+ on first retry
- Zero destructive queries executed
- 99%+ uptime for hosted deployment

**Quality**
- Unit test coverage: 80%+
- Integration test pass rate: 100%
- Ragas score average: >0.75

**User Experience**
- Session persistence: 100% across page refreshes
- Error messages: Clear and actionable
- Debugging panel: All SQL visible and editable
