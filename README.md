# Text-to-SQL Agentic Pipeline

<div align="center">

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python)
![LangGraph](https://img.shields.io/badge/LangGraph-0.2%2B-green?style=flat-square)
![Streamlit](https://img.shields.io/badge/Streamlit-1.30%2B-red?style=flat-square&logo=streamlit)
![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)
![Tests](https://img.shields.io/badge/Tests-940%2B%20passing-brightgreen?style=flat-square)

**A production-grade multi-agent system that converts natural language questions into accurate SQL queries and executes them — with self-correction, human approval, and live visualizations.**

</div>

---

## Overview

Text-to-SQL Agentic Pipeline uses a **LangGraph multi-agent workflow** to break the SQL generation problem into specialized steps: understanding intent, retrieving relevant schema, planning the query, generating dialect-specific SQL, safely executing it, and self-correcting errors when they occur.

It supports **DuckDB, PostgreSQL, MySQL, and SQLite** out of the box and works with large databases (100+ tables) through semantic schema retrieval via **Pinecone**.

```
User Question  →  Gatekeeper  →  Schema Linker  →  Query Planner  →  SQL Generator
                                                                            |
                                                                       HITL Approval
                                                                            |
                                                                        Executor
                                                                       /        \
                                                                  Success      Failure
                                                                     |            |
                                                                  Formatter   Corrector (x2)
                                                                     |
                                                               Answer + Charts
```

---

## Features

- **Multi-agent orchestration** via LangGraph with cyclic state management
- **Semantic schema retrieval** — Pinecone vector search finds only the relevant tables, not the entire schema
- **Dialect-aware SQL generation** — PostgreSQL, MySQL, SQLite, DuckDB
- **Self-correction loop** — classifies errors against a 20-category taxonomy and retries up to 2 times
- **Human-in-the-Loop (HITL)** — optional approval gate before query execution
- **Safety scanner** — blocks destructive statements (DROP, DELETE, TRUNCATE, ALTER, etc.) at the statement level with no false positives on column names like `updated_at`
- **Groq + OpenAI fallback** — Llama-3.3-70B as primary, GPT-4o as fallback, with automatic switching on failure
- **Streamlit frontend** — chat interface, live debugging panel, Edit & Rerun SQL, auto-generated charts
- **Token + cost tracking** per query across providers
- **Evaluation** — Ragas scoring is planned (Phase 11) and not implemented yet

---

## Architecture

![Software Engineering Architecture & Code Flow Map](assets/architecture.png)

### Agent Nodes

| Node | Responsibility |
|---|---|
| **Gatekeeper** | Classifies the input: SQL query, conversational question, or follow-up |
| **Schema Linker** | Queries Pinecone to retrieve only the relevant tables and columns |
| **Query Planner** | Generates a Chain-of-Thought execution plan for complex queries |
| **SQL Generator** | Translates the plan into dialect-specific SQL |
| **HITL** | Interrupts graph for human review (configurable on/off) |
| **Executor** | Runs the SQL with timeout enforcement and auto `LIMIT 1000` injection |
| **Correction Plan** | Diagnoses SQL failures using the error taxonomy |
| **Correction SQL** | Rewrites the SQL based on the diagnosis (max 2 retries) |
| **Formatter** | Generates a natural language answer from the results |
| **Evaluator** | Placeholder for Ragas scoring (not implemented yet) |

### State (`AgentState`)

```python
{
    "messages":        [],   # Conversation history
    "user_query":      "",   # Original natural language question
    "resolved_query":  "",   # Clarified query after gatekeeper
    "schema":          {},   # Full database schema
    "relevant_tables": [],   # Pinecone-retrieved subset
    "query_plan":      "",   # Chain-of-Thought plan
    "generated_sql":   "",   # Current SQL draft
    "db_results":      [],   # Execution results
    "error_log":       "",   # Failure details
    "retry_count":     0,    # Correction attempts (max 2)
    "final_answer":    "",   # Natural language response
    "usage_metadata":  {}    # Token + cost tracking
}
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Agent orchestration | LangGraph 0.2+, LangChain 0.3+ |
| Primary LLM | Groq — Llama-3.3-70B Versatile |
| Fallback LLM | OpenAI — GPT-4o / GPT-4o-mini |
| Local LLM | Ollama — Qwen3:8B |
| Vector DB | Pinecone (serverless) + ChromaDB (local fallback) |
| Embeddings | Sentence Transformers |
| Databases | DuckDB, PostgreSQL, MySQL, SQLite |
| Frontend | Streamlit 1.30+ |
| Charts | Plotly |
| Evaluation | Ragas 0.2+ |
| Testing | pytest 9.0+ |

---

## Quick Start

### Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

### 1. Clone and install

```bash
git clone https://github.com/your-username/text-2-sql-agentic-pipeline.git
cd text-2-sql-agentic-pipeline

# Install core dependencies
uv pip install -e .

# Install with vector DB support
uv pip install -e ".[vector]"

# Install with PostgreSQL support
uv pip install -e ".[postgresql]"

# Install with MySQL support
uv pip install -e ".[mysql]"

# Install all extras including dev tools
uv pip install -e ".[vector,postgresql,mysql,dev]"
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
# LLM Providers
GROQ_API_KEY=your_groq_api_key
OPENAI_API_KEY=your_openai_api_key

# Vector DB (optional — for large schemas)
PINECONE_API_KEY=your_pinecone_api_key
PINECONE_INDEX_NAME=text2sql-schema

# Database (optional — defaults to bundled Chinook SQLite)
DATABASE_URL=postgresql://user:password@localhost:5432/mydb
```

### 3. Run the app

```bash
streamlit run streamlit_app/app.py
```

Open [http://localhost:8501](http://localhost:8501) in your browser.

---

## Usage

### Via Streamlit UI

1. Open the sidebar and enter your API keys (or load from `.env`)
2. Select your database type and connect
3. Type a natural language question in the chat box
4. Review the generated SQL in the debug panel
5. Approve execution (if HITL is enabled) or let it run automatically
6. See results as a table, natural language answer, and auto-generated chart

**Example questions:**
```
Show total sales by country for 2023
Which customers have placed more than 5 orders?
List the top 10 artists by total track count
What is the average invoice total per billing country?
```

### Via Python

```python
from graph.builder import compiled_graph

result = compiled_graph.invoke(
    {"user_query": "Show total sales by country"},
    config={"configurable": {"thread_id": "session-1"}}
)

print(result["final_answer"])
print(result["generated_sql"])
```

---

## Running as a service

The same pipeline can run as an HTTP API that several tenants share. Each tenant has its own API key, its own database connections and its own conversation threads. The Streamlit app does not need any of this.

### Environment variables

| Variable | Default | What it does |
|---|---|---|
| `T2S_SECRET_KEY` | none | Fernet key that encrypts stored database credentials. Required unless the metadata store is in memory. |
| `T2S_ADMIN_API_KEY` | none | Key for the `/v1/admin/*` routes. They are switched off while it is unset. |
| `T2S_METADATA_URL` | `sqlite:///.t2s/metadata.db` | SQLAlchemy URL for tenants, API keys and connections. |
| `T2S_CHECKPOINT_URL` | `memory://` | Where conversation state is kept: `memory://`, `sqlite:///path/to/file.db` or `postgresql://...`. |
| `T2S_REDIS_URL` | none | If set, rate limits and the cache are shared through Redis. Otherwise they live in the API process. |
| `T2S_RATE_LIMIT_FAIL_MODE` | `local` | What the rate limiter does when Redis is down: `local` keeps limiting inside each process, `open` lets everything through, `closed` rejects everything. |
| `T2S_RATE_LIMIT_PER_MINUTE` | `60` | Requests per minute for each tenant on the query endpoints. |
| `T2S_MAX_CONCURRENT_QUERIES` | `8` | Queries one tenant may have running at the same time. |
| `T2S_ALLOW_FILE_DATABASES` | `false` | Lets tenants register SQLite or DuckDB file paths. Leave it off unless you are the only tenant. |

Generate the two keys like this:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # T2S_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"                                 # T2S_ADMIN_API_KEY
```

If you lose `T2S_SECRET_KEY`, the stored database passwords cannot be read any more and the connections have to be registered again.

### Start it with Docker Compose

```bash
cp .env.example .env
# fill in T2S_SECRET_KEY, T2S_ADMIN_API_KEY, POSTGRES_PASSWORD and at least one LLM key
docker compose up --build
```

This starts three containers: the API on port 8000, PostgreSQL 16 for metadata and checkpoints, and Redis 7 for rate limiting and the cache. PostgreSQL data is kept in the `pgdata` volume. Check that the API is up:

```bash
curl http://localhost:8000/healthz
```

The image does not include the vector libraries, because they pull in PyTorch. Without them the planner gets the full schema instead of a retrieved subset, which is fine for small and medium schemas. To include them, build with `--build-arg EXTRAS=service,postgresql,mysql,vector`.

To run without Docker, install the extras and start uvicorn. With the defaults this uses a local SQLite file for metadata and keeps checkpoints in memory.

```bash
pip install -e ".[service,postgresql,mysql]"
export T2S_SECRET_KEY=... T2S_ADMIN_API_KEY=...
uvicorn api.main:app --port 8000
```

### Create a tenant and an API key

```bash
curl -s -X POST http://localhost:8000/v1/admin/tenants \
  -H "X-Admin-Key: $T2S_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "acme"}'
```

The response contains the tenant and its first key under `api_key.key`. The key is shown once, so store it now. The examples below assume it is in `$API_KEY`.

### Register a database

```bash
curl -s -X POST http://localhost:8000/v1/connections \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "name": "sales",
        "db_type": "postgresql",
        "params": {"host": "db.example.com", "port": 5432, "database": "sales",
                   "user": "readonly_user", "password": "..."}
      }'
```

The API connects once to test the parameters and returns the connection with its `id`. The password is stored encrypted and is never returned. Use a database user that can only read.

Hosts on private or loopback addresses are refused by default, so a tenant cannot point the service at your internal network. If the database you want to query is in the same Compose network or on your own machine, set `T2S_ALLOW_PRIVATE_HOSTS=true`.

### Ask a question

```bash
curl -s -X POST http://localhost:8000/v1/query \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"connection_id": "<id from the previous step>", "question": "Top 5 customers by revenue"}'
```

The response has `status`, `answer`, `sql`, `rows` and a `thread_id`. Send the same `thread_id` with the next question to continue the conversation.

`POST /v1/query/stream` takes the same body and returns server-sent events, so a client can show progress while the query runs:

```bash
curl -N -X POST http://localhost:8000/v1/query/stream \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"connection_id": "<id>", "question": "Monthly revenue in 2024"}'
```

### Approve SQL before it runs

Add `"require_approval": true` to a query and the run stops before execution. The response then has `status: "awaiting_approval"` and the SQL under `pending_approval`. Decide with:

```bash
curl -s -X POST http://localhost:8000/v1/threads/<thread_id>/approval \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"decision": "approve"}'
```

`decision` is `approve`, `reject` or `edit`. With `edit`, send the corrected statement in `sql`. Pending approvals survive a restart only if `T2S_CHECKPOINT_URL` points at SQLite or PostgreSQL.

---

## Configuration

All settings live in `config/config.yaml`:

```yaml
database:
  default_type: duckdb          # duckdb | sqlite | postgresql | mysql
  default_path: data/chinook.db
  query_timeout: 60             # seconds

llm:
  primary_provider: groq
  groq_model: llama-3.3-70b-versatile
  fallback_provider: openai
  openai_model_default: gpt-4o-mini
  openai_model_complex: gpt-4o

app:
  max_correction_retries: 3

hitl:
  enabled: false                # set true to require approval before execution
  auto_approve_simple: true     # auto-approve single-table SELECT queries
```

---

## Project Structure

```
text-2-sql-agentic-pipeline/
├── agents/
│   └── nodes/               # 10 LangGraph agent node implementations
│       ├── gatekeeper.py
│       ├── schema_linker.py
│       ├── query_planner.py
│       ├── sql_generator.py
│       ├── hitl.py
│       ├── executor.py
│       ├── correction_plan.py
│       ├── correction_sql.py
│       ├── formatter.py
│       └── evaluator.py
├── api/                     # FastAPI service (multi-tenant HTTP API)
│   ├── main.py              # create_app() and the `app` uvicorn serves
│   ├── auth.py              # Tenants and API keys
│   ├── settings.py          # T2S_* settings
│   └── routes/              # admin, connections, query, health
├── graph/
│   ├── state.py             # AgentState TypedDict
│   ├── builder.py           # StateGraph compilation
│   ├── checkpointing.py     # Checkpointer from T2S_CHECKPOINT_URL (memory, SQLite, PostgreSQL)
│   ├── resilience.py        # Node wrapper for LLM outages and usage tracking
│   └── conditions.py        # Conditional routing functions
├── infra/
│   ├── ratelimit.py         # Rate limiter (in-process or Redis)
│   └── cache.py             # TTL cache (in-process or Redis)
├── database/
│   ├── manager.py           # Unified DB facade + schema extraction
│   ├── registry.py          # Per-tenant connection registry
│   ├── credentials.py       # Encryption of stored database credentials
│   ├── metadata.py          # Engine for the metadata store
│   ├── safety.py            # Destructive statement scanner
│   ├── schema_utils.py      # Schema introspection helpers
│   └── connectors/          # DuckDB, SQLite, PostgreSQL, MySQL
├── llm/
│   ├── fallback.py          # FallbackClient (Groq → OpenAI)
│   ├── context.py           # Per-request provider keys
│   ├── groq_client.py
│   ├── openai_client.py
│   └── usage_tracker.py     # Token + cost tracking
├── vector/
│   ├── embeddings.py        # Schema embedding generation
│   ├── retriever.py         # Pinecone + ChromaDB retrieval
│   ├── namespace.py         # Vector namespace per connection and schema version
│   └── schema_graph.py      # FK relationship graph for JOIN hints
├── streamlit_app/
│   ├── app.py               # Main Streamlit entry point
│   └── components/
│       ├── sidebar.py       # API keys, DB config, HITL toggle
│       ├── chat.py          # Chat interface with streaming
│       ├── debug_panel.py   # SQL output, CoT plan, retry logs
│       └── charts.py        # Auto-generated Plotly charts
├── config/
│   ├── config.yaml          # Main configuration
│   ├── error-taxonomy.json  # 20 SQL error categories
│   ├── safety_config.yaml   # Blocked SQL statement types
│   └── pinecone_config.yaml # Vector index configuration
├── data/
│   └── chinook.db           # Bundled sample database (music store)
├── tests/                   # 940+ tests across all modules
│   ├── agents/
│   ├── api/
│   ├── database/
│   ├── graph/
│   ├── infra/
│   ├── llm/
│   ├── safety/
│   ├── vector/
│   └── ui/
├── utils/
│   └── error_parser.py      # PostgreSQL/MySQL error parsers
├── docs/
│   └── phase2-contracts.md  # Interfaces between the service modules
├── Dockerfile
├── docker-compose.yml       # API + PostgreSQL + Redis
├── .env.example
└── pyproject.toml
```

---

## Running Tests

```bash
# Run full test suite
uv run pytest

# Run a specific module
uv run pytest tests/agents/
uv run pytest tests/safety/

# Run with coverage
uv run pytest --cov=. --cov-report=html
```

---

## Safety

The pipeline enforces multiple layers of protection against destructive queries:

1. **Statement-level scanner** — blocks `DROP`, `DELETE`, `TRUNCATE`, `UPDATE`, `INSERT`, `ALTER`, `CREATE`, `GRANT`, `REVOKE` at parse time, with no false positives on column names like `updated_at` or `delete_flag`
2. **Read-only DB connections** — connectors open connections in read-only mode where the driver supports it
3. **Human-in-the-Loop gate** — optionally require explicit approval before any query runs
4. **Auto LIMIT injection** — queries without a `LIMIT` clause automatically get `LIMIT 1000`
5. **Execution timeout** — 60-second hard timeout on all queries (configurable)

---

## Roadmap

| Phase | Status |
|---|---|
| 1 — Foundation & Core Infrastructure | Complete |
| 2 — AgentState & LangGraph Skeleton | Complete |
| 3 — Vector Schema Retrieval (Pinecone) | Complete |
| 4 — Specialized Agent Nodes | Complete |
| 5 — Execution & Safety Layer | Complete |
| 6 — Error Correction Loop | Complete |
| 7 — LLM Integration & Fallback | Complete |
| 8 — Streamlit Frontend | In Progress (4/5) |
| 9 — Memory & History | Planned |
| 10 — Testing & Quality | Planned |
| 11 — Evaluation (Ragas) | Planned |
| 12 — Deployment & Documentation | Planned |

---

## Supported Databases

| Database | Connection | Status |
|---|---|---|
| DuckDB | Local file / in-memory | Supported |
| SQLite | Local file | Supported |
| PostgreSQL | `postgresql://user:pass@host:port/db` | Supported |
| MySQL | `mysql://user:pass@host:port/db` | Supported |

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

<div align="center">
Built with LangGraph · Groq · Pinecone · Streamlit
</div>
