"""Tests for Phase 6 error correction loop — ERROR-001, ERROR-002, ERROR-003.

Wave 1: 5 taxonomy/utility tests are fully implemented and pass.
Wave 2: 7 node behavior stubs are skipped until correction_plan_node and
        correction_sql_node are implemented.
"""
import asyncio
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.agents.conftest import make_agent_state


# ---------------------------------------------------------------------------
# Mock helpers (for Wave 2 node tests)
# ---------------------------------------------------------------------------

def _make_llm_mock(content: str) -> MagicMock:
    """Return a fully configured ChatGroq mock whose ainvoke returns content."""
    mock_response = MagicMock()
    mock_response.content = content
    mock_llm_instance = MagicMock()
    mock_llm_instance.ainvoke = AsyncMock(return_value=mock_response)
    mock_llm_class = MagicMock(return_value=mock_llm_instance)
    return mock_llm_class


def _inject_chatgroq_mock(mock_class: MagicMock):
    """Inject mock into sys.modules so lazy import inside function picks it up."""
    fake_module = types.ModuleType("langchain_groq")
    fake_module.ChatGroq = mock_class
    sys.modules["langchain_groq"] = fake_module


def _inject_correction_plan_mock(mock_class: MagicMock):
    """Reload correction_plan module with ChatGroq mock injected."""
    _inject_chatgroq_mock(mock_class)
    import agents.nodes.correction_plan as cp_mod
    importlib.reload(cp_mod)
    return cp_mod


def _inject_correction_sql_mock(mock_class: MagicMock):
    """Reload correction_sql module with ChatGroq mock injected."""
    _inject_chatgroq_mock(mock_class)
    import agents.nodes.correction_sql as cs_mod
    importlib.reload(cs_mod)
    return cs_mod


# ===========================================================================
# ERROR-001: Taxonomy and classification tests (Wave 1 — fully implemented)
# ===========================================================================

def test_taxonomy_structure():
    """ERROR-001: _load_taxonomy() returns 20 categories each with required keys."""
    from utils.error_parser import _load_taxonomy

    taxonomy = _load_taxonomy()
    assert isinstance(taxonomy, dict)
    assert "categories" in taxonomy
    cats = taxonomy["categories"]
    assert len(cats) == 20, f"Expected 20 categories, got {len(cats)}"

    required_keys = {"id", "name", "severity", "strategy", "prompt_hint", "patterns"}
    dialect_keys = {"postgres", "mysql", "sqlite", "duckdb"}
    for cat in cats:
        missing = required_keys - set(cat.keys())
        assert not missing, f"Category {cat.get('id', '?')} missing keys: {missing}"
        missing_dialects = dialect_keys - set(cat.get("patterns", {}).keys())
        assert not missing_dialects, (
            f"Category {cat['id']} missing dialect patterns: {missing_dialects}"
        )


def test_classify_postgres_syntax_error():
    """ERROR-001: classify_error returns (syntax_error, high) for postgres syntax error."""
    from utils.error_parser import _load_taxonomy, classify_error

    taxonomy = _load_taxonomy()
    error_log = {"message": "syntax error at or near 'SELCT'", "dialect": "postgres"}
    category, confidence = classify_error(error_log, taxonomy)
    assert category["id"] == "syntax_error", f"Expected 'syntax_error', got {category['id']}"
    assert confidence == "high"


def test_classify_sqlite_missing_table():
    """ERROR-001: classify_error returns (missing_table, high) for sqlite missing table."""
    from utils.error_parser import _load_taxonomy, classify_error

    taxonomy = _load_taxonomy()
    error_log = {"message": "no such table: invoices", "dialect": "sqlite"}
    category, confidence = classify_error(error_log, taxonomy)
    assert category["id"] == "missing_table", f"Expected 'missing_table', got {category['id']}"
    assert confidence == "high"


def test_llm_fallback_on_unknown_error():
    """ERROR-001: classify_error returns (unknown, low) when no regex pattern matches.

    The LLM fallback itself is in correction_plan_node (Wave 2); this test verifies
    that classify_error correctly signals low confidence for unrecognized errors.
    """
    from utils.error_parser import _load_taxonomy, classify_error

    taxonomy = _load_taxonomy()
    error_log = {
        "message": "completely unrecognizable_error_xyz_99999_abc",
        "dialect": "sqlite",
    }
    category, confidence = classify_error(error_log, taxonomy)
    assert category["id"] == "unknown", f"Expected 'unknown', got {category['id']}"
    assert confidence == "low"


def test_fuzzy_match_table_name():
    """ERROR-003: get_fuzzy_matches returns top matches for a misspelled table name."""
    from utils.error_parser import get_fuzzy_matches

    candidates = ["Invoice", "InvoiceLine", "Customer", "Artist", "Track"]
    matches = get_fuzzy_matches("Inovice", candidates)
    assert len(matches) > 0, "Expected at least one fuzzy match"
    assert "Invoice" in matches, f"Expected 'Invoice' in matches, got {matches}"


# ===========================================================================
# ERROR-002: correction_plan_node behavior tests (Wave 2 stubs — skipped)
# ===========================================================================

