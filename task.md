# Text-to-SQL Agentic Pipeline Tasks

## Phase 1: Environment & Core DB Layer
- [/] Initialize Python Environment (`requirements.txt`)
- [ ] Develop `DatabaseManager`
  - [ ] DuckDB/MySQL connection support
  - [ ] Schema fetching functionality
  - [ ] Safe query execution

## Phase 2: Agent Nodes & LangGraph Orchestration
- [ ] Define `AgentState` schema
- [ ] Implement Agent Prompts (Schema Linker, Query Planner, SQL Generator)
- [ ] Implement Correction Logic (`error-taxonomy.json`, Correction Agents)
- [ ] Compile LangGraph StateGraph & configure Multi-Session Memory

## Phase 3: Frontend (Streamlit)
- [ ] Build Configuration Sidebar
- [ ] Build Chat Interface
- [ ] Implement Interactive Debugging Panel
- [ ] Add Dynamic Dashboards & Score Display

## Phase 4: Deployment Strategy
- [ ] Setup Dockerfile and docker-compose.yml
- [ ] Document Deployment Instructions
