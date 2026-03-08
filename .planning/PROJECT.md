# Project: Agentic Text-to-SQL Pipeline

## Vision

Build a production-grade, agentic Text-to-SQL system that converts natural language questions into accurate SQL queries and executes them against multiple database backends. The system uses LangGraph's multi-agent orchestration combined with SQL-of-Thought decomposition to handle complex queries involving multi-table joins, aggregations, subqueries, CTEs, and window functions.

## What We're Building

An intelligent SQL generation pipeline that:
- Accepts natural language questions in a conversational interface
- Understands database schemas automatically through vector-based retrieval
- Plans and generates dialect-specific SQL through specialized agents
- Self-corrects errors through a feedback loop with error taxonomy
- Provides human-in-the-loop approval for query validation
- Executes queries safely with read-only access
- Formats results with natural language explanations and visualizations
- Tracks accuracy through ragas evaluation
- Maintains conversation context across sessions
- Stores query history and analytics

## Why This Matters

Current text-to-SQL solutions struggle with:
- Complex multi-step queries requiring planning
- Large database schemas (hundreds of tables)
- Error recovery and self-correction
- Transparency in SQL generation process
- Production-grade safety and validation

This system solves these by combining:
- **LangGraph** for cyclic state management and agent orchestration
- **Multi-Agent Architecture** with specialized roles (Schema Linking, Query Planning, SQL Generation, Correction)
- **Vector Retrieval** (Pinecone) for semantic schema filtering
- **Error Taxonomy** for intelligent self-correction
- **Human-in-the-Loop** for quality assurance
- **Ragas Evaluation** for accuracy scoring

## Core Capabilities

### 1. Multi-Agent LangGraph Workflow

**Specialized Agents:**
- **Gatekeeper Node**: Validates queries, handles conversational questions, detects follow-ups
- **Schema Linking Node**: Uses Pinecone vector DB to retrieve only relevant tables/columns
- **Query Planning Node**: Creates Chain-of-Thought execution plan for complex queries
- **SQL Generation Agent**: Translates plans to dialect-specific SQL (PostgreSQL, MySQL, SQLite, DuckDB)
- **Quality & Validation Stage**: Human-in-the-loop approval with SQL explanation
- **Execution & Safety Node**: Scans for destructive operations, executes with read-only access
- **Correction Plan Agent**: Analyzes failures against error-taxonomy.json (20 categories)
- **Correction SQL Agent**: Modifies SQL based on diagnostic feedback (max 2 retries)
- **Format Answer & Visuals Node**: Generates natural language responses and chart flags
- **Evaluation Node**: Scores accuracy using ragas (0.0-1.0 confidence)

**State Management (AgentState):**
```python
{
    "messages": [],           # Chat history with LangChain structured messages
    "user_query": "",         # Original natural language question
    "schema": {},             # Full database schema context
    "relevant_tables": [],    # Vector-retrieved subset of tables
    "query_plan": "",         # Chain-of-Thought execution plan
    "generated_sql": "",      # Current drafted SQL
    "db_results": [],         # Execution results
    "error_log": "",          # SQL execution failure details
    "retry_count": 0,         # Correction attempt counter (max 2)
    "final_answer": ""        # Formatted natural language response
}
```

### 2. Database Support

**Multi-Database Backend:**
- **DuckDB**: Fast local analytical queries, prototyping
- **MySQL/PostgreSQL**: Remote production databases
- **SQLite**: Local file-based databases

**DatabaseManager Features:**
- Automatic schema extraction (tables, columns, types, primary keys, sample rows)
- Read-only user connections for safety
- Dialect-specific SQL generation
- Connection pooling and error handling

### 3. LLM Configuration

**Primary Provider:** Groq API (Llama-3 70B / Mixtral)
- High-speed, low-cost generation
- Real-time query processing

**Fallback Provider:** OpenAI (gpt-4o-mini / gpt-4o)
- Used when Groq fails or for complex planning tasks
- Configurable fallback strategy

**Future Support:** Anthropic Claude (for advanced reasoning)

### 4. Memory Architecture

**Short-Term Memory:**
- In-session conversation context
- Current query execution state
- Active correction loops

**Long-Term Memory:**
- Query history storage (NL question → SQL → results)
- Analytics aggregation (common patterns, accuracy trends)
- User preferences and database configurations
- Thread-based session isolation (LangGraph MemorySaver)

### 5. Safety & Validation

**Pre-Execution Safety:**
- Destructive keyword scanning (DROP, DELETE, TRUNCATE, ALTER)
- Read-only database user enforcement
- Human-in-the-loop approval gate

**Post-Execution Validation:**
- Error taxonomy-based diagnostics
- Automatic retry with corrections (max 2 attempts)
- Ragas accuracy scoring