def test_correction_plan_returns_structured_plan():
    """ERROR-002: correction_plan_node returns a dict with required keys."""
    cp_mod = _inject_correction_plan_mock(_make_llm_mock('{"strategy": "fix_syntax"}'))
    state = make_agent_state()
    state["error_log"] = {"message": "syntax error at or near 'FROM'", "dialect": "sqlite"}
    result = asyncio.run(cp_mod.correction_plan_node(state))
    assert "correction_plan" in result
    plan = result["correction_plan"]
    assert isinstance(plan, dict)
    required = {"error_category", "strategy", "prompt_hint"}
    missing = required - set(plan.keys())
    assert not missing, f"correction_plan missing keys: {missing}"


def test_transient_error_no_llm_call():
    """ERROR-002: correction_plan_node skips LLM call for transient errors."""
    mock_class = _make_llm_mock("{}")
    cp_mod = _inject_correction_plan_mock(mock_class)
    state = make_agent_state()
    state["error_log"] = {"message": "connection refused", "dialect": "postgres"}
    asyncio.run(cp_mod.correction_plan_node(state))
    mock_llm_instance = mock_class.return_value
    mock_llm_instance.ainvoke.assert_not_called()


@pytest.mark.skip(reason="Wave 2: correction_sql_node not yet implemented")
def test_retry_count_increments():
    """ERROR-002: correction_sql_node increments retry_count by 1."""
    cs_mod = _inject_correction_sql_mock(_make_llm_mock("SQL: SELECT 1"))
    state = make_agent_state()
    state["retry_count"] = 0
    state["correction_plan"] = {"strategy": "fix_syntax", "prompt_hint": "Fix syntax"}
    state["generated_sql"] = "SELCT 1"
    result = asyncio.run(cs_mod.correction_sql_node(state))
    assert result.get("retry_count") == 1


@pytest.mark.skip(reason="Wave 2: correction_sql_node not yet implemented")
def test_error_log_cleared_after_correction():
    """ERROR-002: correction_sql_node returns error_log: None after rewrite."""
    cs_mod = _inject_correction_sql_mock(_make_llm_mock("SQL: SELECT 1"))
    state = make_agent_state()
    state["error_log"] = {"message": "syntax error", "dialect": "sqlite"}
    state["correction_plan"] = {"strategy": "fix_syntax", "prompt_hint": "Fix syntax"}
    result = asyncio.run(cs_mod.correction_sql_node(state))
    assert result.get("error_log") is None


@pytest.mark.skip(reason="Wave 2: correction_sql_node not yet implemented")
def test_sql_history_accumulates():
    """ERROR-002: correction_sql_node appends the previous SQL+error to sql_history."""
    cs_mod = _inject_correction_sql_mock(_make_llm_mock("SQL: SELECT 1"))
    state = make_agent_state()
    state["generated_sql"] = "SELCT 1"
    state["error_log"] = {"message": "syntax error", "dialect": "sqlite"}
    state["correction_plan"] = {"strategy": "fix_syntax", "prompt_hint": "Fix syntax"}
    state["sql_history"] = None
    result = asyncio.run(cs_mod.correction_sql_node(state))
    history = result.get("sql_history")
    assert isinstance(history, list), f"Expected list, got {type(history)}"
    assert len(history) == 1
    entry = history[0]
    assert "sql" in entry
    assert "error" in entry
    assert "attempt_num" in entry


# ===========================================================================
# ERROR-003: SQL rewrite and integration tests (Wave 2 stubs — skipped)
# ===========================================================================

@pytest.mark.skip(reason="Wave 2: correction_sql_node not yet implemented")
def test_sql_rewrite_uses_plan():
    """ERROR-003: correction_sql_node calls LLM and returns corrected generated_sql."""
    corrected = "SELECT * FROM Invoice"
    cs_mod = _inject_correction_sql_mock(_make_llm_mock(f"SQL: {corrected}"))
    state = make_agent_state()
    state["generated_sql"] = "SELCT * FORM Invoice"
    state["correction_plan"] = {"strategy": "fix_syntax", "prompt_hint": "Fix syntax"}
    result = asyncio.run(cs_mod.correction_sql_node(state))
    assert "generated_sql" in result
    assert result["generated_sql"] == corrected


@pytest.mark.skip(reason="Wave 2: integration test requires both nodes implemented")
def test_wrong_table_self_corrects():
    """ERROR-003: Full correction loop (plan + rewrite) produces valid SQL for table typo."""
    state = make_agent_state()
    state["generated_sql"] = "SELECT * FROM Invoce"
    state["error_log"] = {"message": "no such table: Invoce", "dialect": "sqlite"}
    state["retry_count"] = 0

    # Step 1: correction_plan_node classifies and builds plan
    cp_mod = _inject_correction_plan_mock(_make_llm_mock("{}"))
    plan_result = asyncio.run(cp_mod.correction_plan_node(state))
    assert plan_result.get("correction_plan") is not None

    # Step 2: correction_sql_node uses the plan to rewrite
    state.update(plan_result)
    corrected_sql = "SELECT * FROM Invoice"
    cs_mod = _inject_correction_sql_mock(_make_llm_mock(f"SQL: {corrected_sql}"))
    sql_result = asyncio.run(cs_mod.correction_sql_node(state))
    assert sql_result.get("generated_sql") == corrected_sql
