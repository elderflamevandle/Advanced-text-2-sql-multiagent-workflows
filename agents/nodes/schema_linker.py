"""Schema linker agent node: retrieves relevant schema tables via vector search."""
import asyncio
import logging

from graph.state import AgentState
from vector.namespace import schema_namespace

logger = logging.getLogger(__name__)


def _full_schema(state: AgentState) -> dict | None:
    """The complete schema of the database this run is about.

    For a registered connection the registry is the source of truth: ``schema`` in state
    is replaced by the narrowed schema once this node has run, so on a later turn of the
    same thread it may hold only a few tables. Without a connection id (legacy single
    database mode), or when the registry cannot answer, the schema in state is used.
    """
    state_schema = state.get("schema")
    connection_id = state.get("connection_id")
    if not connection_id:
        return state_schema
    try:
        from database.registry import get_registry

        schema = get_registry().get_schema(state.get("tenant_id"), connection_id)
        return schema or state_schema
    except Exception as exc:  # noqa: BLE001 - includes NotImplementedError and ConnectionNotFound
        logger.debug("schema_linker_node: registry schema unavailable (%s), using state", exc)
        return state_schema


def _retrieve(query: str, schema: dict, namespace: str) -> list:
    """Blocking part (model load, embedding, vector-store calls): runs in a worker thread."""
    # Lazy import: the vector libraries are optional extras.
    from vector.retriever import get_retriever

    retriever = get_retriever()
    # Idempotent: embeds only when this namespace has no vectors yet.
    retriever.embed_schema(schema, namespace)
    result = retriever.retrieve_tables(query, namespace, top_k=5)
    return list(result.get("tables") or [])


async def schema_linker_node(state: AgentState) -> dict:
    """Retrieves relevant schema tables via vector search, falls back to full schema on error.

    The vector namespace is derived from the connection id and a hash of the schema (see
    vector/namespace.py), so every database gets its own embeddings and a changed schema
    is embedded again.
    """
    # Prefer resolved_query (rewritten follow-up) over raw user_query
    query = state.get("resolved_query") or state.get("user_query", "")
    full_schema = await asyncio.to_thread(_full_schema, state)

    if not full_schema:
        # Nothing to embed or to narrow.
        return {"relevant_tables": [], "schema": full_schema}

    try:
        namespace = schema_namespace(state.get("connection_id"), full_schema)
        retrieved = await asyncio.to_thread(_retrieve, query, full_schema, namespace)

        # Keep only tables that exist in this schema: the store may return a table that
        # has since been dropped, and downstream prompts must not mention it.
        relevant_tables = [t for t in dict.fromkeys(retrieved) if t in full_schema]
        unknown = [t for t in retrieved if t not in full_schema]
        if unknown:
            logger.warning("schema_linker_node: ignoring tables not in the schema: %s", unknown)

        if relevant_tables:
            narrowed_schema = {t: full_schema[t] for t in relevant_tables}
        else:
            # Nothing usable came back: give the planner everything.
            relevant_tables = list(full_schema.keys())
            narrowed_schema = full_schema

        logger.info(
            "schema_linker_node: %d of %d tables selected (namespace %s) for query: %s",
            len(relevant_tables), len(full_schema), namespace, query,
        )
        return {"relevant_tables": relevant_tables, "schema": narrowed_schema}

    except Exception as exc:
        logger.warning(
            "schema_linker_node: retriever failed (%s), falling back to full schema", exc
        )
        return {"relevant_tables": list(full_schema.keys()), "schema": full_schema}