### 6. Frontend (Streamlit)

**Configuration Sidebar:**
- API key management (Groq, OpenAI, Pinecone)
- Database credentials and type selector
- Human-in-the-loop toggle
- Token/cost usage tracker

**Chat Interface:**
- Natural language query input
- Conversational follow-up support
- Message history with context

**Interactive Debugging Panel:**
- Chain-of-Thought query plan display
- Generated SQL output with syntax highlighting
- Retry logs and error messages
- "Edit & Rerun SQL" manual override
- Ragas confidence score display

**Dynamic Dashboards:**
- Auto-generated charts for time-series/categorical data
- Query history analytics
- Accuracy metrics over time

## Technology Stack

**Core Framework:**
- `langgraph` - Agent orchestration, state management, cyclic workflows
- `langchain` - LLM abstraction, message handling, prompt templates

**Frontend:**
- `streamlit` - Web UI, interactive debugging, visualizations

**LLM Providers:**
- `groq` - Primary generation (Llama-3 70B, Mixtral)
- `openai` - Fallback provider (gpt-4o-mini, gpt-4o)

**Database Layer:**
- `duckdb` - Local analytical database
- `mysql-connector-python` - MySQL connections
- `psycopg2` - PostgreSQL connections
- `sqlite3` - SQLite connections

**Vector Database:**
- `pinecone-client` - Semantic schema retrieval

**Evaluation:**
- `ragas` - Query accuracy scoring

**Testing:**
- `pytest` - Unit tests for each agent node
- `pytest-asyncio` - Async graph testing
- Sample databases for integration tests

**Deployment:**
- `docker` / `docker-compose` - Containerization
- Streamlit Community Cloud (fast prototyping)
- Render / Railway / AWS Fargate (production)

## Success Criteria

**Accuracy:**
- 85%+ accuracy on standard text-to-SQL benchmarks (Spider, WikiSQL)
- 90%+ accuracy on simple queries (single table, basic filters)
- 70%+ accuracy on complex queries (multi-join, aggregations, CTEs)

**Performance:**
- <3 seconds for simple query end-to-end (NL → SQL → results)
- <10 seconds for complex queries requiring planning
- Schema retrieval <500ms with Pinecone

**Reliability:**
- Self-correction success rate >60% on first retry
- Zero destructive queries executed in production
- 99%+ uptime for deployed service

**User Experience:**
- Conversational follow-up context maintained across 5+ turns
- Human-in-the-loop approval <10 seconds review time
- Debugging panel provides actionable insights for 100% of failures

**Developer Experience:**
- Unit test coverage >80% for all agent nodes
- Integration tests cover all database backends
- Docker deployment works out-of-box
- Clear documentation for extending to new databases/LLMs

## Out of Scope (Future Enhancements)

**V1 Exclusions:**
- Multi-user authentication and authorization
- Query result caching layer
- Advanced visualizations (custom dashboards, BI-style reports)
- Support for NoSQL databases (MongoDB, Cassandra)
- Fine-tuned LLM models for domain-specific SQL
- Automated schema documentation generation
- Natural language query suggestions/autocomplete
- Scheduled/automated query execution
- Export to Excel/CSV/PDF
- Mobile-responsive UI

**V2 Potential Features:**
- Multi-tenant isolation with user workspaces
- Query performance optimization suggestions
- Data lineage tracking
- Integration with dbt for transformation workflows
- Support for streaming databases (Kafka, Flink)
- Voice input for queries
- Slack/Teams integration for collaborative querying
- Cost estimation for expensive queries
- Automatic index recommendations

## Project Structure

```
text-2-sql-agentic-pipeline/
├── src/
│   ├── agents/              # LangGraph agent nodes
│   │   ├── gatekeeper.py
│   │   ├── schema_linker.py
│   │   ├── query_planner.py
│   │   ├── sql_generator.py
│   │   ├── executor.py
│   │   ├── corrector.py
│   │   └── formatter.py
│   ├── database/            # Database management
│   │   ├── manager.py
│   │   ├── connectors/
│   │   └── schema_extractor.py
│   ├── graph/               # LangGraph workflow
│   │   ├── state.py
│   │   ├── graph_builder.py
│   │   └── conditions.py
│   ├── llm/                 # LLM provider abstraction
│   │   ├── groq_client.py
│   │   ├── openai_client.py
│   │   └── fallback.py
│   ├── vector/              # Pinecone schema retrieval
│   │   ├── embeddings.py
│   │   └── retriever.py
│   ├── evaluation/          # Ragas integration
│   │   └── evaluator.py
│   ├── memory/              # Session & history management
│   │   ├── session.py
│   │   └── history.py
│   └── utils/
│       ├── error_taxonomy.json
│       └── prompts/
├── streamlit_app/           # Frontend UI
│   ├── app.py
│   ├── components/
│   └── styles/
├── tests/                   # Unit & integration tests
│   ├── agents/
│   ├── database/
│   └── integration/
├── data/                    # Sample databases
│   ├── chinook.db
│   └── retail_sample.duckdb
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── .env.example
├── requirements.txt
└── README.md
```

