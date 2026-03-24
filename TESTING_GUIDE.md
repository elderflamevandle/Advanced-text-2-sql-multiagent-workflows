# Testing Guide — Text-2-SQL Agentic Pipeline

## Database Support

The pipeline supports 4 databases — pick one:

| DB | When to use | Install |
|----|-------------|---------|
| **DuckDB** | Default, zero-config, analytical queries | Built-in |
| **SQLite** | Lightweight, existing `.db` files | Built-in |
| **PostgreSQL** | Production, shared team DB | `pip install -e '.[postgresql]'` |
| **MySQL** | Existing MySQL server | `pip install -e '.[mysql]'` |

Set `DB_TYPE` in your `.env` (copy from `.env.example`).

---

## Quickest Way to Test: Use the Built-in Sample DB

There is already a `data/chinook.db` (the classic Chinook music store dataset — DuckDB format). It has:
- 11 tables: `Artist`, `Album`, `Track`, `Invoice`, `Customer`, `Employee`, etc.
- ~400 artists, ~350 albums, ~3,500 tracks, realistic foreign keys

**To test immediately:**
1. Launch the app: `streamlit run streamlit_app/app.py`
2. In the sidebar — leave DB path as `data/chinook.db`, type `duckdb`
3. Try queries like:
   - *"Who are the top 5 artists by total sales?"*
   - *"List all tracks in the Jazz genre"*
   - *"Which customers spent more than $40?"*

---

## Bringing Your Own Data

### Option A — DuckDB (easiest, no server needed)

```python
import duckdb

con = duckdb.connect("data/my_database.duckdb")

# Load from CSV
con.execute("CREATE TABLE sales AS SELECT * FROM read_csv_auto('sales.csv')")

# Load from Parquet
con.execute("CREATE TABLE products AS SELECT * FROM read_parquet('products.parquet')")

# Load from Pandas DataFrame
import pandas as pd
df = pd.read_csv("orders.csv")
con.register("orders_df", df)
con.execute("CREATE TABLE orders AS SELECT * FROM orders_df")

con.close()
```

Then point the sidebar to `data/my_database.duckdb`.

### Option B — SQLite

```python
import sqlite3, pandas as pd

con = sqlite3.connect("data/my_database.db")
df = pd.read_csv("sales.csv")
df.to_sql("sales", con, if_exists="replace", index=False)
con.close()
```

Then point the sidebar to `data/my_database.db`, type `sqlite`.

### Option C — PostgreSQL / MySQL

Fill in the sidebar fields (host, port, user, password, dbname) — the system connects directly to your existing server. No ingestion needed.

---

## Data Dictionary (How the Pipeline Understands Your Schema)

You do not create a manual data dictionary — the system **auto-introspects** your database. Here is what it extracts per table:

```
Table: Invoice
  Columns: InvoiceId (INTEGER, PK), CustomerId (INTEGER, FK→Customer),
           InvoiceDate (TEXT), Total (REAL)
  Foreign Keys: CustomerId → Customer.CustomerId
  Sample rows: [first 2 rows of actual data]
```

This is done by `DatabaseManager.get_schema()` — stored as a `SchemaTable` TypedDict — then **embedded into Pinecone** (or local ChromaDB) as vectors so the agent can semantically find the right tables for any natural language query.

### To trigger schema indexing

After connecting, the `schema_linker_node` auto-embeds your schema on first query. If you change your schema later, click **Reconnect** in the sidebar to force a `refresh_schema()`.

---

## Setting Up Pinecone (recommended for schemas with 10+ tables)

1. Get a free API key at https://pinecone.io
2. Add to `.env`:
   ```
   PINECONE_API_KEY=your_key_here
   ```
3. The system auto-creates the index `text2sql-schema` on first run (AWS us-east-1, cosine similarity, 1024-dim)

If `PINECONE_API_KEY` is not set, the system falls back to local **ChromaDB** — no setup needed.

---

## Recommended Test Flow

```
1. Start with data/chinook.db (DuckDB) — zero config, works immediately
2. Verify streaming, charts, debug panel, and HITL card all work
3. Swap in your own CSV/Parquet via the DuckDB ingestion snippet above
4. For production: point at your PostgreSQL/MySQL server directly via the sidebar
```

The sidebar handles all connection switching at runtime — no code changes needed.

---

## Key Config Files

| File | Purpose |
|------|---------|
| `config/config.yaml` | DB defaults, LLM settings, retry logic, HITL, logging |
| `config/pinecone_config.yaml` | Pinecone index name, dimension, region |
| `config/safety_config.yaml` | Allowed SQL statements (SELECT, WITH only) |
| `config/error-taxonomy.json` | Error classification for self-correction |
| `.env.example` | Template for API keys and DB credentials |

---

## Sample Chinook Queries to Verify Each Feature

| Feature | Query to try |
|---------|-------------|
| Basic SELECT | *"Show me all artists"* |
| Aggregation + chart | *"Total sales by country"* |
| Multi-table JOIN | *"Top 5 customers by invoice total"* |
| Filter | *"Tracks longer than 5 minutes in the Rock genre"* |
| HITL trigger | *"Delete all invoices from 2009"* (safety gate fires) |
