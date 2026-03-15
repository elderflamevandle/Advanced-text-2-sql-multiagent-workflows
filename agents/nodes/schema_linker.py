"""Schema linker agent node: retrieves relevant schema tables via vector search."""
import logging
from graph.state import AgentState

logger = logging.getLogger(__name__)


async def schema_linker_node(state: AgentState) -> dict:
    """Retrieves relevant schema tables via vector search, falls back to full schema on error."""
    # 1. Extract query: prefer resolved_query (rewritten follow-up) over raw user_query
    query = state.get("resolved_query") or state.get("user_query", "")
    db_type = state.get("db_type", "unknown")
    full_schema = state.get("schema")

    try:
        # 2. Lazy import get_retriever from vector.retriever
        from vector.retriever import get_retriever

        retriever = get_retriever()

        # 3. Embed schema (idempotent — no-op if already embedded)
        if full_schema:
            retriever.embed_schema(full_schema, db_type)

        # 4. Retrieve relevant tables
        result = retriever.retrieve_tables(query, db_type, top_k=5)
        relevant_tables = result["tables"]

        # 5. Narrow schema to relevant tables; fall back to full if nothing matched
        if full_schema:
            narrowed_schema = {t: full_schema[t] for t in relevant_tables if t in full_schema}
            if not narrowed_schema:
                narrowed_schema = full_schema
        else:
            narrowed_schema = full_schema

        logger.info("schema_linker_node: retrieved %d tables for query: %s", len(relevant_tables), query)
        return {"relevant_tables": relevant_tables, "schema": narrowed_schema}

    except Exception as exc:
        logger.warning("schema_linker_node: retriever failed (%s), falling back to full schema", exc)
        fallback_tables = list(full_schema.keys()) if full_schema else []
        return {"relevant_tables": fallback_tables, "schema": full_schema}