## Development Phases

### Phase 1: Foundation & Core Infrastructure
- Python environment setup with dependencies
- AgentState schema definition
- DatabaseManager implementation (DuckDB, MySQL, SQLite)
- Basic LangGraph skeleton with placeholder nodes
- Sample database setup (Chinook/retail)

### Phase 2: Agent Nodes & LangGraph Orchestration
- Implement all specialized agent nodes
- Draft agent prompts for each role
- Build error-taxonomy.json with 20 SQL error categories
- Compile full LangGraph with conditional edges
- Implement correction loop logic (max 2 retries)
- Add MemorySaver for multi-session support

### Phase 3: Vector Schema Retrieval
- Pinecone integration for semantic search
- Automatic schema embedding on database connection
- Schema Linking Node retrieval implementation
- Optimize for large schemas (100+ tables)

### Phase 4: LLM Integration & Fallback
- Groq API client implementation
- OpenAI fallback strategy
- Prompt template system
- Token usage tracking
- Cost calculation

### Phase 5: Streamlit Frontend
- Configuration sidebar with API key inputs
- Chat interface with message history
- Interactive debugging panel
- SQL editing and manual rerun
- Dynamic chart generation
- Ragas score display

### Phase 6: Memory & History
- Short-term memory (in-session context)
- Long-term memory (query history storage)
- Analytics dashboard (query patterns, accuracy trends)
- Thread-based session isolation

### Phase 7: Safety & Validation
- Destructive keyword scanning
- Read-only database user enforcement
- Human-in-the-loop approval gate
- Ragas evaluation integration

### Phase 8: Testing & Quality
- Unit tests for each agent node (pytest)
- Integration tests with sample databases
- Golden dataset testing (known NL → SQL pairs)
- Error correction loop testing
- End-to-end workflow testing

### Phase 9: Deployment
- Docker containerization
- Docker Compose setup with bundled database
- Environment variable management
- Streamlit Community Cloud deployment guide
- Production deployment guide (Render/Railway/AWS)

### Phase 10: Documentation & Polish
- README with quickstart guide
- Agent architecture documentation
- Database connection guide
- Deployment instructions
- API key setup guide
- Troubleshooting guide

## Key Decisions

**Architecture:**
- LangGraph over pure LangChain for cyclic workflows and state management
- Multi-agent specialization over monolithic SQL generation
- Vector retrieval over full schema context to handle large databases

**LLM Strategy:**
- Groq primary for speed/cost optimization
- OpenAI fallback for reliability on complex queries
- Configurable provider to support future models

**Database Design:**
- Extensible DatabaseManager pattern for easy addition of new databases
- Read-only connections enforced at connection level
- Automatic schema extraction to minimize manual configuration

**Memory:**
- LangGraph MemorySaver for thread isolation
- Separate short-term (session) and long-term (history) storage
- In-memory for V1 with database persistence in V2

**Safety:**
- Human-in-the-loop required by default, configurable toggle
- Multi-layer safety: keyword scan + read-only user + approval gate
- Error taxonomy enables intelligent self-correction vs. fast failure

**Frontend:**
- Streamlit for rapid development and built-in components
- Debugging panel for transparency and developer trust
- Chart auto-generation for common data patterns

**Deployment:**
- Docker-first for consistency across environments
- Multiple deployment paths (local, cloud, self-hosted)
- Environment-based configuration for secrets management

## Risk Mitigation

**LLM Accuracy:**
- Multi-agent decomposition improves reliability over single-pass
- Error taxonomy enables targeted corrections
- Human-in-the-loop provides safety net
- Ragas evaluation provides objective accuracy metrics

**Performance:**
- Vector retrieval reduces context size for faster generation
- Groq API provides sub-second LLM responses
- DuckDB enables fast local prototyping

**Security:**
- Read-only database users prevent destructive operations
- Keyword scanning catches malicious queries
- Human approval required before execution

**Scalability:**
- Thread-based session isolation supports multiple users
- Pinecone handles millions of schema embeddings
- Docker enables horizontal scaling

**Maintenance:**
- Unit tests ensure agent reliability
- Golden dataset testing catches regressions
- Modular architecture enables independent component updates
