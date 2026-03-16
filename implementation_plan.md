# Comprehensive Implementation Plan: Agentic Text-to-SQL Pipeline

This plan synthesizes your two distinct approaches (the LangGraph-based single-pass architecture and the SQL-of-Thought multi-agent architecture) into a robust, deployable, and scalable Python application.

## I. System Architecture & Technology Stack

By combining **LangGraph** (State/Graph management) with the **Multi-Agent specialized roles** (Schema Linking, Query Planning, Correction), we achieve a highly accurate and observable pipeline capable of handling complex interactions.

- **Orchestration Framework:** `langgraph` (Python) for managing the cyclic state, memory, and routing between specialized agents. We will utilize **LangGraph patterns:** Agent loops (for error correction), specific node types (planning, generation, execution), and potentially human-in-the-loop nodes for query approval.
- **Frontend & UI:** `streamlit` serving as the primary interactive Web UI. It handles database configuration, chat-based interaction with conversational memory, dynamic dashboards, and a dedicated SQL debugging panel.
- **Large Language Model (LLM):** Configurable LLM provider structure. Initial implementation prioritizes the Groq API (e.g., Llama-3 70B or Mixtral) for high-speed, low-cost generation, with fallback to OpenAI (`gpt-4o-mini` or `gpt-4o`) or Anthropic for complex planning tasks.
- **Database Backend:** The `DatabaseManager` will be designed to be extensible to **all major SQL databases**. Initial support will be implemented for:
  - **DuckDB** (fast, local analytical queries, great for prototyping)
  - **MySQL / PostgreSQL** (remote production databases)
  - **SQLite** (local file-based)
- **Evaluation:** `ragas` for scoring the accuracy of generated queries and answers.

---

## II. Multi-Agent LangGraph Workflow

Instead of a single "Generate SQL" node, the graph will follow the **SQL-of-Thought** decomposition principles, managed via LangGraph's state dictionary (`AgentState`).

### 1. The State Dictionary (`AgentState`)
- `messages`: Chat history (LangChain structured messages). Enables handling follow-up questions and conversational context across a session.
- `user_query`: Original natural language question.
- `schema`: Database schema context (automatically learned/fetched directly from the database schema tables).
- `relevant_tables`: Extracted subset of tables (Vectorized Schema Retrieval).
- `query_plan`: The Chain-of-Thought execution plan. Supports building **complex queries** (multi-table joins, aggregations, grouping, subqueries/CTEs, and window functions).
- `generated_sql`: The current drafted SQL.
- `db_results`: Results from execution.
- `error_log`: Details of any SQL execution failures.
- `retry_count`: Integer counter (max 2 attempts).
- `final_answer`: Formatted natural language response.

### 2. Specialized Agent Nodes (The Pipeline Stages)
- **Gatekeeper Node (Query Understanding):** Validates the query. If it's conversational/gibberish, bypasses SQL generation and answers directly. Identifies if the user is asking a follow-up question requiring conversational history.
- **Schema Linking Node (Retrieval):** Instead of passing hundreds of tables to the LLM context, it uses a Vector Database to fetch *only* the most relevant tables/columns based on semantic similarity to the `user_query`. The system learns this automatically from the database metadata.
- **Query Planning Node (CoT):** Uses the relevant schema to break down the request into specific SQL clauses, explicitly mapping out joins, groupings, aggregations, and subqueries.
- **SQL Generation Agent:** Translates the CoT plan into strict, dialect-specific SQL (e.g., PostgreSQL vs MySQL syntax).
- **Quality & Validation Stage (Optional Human-in-the-Loop):** Before execution, the generated SQL and an explanation can be presented. If configured, a Human-in-the-loop node pauses the graph, requiring explicit user approval (via the UI) to proceed.
- **Execution & Safety Node:** Scans the SQL for destructive keywords (`DROP`, `DELETE`, `TRUNCATE`). Connects using a **Read-Only** DB user. If safe, executes against the target database. If it fails, transitions to the Correction loop.
- **Correction Plan Agent:** *Condition: Only triggered if Execution fails.* Analyzes the error against an imported `error-taxonomy.json` (20 common categories) to formulate a diagnostic fix.
- **Correction SQL Agent:** Modifies the SQL based on the correction plan and increments `retry_count`. (Limits loop to max 2 tries).
- **Format Answer & Visuals Node:** Converts absolute data into a natural language response. If the data is time-series or categorical, it flags it for the UI to generate dynamic charts.
- **Evaluation Node:** Hands off the query, SQL, and final answer to `ragas` for a validation confidence score (0.0 to 1.0).

