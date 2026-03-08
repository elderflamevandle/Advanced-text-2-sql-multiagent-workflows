# Phase 1 Context: Foundation & Core Infrastructure

**Phase Goal:** Establish Python environment, project structure, and basic database connectivity

**Created:** 2025-03-08
**Discussed with:** User

---

## Implementation Decisions

### 1. Project Structure Organization

**Decision:** Separate top-level directories (no `src/` wrapper)

**Structure:**
```
text-2-sql-agentic-pipeline/
├── agents/                 # LangGraph agent nodes
├── database/               # Database layer
│   ├── connectors/        # Dialect-specific connectors (nested)
│   ├── manager.py         # DatabaseManager class (short name)
│   └── schema_utils.py    # Schema extraction utilities (descriptive)
├── graph/                  # LangGraph state and graph builder
├── llm/                    # LLM provider clients
├── vector/                 # Pinecone schema retrieval
├── evaluation/             # Ragas integration
├── memory/                 # Session and history management
├── utils/                  # Shared utilities
├── streamlit_app/          # Streamlit UI (separate from backend)
├── tests/                  # Test suite
├── config/                 # Configuration files location
│   ├── config.yaml
│   └── error-taxonomy.json
├── data/                   # Sample databases
│   └── chinook.db
├── .env.example            # Environment template
├── pyproject.toml          # Package definition with optional deps
└── README.md
```

**Rationale:**
- Clean separation between backend (agents/, database/, etc.) and frontend (streamlit_app/)
- Nested subdirectories for logical grouping (connectors/, nodes/)
- Mixed naming: modules use short names matching classes, utilities use descriptive names

**Module Depth:** Use nested subdirectories (e.g., `database/connectors/`) for logical grouping

**File Naming Convention:**
- **Modules containing main classes:** Short names matching the class
  - `database/manager.py` → contains `DatabaseManager`
  - `graph/builder.py` → contains `GraphBuilder`
- **Utility/helper files:** Descriptive names
  - `database/schema_utils.py` → schema extraction helpers
  - `utils/prompts/` → prompt templates

**Configuration Files:** Live in `/config` directory (organized, avoids root clutter)

---

### 2. Dependency Management

**Decision:** Use `pyproject.toml` with optional dependency groups

**Version Pinning Strategy:** Compatible ranges (e.g., `langchain>=0.1.0,<0.2.0`)
- Allows patch/minor updates
- Prevents breaking major version changes
- Users can generate lockfile with `pip freeze > requirements-lock.txt` if needed

**Dev Dependencies Separation:** Use `pyproject.toml` with `[project.optional-dependencies]`

**Example `pyproject.toml` structure:**
```toml
[project]
name = "text2sql-agentic-pipeline"
version = "1.0.0"
requires-python = ">=3.10"
dependencies = [
    "langchain>=0.2.0,<0.3.0",
    "langgraph>=0.1.0,<0.2.0",
    "streamlit>=1.30.0,<2.0.0",
    "duckdb>=0.10.0,<1.0.0",
    "ragas>=0.1.0,<0.2.0",
    "pinecone-client>=3.0.0,<4.0.0",
    "groq>=0.4.0,<1.0.0",
    "openai>=1.0.0,<2.0.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0.0", "black>=24.0.0", "ruff>=0.2.0"]
mysql = ["mysql-connector-python>=8.0.0,<9.0.0"]
postgresql = ["psycopg2-binary>=2.9.0,<3.0.0"]
```

**Installation patterns:**
- Basic install: `pip install -e .`
- With dev tools: `pip install -e ".[dev]"`
- With MySQL support: `pip install -e ".[mysql]"`
- With all databases: `pip install -e ".[dev,mysql,postgresql]"`

**Python Version:** Minimum Python 3.10+ (good balance of compatibility and modern features)

**Rationale:**
- Compatible ranges allow security patches without manual updates
- Optional extras avoid forcing large database drivers on all users
- `pyproject.toml` is modern Python standard (PEP 621)

---

### 3. DatabaseManager Design

**Decision:** Global singleton connection pool with smart caching and retry logic

**Connection Pooling Strategy:** Global singleton pool per database type
- One shared pool for all DuckDB connections
- One shared pool for all MySQL connections
- Etc.
- Initialized lazily on first use
- Thread-safe access

**Implementation Pattern:**
```python
class ConnectionPoolManager:
    _pools = {}  # {db_type: pool}
    _lock = threading.Lock()

    @classmethod
    def get_pool(cls, db_type: str, **kwargs):
        # Singleton pattern with lazy initialization
```

**Schema Caching Approach:** On-demand refresh with indefinite caching
- Cache schema in memory after first extraction
- Cache persists until manual `refresh_schema()` call
- No TTL-based expiration (schema changes are rare in production)
- Schema includes: tables, columns, types, PKs, FKs, sample rows

**Cache invalidation:**
- Manual: `db_manager.refresh_schema()`
- Automatic: After schema-altering operations (if detected)

