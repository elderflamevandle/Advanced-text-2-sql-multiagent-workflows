"""Human-in-the-loop (HITL) approval gate node.

Exports:
    hitl_node(state) -> dict — async LangGraph node that pauses execution
                               for user approval of complex SQL queries.

Behaviour:
    1. HITL disabled in config  -> auto_approved (pass-through)
    2. Already has approval_status (graph resumed after interrupt)
       - "approved"  -> pass through (return {})
       - "rejected"  -> set error_log and return rejected status
       - "edited"    -> accept edited SQL already in state, return approved
    3. Simple query + auto_approve_simple=True -> auto_approved
    4. Otherwise -> raise LangGraph interrupt for human review
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from graph.state import AgentState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_hitl_config() -> dict:
    """Load hitl section from config/config.yaml. Not cached — tests may patch."""
    config_path = Path(__file__).parent.parent.parent / "config" / "config.yaml"
    with open(config_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data.get("hitl", {})


# ---------------------------------------------------------------------------
# Simple query detection
# ---------------------------------------------------------------------------

# Patterns that make a query "complex" (not simple)
_JOIN_RE = re.compile(r"\bJOIN\b", re.IGNORECASE)
_UNION_RE = re.compile(r"\bUNION\b", re.IGNORECASE)
_WITH_RE = re.compile(r"^\s*WITH\b", re.IGNORECASE)


def _is_simple_query(sql: str) -> bool:
    """Return True if sql is a single-table SELECT with no JOINs/subqueries/UNIONs/CTEs.

    Rules (all case-insensitive):
    - Contains JOIN keyword         -> not simple
    - Contains nested SELECT        -> not simple (subquery)
    - Contains UNION keyword        -> not simple
    - Starts with WITH              -> not simple (CTE)
    - Otherwise                     -> simple
    """
    if _JOIN_RE.search(sql):
        return False
    # Subquery: more than one SELECT occurrence
    if len(re.findall(r"\bSELECT\b", sql, re.IGNORECASE)) > 1:
        return False
    if _UNION_RE.search(sql):
        return False
    if _WITH_RE.match(sql):
        return False
    return True


# ---------------------------------------------------------------------------
# HITL node
# ---------------------------------------------------------------------------

async def hitl_node(state: AgentState) -> dict:
    """HITL approval gate: interrupt for complex queries, auto-approve simple ones.

    Returns:
        dict with updated approval_status and optionally error_log.
    """
    logger.info("hitl_node called for query: %s", state.get("user_query", ""))

    hitl_cfg = _load_hitl_config()
    hitl_enabled: bool = hitl_cfg.get("enabled", True)
    auto_approve_simple: bool = hitl_cfg.get("auto_approve_simple", True)

    # ------------------------------------------------------------------
    # 1. HITL globally disabled — pass through
    # ------------------------------------------------------------------
    if not hitl_enabled:
        logger.debug("hitl_node: HITL disabled — auto-approving")
        return {"approval_status": "auto_approved"}

    # ------------------------------------------------------------------
    # 2. Already has approval_status — node is being resumed after interrupt
    # ------------------------------------------------------------------
    approval_status: str | None = state.get("approval_status")
    if approval_status is not None:
        if approval_status == "approved":
            logger.info("hitl_node: resumed with approval_status=approved — passing through")
            return {}
        if approval_status == "rejected":
            logger.warning("hitl_node: resumed with approval_status=rejected — setting error_log")
            return {
                "approval_status": "rejected",
                "error_log": {
                    "error_type": "user_rejected",
                    "message": "Query rejected by user during HITL review.",
                    "dialect": state.get("db_type", "unknown"),
                    "failed_sql": state.get("generated_sql"),
                    "hint": "The SQL was reviewed and rejected. Please rephrase your question.",
                },
            }
        if approval_status == "edited":
            logger.info("hitl_node: resumed with approval_status=edited — approving edited SQL")
            return {"approval_status": "approved"}

    # ------------------------------------------------------------------
    # 3. Auto-approve simple queries (when feature enabled)
    # ------------------------------------------------------------------
    generated_sql: str | None = state.get("generated_sql") or ""
    if auto_approve_simple and _is_simple_query(generated_sql):
        logger.info("hitl_node: simple query detected — auto-approving")
        return {"approval_status": "auto_approved"}

    # ------------------------------------------------------------------
    # 4. Complex query — pause for human review via LangGraph interrupt
    # ------------------------------------------------------------------
    sql_explanation: str | None = state.get("sql_explanation")
    relevant_tables: list | None = state.get("relevant_tables")

    logger.info("hitl_node: complex query — raising interrupt for human review")

    from langgraph.types import interrupt  # noqa: PLC0415 — lazy import

    interrupt(
        {
            "type": "hitl_review",
            "generated_sql": generated_sql,
            "sql_explanation": sql_explanation,
            "relevant_tables": relevant_tables,
        }
    )

    # When the graph resumes, this node will be called again with updated state
    # containing approval_status set by the runtime. The code above (step 2) handles it.
    # This return is never reached in normal flow but satisfies static analysis.
    return {}  # pragma: no cover