---

## III. Build & Development Plan

### Phase 1: Environment & Core DB Layer
1. **Initialize Python Environment:** Set up a virtual environment and `requirements.txt` (`langchain`, `langgraph`, `streamlit`, `duckdb`, `mysql-connector-python`, `ragas`).
2. **Develop `DatabaseManager`:** Implement the class that connects to either MySQL or DuckDB, fetches schema (table names, types, primary keys, and 2 sample rows), and executes read-only queries safely.

### Phase 2: Agent Nodes & LangGraph Orchestration
1. **Implement Specialized Prompts:** Design targeted prompts for the Schema Linker, Query Planner, and SQL Generator.
2. **Build Correction Logic:** Implement the `error-taxonomy.json` mapping. Build the Correction Plan and Correction SQL nodes.
3. **Compile the Graph:** Use LangGraph's `StateGraph` to connect nodes. Add conditional edges:
   - `execute_node` -> `success` -> `format_answer`
   - `execute_node` -> `fail (count < 2)` -> `correction_plan` -> `correction_sql` -> `execute_node`
   - `execute_node` -> `fail (count >= 2)` -> `format_answer` (with "Failed to query" message)
4. **Multi-Session Memory:** Integrate LangGraph `MemorySaver` using Thread IDs so multiple interactive users don't overwrite each other's context.

### Phase 3: Frontend (Streamlit)
1. **Configuration Sidebar:** Inputs for Groq API keys, DB credentials, and DB Type selector (MySQL/DuckDB). Token/Cost usage tracker.
2. **Chat Interface:** Main chat area for Natural Language questions.
3. **Interactive Debugging Panel:** Below each answer, show an expandable section with:
   - The CoT Query Plan.
   - The exact SQL Output.
   - Any retry logs or errors encountered.
   - An "Edit & Rerun SQL" button for manual human correction.
4. **Dynamic Dashboards:** Use Streamlit's native charting (e.g., `st.bar_chart` or `st.plotly_chart`) if the returned dataframe fits typical charting shapes.
5. **Score Display:** Display the `ragas` accuracy score explicitly.

---

## IV. Deployment Strategy

To deploy this effectively, we should use a containerized approach for scalability and isolation.

### Option A: Streamlit Community Cloud (Fastest Prototype)
- Perfect if you are querying a remote MySQL database or a checked-in SQLite/DuckDB file.
- Add your API keys and DB credentials as Streamlit Secrets.
- Directly deploy via a GitHub repository.

### Option B: Docker Compose (Best for Production / Local Deployment)
1. **Dockerfile:** Create a Docker container for the Streamlit app.
2. **Docker-Compose:** Package the Streamlit App container alongside a structured DB container (e.g., local MySQL or PostgreSQL) if you want a fully self-contained ecosystem.
3. **Hosting:** Deploy your Docker container to **Render**, **Railway**, or **AWS Fargate**.

**Example `docker-compose.yml` Structure:**
```yaml
services:
  app:
    build: .
    ports:
      - "8501:8501"
    environment:
      - GROQ_API_KEY=${GROQ_API_KEY}
    depends_on:
      - db
  db:
    image: mysql:8.0
    environment:
      - MYSQL_ROOT_PASSWORD=root
      - MYSQL_DATABASE=text2sql
    ports:
      - "3306:3306"
```

---
> [!TIP]
> **Next Steps:** Which part of the system would you like to build first? We can begin with scaffolding out the Streamlit UI, building the `DatabaseManager` to connect to DuckDB/MySQL, or defining the LangGraph `AgentState` schema.
