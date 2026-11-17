"""Evaluator node: the last node of every run. Records the run for later learning.

It scores nothing yet. What it does:
- appends the question and the answer to ``messages`` so the next turn on the same thread
  has chat history (the gatekeeper's follow-up rewrite depends on it);
- for registered connections, logs the query in the query memory (``query_id`` in state is
  what the feedback endpoint takes) and, when the run only succeeded after a correction,
  stores one short lesson built from the correction plan.

Memory failures never fail the run, and no LLM call is made here.
"""
import asyncio
import logging

from graph.state import AgentState

logger = logging.getLogger(__name__)

_MAX_LESSON_CHARS = 300
_MAX_HISTORY_ANSWER_CHARS = 1000


def _one_line(value) -> str:
    return " ".join(str(value or "").split())


def _history_messages(state: AgentState) -> list:
    """This turn as two chat messages (question, answer) for the checkpointed history."""
    question = _one_line(state.get("user_query"))
    answer = str(state.get("final_answer") or "").strip()
    if not question or not answer:
        return []
    from langchain_core.messages import AIMessage, HumanMessage

    is_clarification = (state.get("query_type") == "clarification"
                        and bool(state.get("clarification_question")))
    extra = {"clarification": True} if is_clarification else {}
    return [HumanMessage(content=question),
            AIMessage(content=answer[:_MAX_HISTORY_ANSWER_CHARS], additional_kwargs=extra)]


def _thread_id(state: AgentState) -> str | None:
    """The client's thread id, read from the runnable config (None when unknown)."""
    try:
        from langchain_core.runnables.config import var_child_runnable_config

        config = var_child_runnable_config.get() or {}
        thread = (config.get("configurable") or {}).get("thread_id")
    except Exception:  # noqa: BLE001
        return None
    if not thread:
        return None
    thread = str(thread)
    prefix = f"{state.get('tenant_id')}:"  # the API namespaces thread ids per tenant
    return thread[len(prefix):] if thread.startswith(prefix) else thread


def build_lesson(plan) -> str | None:
    """One or two sentences (root cause, then the fix) from a correction plan.

    Deterministic, built only from the plan (never from result rows) and capped in length.
    Transient errors (retry unchanged) teach nothing and give None.
    """
    if not isinstance(plan, dict) or plan.get("severity") == "transient":
        return None
    cause = _one_line(plan.get("root_cause")).rstrip(".")
    steps = [_one_line(s).rstrip(".") for s in (plan.get("correction_steps") or [])
             if isinstance(s, str) and _one_line(s) and s != "retry_unchanged"]
    if not cause or cause.lower().startswith("parse error"):
        return None
    text = f"{cause}."
    if steps:
        text += f" Fix: {'; '.join(steps[:2])}."
    if len(text) > _MAX_LESSON_CHARS:
        text = text[: _MAX_LESSON_CHARS - 3].rstrip() + "..."
    return text


async def _record(state: AgentState) -> dict:
    connection_id = state.get("connection_id")
    sql = state.get("generated_sql")
    if not connection_id or not sql:
        return {}  # legacy path, or no SQL was produced (chat, clarification, LLM outage)
    from agents.services import memory_service

    service = memory_service()
    if service is None:
        return {}
    tenant_id = state.get("tenant_id")
    error_log = state.get("error_log")
    succeeded = error_log is None and state.get("db_results") is not None
    error_type = None
    if not succeeded:
        error_type = (error_log.get("error_type") if isinstance(error_log, dict) else None) \
            or "query_failed"
    question = state.get("resolved_query") or state.get("user_query") or ""

    update: dict = {}
    query_id = await asyncio.to_thread(
        service.log_query, tenant_id, connection_id,
        question=question, sql=sql,
        status="completed" if succeeded else "failed",
        db_type=state.get("db_type"),
        error_type=error_type,
        tables=tuple(state.get("relevant_tables") or ()),
        thread_id=_thread_id(state),
    )
    if query_id:
        update["query_id"] = str(query_id)

    if succeeded and state.get("sql_history"):
        lesson = build_lesson(state.get("correction_plan"))
        if lesson:
            try:
                await asyncio.to_thread(service.add_lesson, tenant_id, connection_id, lesson,
                                        source="correction")
            except Exception as exc:  # noqa: BLE001
                logger.warning("evaluator_node: add_lesson failed (%s)", type(exc).__name__)
    return update


async def evaluator_node(state: AgentState) -> dict:
    """Record the finished run (chat history, query log, lesson). Never fails the run."""
    logger.info("evaluator_node called for query: %s", state.get("user_query", ""))
    update: dict = {}
    try:
        update.update(await _record(state))
    except Exception as exc:  # noqa: BLE001 - includes NotImplementedError from a stub
        logger.warning("evaluator_node: query memory unavailable (%s)", type(exc).__name__)
    try:
        messages = _history_messages(state)
        if messages:
            update["messages"] = messages
    except Exception as exc:  # noqa: BLE001
        logger.warning("evaluator_node: chat history not recorded (%s)", type(exc).__name__)
    return update