**Error Handling Philosophy:** Retry with exponential backoff (3 attempts)
- Transient network errors → retry
- Authentication/permission errors → fail immediately
- Syntax errors → fail immediately (no retry)

**Retry configuration:**
```python
max_retries = 3
backoff_factor = 2  # Wait 1s, 2s, 4s
retry_on = (ConnectionError, TimeoutError, OperationalError)
```

**Sample Row Count:** 2 rows per table
- Minimal but sufficient for understanding column types and data patterns
- Keeps schema payload small for large databases
- Fetched during initial schema extraction

**Rationale:**
- Global singleton avoids connection exhaustion with many concurrent queries
- Indefinite caching optimized for production (schemas rarely change)
- Retry logic handles transient failures gracefully
- 2 sample rows balance schema understanding vs performance

---

### 4. Configuration Management

**Decision:** Flat `.env` file with sensible defaults and visible warnings

**Environment Variable Structure:** Flat with prefixes (simple, widely supported)

**Example `.env` structure:**
```env
# LLM Configuration
GROQ_API_KEY=your_groq_key_here
OPENAI_API_KEY=your_openai_key_here

# Vector Database
PINECONE_API_KEY=your_pinecone_key_here
PINECONE_ENVIRONMENT=us-west1-gcp

# Database Connection (defaults to local DuckDB if not specified)
DB_TYPE=duckdb
DB_HOST=localhost
DB_PORT=5432
DB_NAME=sample_db
DB_USER=readonly_user
DB_PASSWORD=
DB_TIMEOUT=30

# Application Settings
MAX_RETRIES=3
SCHEMA_CACHE_ENABLED=true
HUMAN_IN_LOOP_ENABLED=true
```

**Secrets Management Approach:** Local `.env` only for Phase 1
- Simple file-based configuration
- Suitable for development and prototyping
- `.env` file in `.gitignore` (never committed)
- `.env.example` checked into git as template
- Future phases can add cloud secrets support (AWS Secrets Manager, etc.)

**Default Values Philosophy:** Sensible defaults for everything
- **LLM:** Default to Groq if only one key provided
- **Database:** Default to DuckDB with `data/chinook.db`
- **Timeouts:** 30s database query timeout, 60s LLM timeout
- **Retries:** 3 attempts with exponential backoff
- **Features:** Human-in-loop enabled by default

**Configuration Validation:** Best-effort with visible warnings
- App starts even if configuration is suboptimal
- Log WARNING when defaults are used: `"No GROQ_API_KEY found, using OPENAI_API_KEY as fallback"`
- Log INFO when defaults applied: `"DB_TYPE not specified, defaulting to DuckDB (data/chinook.db)"`
- Display warnings in Streamlit UI sidebar (orange badge with warning icon)

**Warning Display Strategy:**
```python
# Console logging
logger.warning("⚠ GROQ_API_KEY not set, using OpenAI fallback")
logger.info("ℹ DB_TYPE not specified, defaulting to DuckDB")

# Streamlit UI
if config.using_defaults:
    st.sidebar.warning("⚠ Using default configuration")
    with st.sidebar.expander("Configuration Warnings"):
        for warning in config.warnings:
            st.write(f"• {warning}")
```

**Rationale:**
- Flat structure is simple and widely compatible with deployment platforms
- Sensible defaults enable "just works" experience for local development
- Visible warnings educate users about configuration without blocking progress
- Local `.env` sufficient for Phase 1 (cloud secrets add complexity)

---

## Code Context

**Existing Assets:** None (greenfield project)

**Reusable Patterns:** N/A (Phase 1 establishes patterns)

---

## Deferred Decisions

These were explicitly noted as out of scope for Phase 1:

- **Cloud secrets management** - AWS Secrets Manager, Azure Key Vault integration (deferred to deployment phase)
- **Advanced connection pooling** - Per-query connection limits, pool monitoring (add if needed)
- **Configuration UI** - Web-based configuration editor (Streamlit settings panel in later phase)
- **Multi-environment configs** - Separate dev/staging/prod config files (add when deploying)

---

## Success Criteria (Phase 1 Specific)

Based on ROADMAP.md, Phase 1 is complete when:

✓ Virtual environment with all dependencies installed (via `pyproject.toml`)
✓ Clean modular directory structure (separate top-level dirs, nested connectors)
✓ DatabaseManager connects to DuckDB, MySQL, PostgreSQL, SQLite (with global pooling)
✓ Schema introspection fetches tables, columns, types, PKs, FKs (with caching)
✓ Sample database (Chinook) included and accessible (2 rows per table)
✓ `.env.example` created for secrets management (flat structure with defaults)
✓ Basic smoke test: connect to sample DB and fetch schema (with retry logic)

---

## Next Steps

With this context captured, proceed to:

1. **Plan Phase 1** - `/gsd:plan-phase 1` will use these decisions to create detailed PLAN.md
2. **Execute Phase 1** - `/gsd:execute-phase 1` will implement following this structure

These decisions will guide all downstream planning and implementation.

---

*Context captured: 2025-03-08*
*All 4 gray areas discussed and locked*
