# Text-to-SQL Agentic Pipeline

<div align="center">

![Python](https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python)
![LangGraph](https://img.shields.io/badge/LangGraph-0.2%2B-green?style=flat-square)
![Streamlit](https://img.shields.io/badge/Streamlit-1.30%2B-red?style=flat-square&logo=streamlit)
![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)
![Tests](https://img.shields.io/badge/Tests-191%2B%20passing-brightgreen?style=flat-square)

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
- **Ragas evaluation** — confidence score (0.0–1.0) returned for every query

---

## Architecture

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
| **Evaluator** | Scores the answer accuracy using Ragas |

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
├── graph/
│   ├── state.py             # AgentState TypedDict
│   ├── builder.py           # StateGraph compilation
│   └── conditions.py        # Conditional routing functions
├── database/
│   ├── manager.py           # Unified DB facade + schema extraction
│   ├── safety.py            # Destructive statement scanner
│   ├── schema_utils.py      # Schema introspection helpers
│   └── connectors/          # DuckDB, SQLite, PostgreSQL, MySQL
├── llm/
│   ├── fallback.py          # FallbackClient (Groq → OpenAI)
│   ├── groq_client.py
│   ├── openai_client.py
│   └── usage_tracker.py     # Token + cost tracking
├── vector/
│   ├── embeddings.py        # Schema embedding generation
│   ├── retriever.py         # Pinecone + ChromaDB retrieval
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
├── tests/                   # 191+ tests across all modules
│   ├── agents/
│   ├── database/
│   ├── graph/
│   ├── llm/
│   ├── safety/
│   ├── vector/
│   └── ui/
├── utils/
│   └── error_parser.py      # PostgreSQL/MySQL error parsers
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
