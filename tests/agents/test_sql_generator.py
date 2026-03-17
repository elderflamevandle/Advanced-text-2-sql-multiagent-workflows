"""Unit tests for agents/nodes/sql_generator.py — AGENT-004 / LLM-003."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.agents.conftest import make_agent_state


def _make_llm_mock(content: str) -> MagicMock:
    """Return a mock llm whose ainvoke returns content."""
    mock_response = MagicMock()
    mock_response.content = content
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=mock_response)
    return mock_llm


def _patch_get_llm(mock_llm):
    """Patch llm.fallback.get_llm to return mock_llm."""
    return patch("llm.fallback.get_llm", return_value=mock_llm)


# ---------------------------------------------------------------------------
# test_prompt_constant_exists
# ---------------------------------------------------------------------------

def test_prompt_constant_exists():
    """_GENERATOR_PROMPT must be a non-empty module-level string (LLM-003)."""
    import agents.nodes.sql_generator as sg_mod
    assert hasattr(sg_mod, "_GENERATOR_PROMPT"), "_GENERATOR_PROMPT constant missing"
    assert isinstance(sg_mod._GENERATOR_PROMPT, str)
    assert len(sg_mod._GENERATOR_PROMPT.strip()) > 0


# ---------------------------------------------------------------------------
# test_returns_select
# ---------------------------------------------------------------------------

def test_returns_select():
    """sql_generator_node returns generated_sql starting with SELECT."""
    import agents.nodes.sql_generator as sg_mod

    mock_llm = _make_llm_mock("SQL:\nSELECT SUM(Total) FROM Invoice\nEXPLANATION:\nSums all invoice totals.")

    state = make_agent_state(user_query="total revenue")
    state["query_plan"] = {"select": ["SUM(Total)"], "from": "Invoice", "complexity": "simple"}
    with _patch_get_llm(mock_llm):
        result = asyncio.run(sg_mod.sql_generator_node(state))

    assert "generated_sql" in result
    assert result["generated_sql"].upper().startswith("SELECT")


# ---------------------------------------------------------------------------
# test_returns_with_cte
# ---------------------------------------------------------------------------

def test_returns_with_cte():
    """sql_generator_node accepts SQL starting with WITH (CTE)."""
    import agents.nodes.sql_generator as sg_mod

    mock_llm = _make_llm_mock("SQL:\nWITH monthly AS (SELECT * FROM Invoice) SELECT * FROM monthly\nEXPLANATION:\nUses a CTE.")

    state = make_agent_state(user_query="monthly breakdown")
    state["query_plan"] = {"ctes": [{"name": "monthly", "query": "SELECT * FROM Invoice"}], "complexity": "moderate"}
    with _patch_get_llm(mock_llm):
        result = asyncio.run(sg_mod.sql_generator_node(state))

    assert result["generated_sql"].upper().startswith("WITH")


# ---------------------------------------------------------------------------
# test_strips_fences
# ---------------------------------------------------------------------------

def test_strips_fences():
    """sql_generator_node strips ```sql fences from LLM output."""
    import agents.nodes.sql_generator as sg_mod

    mock_llm = _make_llm_mock("SQL:\n```sql\nSELECT * FROM Invoice\n```\nEXPLANATION:\nReturns all rows.")

    state = make_agent_state(user_query="all invoices")
    state["query_plan"] = {}
    with _patch_get_llm(mock_llm):
        result = asyncio.run(sg_mod.sql_generator_node(state))

    sql = result["generated_sql"]
    assert "```" not in sql
    assert sql.upper().startswith("SELECT")


# ---------------------------------------------------------------------------
# test_rejects_non_select
# ---------------------------------------------------------------------------

def test_rejects_non_select():
    """sql_generator_node sets error_log and None generated_sql for destructive SQL."""
    import agents.nodes.sql_generator as sg_mod

    mock_llm = _make_llm_mock("SQL:\nDROP TABLE Invoice\nEXPLANATION:\nDrops the table.")

    state = make_agent_state(user_query="delete invoices")
    state["query_plan"] = {}
    with _patch_get_llm(mock_llm):
        result = asyncio.run(sg_mod.sql_generator_node(state))

    assert result.get("generated_sql") is None or result.get("generated_sql") == ""
    assert result.get("error_log"), "error_log should be set for rejected SQL"


# ---------------------------------------------------------------------------
# test_includes_explanation
# ---------------------------------------------------------------------------

def test_includes_explanation():
    """sql_generator_node returns an explanation alongside the SQL."""
    import agents.nodes.sql_generator as sg_mod

    mock_llm = _make_llm_mock(
        "SQL:\nSELECT COUNT(*) FROM Invoice\nEXPLANATION:\nCounts all invoice records."
    )

    state = make_agent_state(user_query="how many invoices")
    state["query_plan"] = {}
    with _patch_get_llm(mock_llm):
        result = asyncio.run(sg_mod.sql_generator_node(state))

    assert result.get("sql_explanation"), "sql_explanation should be non-empty"
    assert "count" in result["sql_explanation"].lower() or "invoice" in result["sql_explanation"].lower()


# ---------------------------------------------------------------------------
# test_dialect_reminders
# ---------------------------------------------------------------------------

def test_dialect_reminders():
    """Prompt passed to LLM contains postgres dialect reminder when db_type=postgres."""
    import agents.nodes.sql_generator as sg_mod

    mock_llm = _make_llm_mock("SQL:\nSELECT * FROM Invoice\nEXPLANATION:\nAll rows.")

    state = make_agent_state(user_query="all invoices")
    state["db_type"] = "postgres"
    state["query_plan"] = {}
    with _patch_get_llm(mock_llm):
        asyncio.run(sg_mod.sql_generator_node(state))

    call_args = mock_llm.ainvoke.call_args
    assert call_args is not None
    messages = call_args[0][0]
    system_msgs = [m for m in messages if hasattr(m, "content") and ("ILIKE" in m.content or "TO_CHAR" in m.content)]
    assert system_msgs, "Postgres dialect reminder not found in system message"
