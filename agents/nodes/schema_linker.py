"""Schema linker agent node: selects the tables for a question (schema catalog when one is
built, else vector search) and fetches examples and lessons from the query memory."""
import asyncio
import logging

from graph.state import AgentState
from vector.namespace import schema_namespace

logger = logging.getLogger(__name__)

EXAMPLE_COUNT = 3
LESSON_LIMIT = 8


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


async def _catalog_selection(state: AgentState, query: str) -> dict | None:
    """Table selection from the schema catalog, or None when the legacy path must be used
    (no connection id, no catalog service, no built catalog, or any failure)."""
    connection_id = state.get("connection_id")
    if not connection_id:
        return None
    from agents.services import catalog_service

    service = catalog_service()
    if service is None:
        return None
    widen = bool(state.get("reselect_count"))
    must_include = tuple(state.get("required_tables") or ())
    try:
        context = await asyncio.to_thread(
            service.select_context, state.get("tenant_id"), connection_id, query,
            widen=widen, must_include=must_include)
    except Exception as exc:  # noqa: BLE001 - includes NotImplementedError from a stub
        logger.warning("schema_linker_node: catalog selection failed (%s), using the "
                       "legacy path", type(exc).__name__)
        return None
    tables = list(getattr(context, "tables", None) or [])
    if context is None or not tables:
        return None
    logger.info("schema_linker_node: catalog selected %d tables (widen=%s)",
                len(tables), widen)
    return {
        "relevant_tables": tables,
        "schema": dict(getattr(context, "schema", None) or {}),
        "schema_context": getattr(context, "prompt_text", None) or None,
        "join_hints": list(getattr(context, "join_hints", None) or []),
    }


def _example_dict(example) -> dict | None:
    """Plain dict for the checkpointed state (the service returns dataclasses)."""
    if isinstance(example, dict):
        question, sql = example.get("question"), example.get("sql")
        verified, score = example.get("verified"), example.get("score")
    else:
        question, sql = getattr(example, "question", None), getattr(example, "sql", None)
        verified, score = getattr(example, "verified", False), getattr(example, "score", None)
    if not question or not sql:
        return None
    try:
        score = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        score = 0.0
    return {"question": str(question), "sql": str(sql), "verified": bool(verified),
            "score": score}


async def _memory_context(state: AgentState, query: str) -> dict:
    """Similar past queries and lessons for this connection. Never raises: a memory
    failure only means the prompts carry no examples or lessons."""
    connection_id = state.get("connection_id")
    if not connection_id:
        return {}
    from agents.services import memory_service

    service = memory_service()
    if service is None:
        return {}
    tenant_id = state.get("tenant_id")
    examples: list = []
    lessons: list = []
    try:
        found = await asyncio.to_thread(
            service.similar_examples, tenant_id, connection_id, query, k=EXAMPLE_COUNT)
        examples = [e for e in (_example_dict(item) for item in (found or [])) if e]
    except Exception as exc:  # noqa: BLE001
        logger.warning("schema_linker_node: similar_examples failed (%s)", type(exc).__name__)
    try:
        found = await asyncio.to_thread(
            service.lessons, tenant_id, connection_id, limit=LESSON_LIMIT)
        lessons = [str(item) for item in (found or []) if item]
    except Exception as exc:  # noqa: BLE001
        logger.warning("schema_linker_node: lessons failed (%s)", type(exc).__name__)
    return {"examples": examples, "lessons": lessons}


async def schema_linker_node(state: AgentState) -> dict:
    """Select the tables for the question and collect prompt context.

    With a built schema catalog for the run's connection, the catalog service picks the
    tables and renders the prompt context (``schema_context``, ``join_hints``). Otherwise
    the vector/fallback path below is used unchanged. For registered connections, similar
    past queries and lessons are fetched from the query memory when it is available.
    """
    # Prefer resolved_query (rewritten follow-up) over raw user_query
    query = state.get("resolved_query") or state.get("user_query", "")
    update = await _catalog_selection(state, query)
    if update is None:
        update = await _legacy_selection(state, query)
    update.update(await _memory_context(state, query))
    return update


async def _legacy_selection(state: AgentState, query: str) -> dict:
    """Retrieves relevant schema tables via vector search, falls back to full schema on error.

    The vector namespace is derived from the connection id and a hash of the schema (see
    vector/namespace.py), so every database gets its own embeddings and a changed schema
    is embedded again.
    """
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
