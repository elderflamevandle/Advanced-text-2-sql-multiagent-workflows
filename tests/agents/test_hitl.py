"""Tests for HITL (Human-In-The-Loop) node.

AGENT-010: Approval gate pauses graph for complex queries,
           auto-approves simple queries, and handles resumption states.
"""
import asyncio
import importlib
import sys
import types
from unittest.mock import MagicMock, patch, call

import pytest

from tests.agents.conftest import make_agent_state


# ---------------------------------------------------------------------------
# Helpers to inject mock langgraph.types before importing hitl module
# ---------------------------------------------------------------------------

def _make_langgraph_types_mock(interrupt_side_effect=None):
    """Return a mock langgraph.types module with a mock interrupt function."""
    mock_module = types.ModuleType("langgraph.types")
    mock_interrupt = MagicMock(side_effect=interrupt_side_effect)
    mock_module.interrupt = mock_interrupt
    return mock_module, mock_interrupt


def _reload_hitl(mock_langgraph_types=None):
    """Remove cached hitl module and reload with optional langgraph.types override."""
    # Remove any previously cached version
    for key in list(sys.modules.keys()):
        if "hitl" in key:
            del sys.modules[key]

    if mock_langgraph_types is not None:
        sys.modules["langgraph.types"] = mock_langgraph_types

    import agents.nodes.hitl as hitl_mod
    importlib.reload(hitl_mod)
    return hitl_mod


# ---------------------------------------------------------------------------
# _is_simple_query classification tests
# ---------------------------------------------------------------------------

class TestIsSimpleQuery:
    """AGENT-010: _is_simple_query correctly classifies SQL statements."""

    def setup_method(self):
        for key in list(sys.modules.keys()):
            if "hitl" in key:
                del sys.modules[key]
        import agents.nodes.hitl as hitl_mod
        self.hitl = hitl_mod

    def test_simple_select_star(self):
        """Single-table SELECT * is simple."""
        assert self.hitl._is_simple_query("SELECT * FROM users") is True

    def test_simple_select_with_where(self):
        """SELECT with WHERE clause is still simple."""
        assert self.hitl._is_simple_query("SELECT id, name FROM users WHERE active = 1") is True

    def test_simple_select_with_order_and_limit(self):
        """SELECT with ORDER BY and LIMIT is still simple."""
        assert self.hitl._is_simple_query(
            "SELECT * FROM products ORDER BY name LIMIT 10"
        ) is True

    def test_join_is_not_simple(self):
        """SELECT with JOIN is not simple."""
        assert self.hitl._is_simple_query(
            "SELECT u.id, o.total FROM users u JOIN orders o ON u.id = o.user_id"
        ) is False

    def test_left_join_is_not_simple(self):
        """SELECT with LEFT JOIN is not simple."""
        assert self.hitl._is_simple_query(
            "SELECT * FROM users LEFT JOIN profiles ON users.id = profiles.user_id"
        ) is False

    def test_subquery_is_not_simple(self):
        """SELECT with subquery is not simple."""
        assert self.hitl._is_simple_query(
            "SELECT * FROM (SELECT id FROM users WHERE active = 1) sub"
        ) is False

    def test_union_is_not_simple(self):
        """UNION query is not simple."""
        assert self.hitl._is_simple_query(
            "SELECT id FROM users UNION SELECT id FROM admins"
        ) is False

    def test_cte_is_not_simple(self):
        """WITH (CTE) query is not simple."""
        assert self.hitl._is_simple_query(
            "WITH active_users AS (SELECT * FROM users WHERE active=1) SELECT * FROM active_users"
        ) is False

    def test_case_insensitive_join(self):
        """JOIN detection is case-insensitive."""
        assert self.hitl._is_simple_query(
            "select * from users join orders on users.id = orders.user_id"
        ) is False


# ---------------------------------------------------------------------------
# hitl_node behaviour tests
# ---------------------------------------------------------------------------

class TestHitlNodeDisabled:
    """AGENT-010: HITL disabled -> auto_approved pass-through without interrupt."""

    def test_hitl_disabled_returns_auto_approved(self):
        """When hitl.enabled=false, node returns auto_approved immediately."""
        mock_module, mock_interrupt = _make_langgraph_types_mock()
        hitl_mod = _reload_hitl(mock_module)

        state = make_agent_state()
        state["generated_sql"] = "SELECT * FROM users JOIN orders ON users.id = orders.user_id"
        state["sql_explanation"] = "Joins users to orders"
        state["relevant_tables"] = ["users", "orders"]

        disabled_config = {
            "hitl": {"enabled": False, "timeout_seconds": 300, "auto_approve_simple": True}
        }
        with patch.object(hitl_mod, "_load_hitl_config", return_value=disabled_config["hitl"]):
            result = asyncio.run(hitl_mod.hitl_node(state))

        assert result == {"approval_status": "auto_approved"}
        mock_interrupt.assert_not_called()


class TestHitlNodeAutoApproveSimple:
    """AGENT-010: Simple queries are auto-approved when auto_approve_simple=True."""

    def test_simple_query_auto_approved(self):
        """Simple SELECT is auto-approved when auto_approve_simple=True."""
        mock_module, mock_interrupt = _make_langgraph_types_mock()
        hitl_mod = _reload_hitl(mock_module)

        state = make_agent_state()
        state["generated_sql"] = "SELECT * FROM users"
        state["sql_explanation"] = "Get all users"
        state["relevant_tables"] = ["users"]

        enabled_config = {
            "enabled": True,
            "timeout_seconds": 300,
            "auto_approve_simple": True,
        }
        with patch.object(hitl_mod, "_load_hitl_config", return_value=enabled_config):
            result = asyncio.run(hitl_mod.hitl_node(state))

        assert result == {"approval_status": "auto_approved"}
        mock_interrupt.assert_not_called()

    def test_simple_query_not_auto_approved_when_disabled(self):
        """Simple query triggers interrupt when auto_approve_simple=False."""
        # We need interrupt to raise something to stop it cleanly — use a sentinel exception
        class _InterruptRaised(Exception):
            pass

        mock_module, mock_interrupt = _make_langgraph_types_mock(
            interrupt_side_effect=_InterruptRaised("interrupted")
        )
        hitl_mod = _reload_hitl(mock_module)

        state = make_agent_state()
        state["generated_sql"] = "SELECT * FROM users"
        state["sql_explanation"] = "Get all users"
        state["relevant_tables"] = ["users"]

        config_no_auto = {
            "enabled": True,
            "timeout_seconds": 300,
            "auto_approve_simple": False,
        }
        with patch.object(hitl_mod, "_load_hitl_config", return_value=config_no_auto):
            with pytest.raises(_InterruptRaised):
                asyncio.run(hitl_mod.hitl_node(state))

        mock_interrupt.assert_called_once()


class TestHitlNodeComplexQueryInterrupt:
    """AGENT-010: Complex queries trigger LangGraph interrupt."""

    def test_complex_query_triggers_interrupt(self):
        """JOIN query causes interrupt() to be called with review payload."""
        class _InterruptRaised(Exception):
            pass

        mock_module, mock_interrupt = _make_langgraph_types_mock(
            interrupt_side_effect=_InterruptRaised("interrupted")
        )
        hitl_mod = _reload_hitl(mock_module)

        sql = "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id"
        explanation = "Retrieves user names with order totals"
        tables = ["users", "orders"]

        state = make_agent_state()
        state["generated_sql"] = sql
        state["sql_explanation"] = explanation
        state["relevant_tables"] = tables

        enabled_config = {
            "enabled": True,
            "timeout_seconds": 300,
            "auto_approve_simple": True,
        }
        with patch.object(hitl_mod, "_load_hitl_config", return_value=enabled_config):
            with pytest.raises(_InterruptRaised):
                asyncio.run(hitl_mod.hitl_node(state))

        mock_interrupt.assert_called_once()
        call_kwargs = mock_interrupt.call_args[0][0]
        assert call_kwargs["type"] == "hitl_review"
        assert call_kwargs["generated_sql"] == sql
        assert call_kwargs["sql_explanation"] == explanation
        assert call_kwargs["relevant_tables"] == tables

    def test_interrupt_payload_includes_all_fields(self):
        """Interrupt payload contains type, generated_sql, sql_explanation, relevant_tables."""
        class _InterruptRaised(Exception):
            pass

        mock_module, mock_interrupt = _make_langgraph_types_mock(
            interrupt_side_effect=_InterruptRaised("interrupted")
        )
        hitl_mod = _reload_hitl(mock_module)

        state = make_agent_state()
        state["generated_sql"] = "SELECT * FROM users u JOIN orders o ON u.id = o.id"
        state["sql_explanation"] = "Joins users to orders"
        state["relevant_tables"] = ["users", "orders"]

        enabled_config = {
            "enabled": True,
            "timeout_seconds": 300,
            "auto_approve_simple": True,
        }
        with patch.object(hitl_mod, "_load_hitl_config", return_value=enabled_config):
            with pytest.raises(_InterruptRaised):
                asyncio.run(hitl_mod.hitl_node(state))

        payload = mock_interrupt.call_args[0][0]
        assert "type" in payload
        assert "generated_sql" in payload
        assert "sql_explanation" in payload
        assert "relevant_tables" in payload


class TestHitlNodeResumption:
    """AGENT-010: Node handles resumption states (approved/rejected/edited)."""

    def test_approved_resumption_passes_through(self):
        """When approval_status='approved' (resumed), node returns empty dict."""
        mock_module, mock_interrupt = _make_langgraph_types_mock()
        hitl_mod = _reload_hitl(mock_module)

        state = make_agent_state()
        state["generated_sql"] = "SELECT * FROM users JOIN orders ON users.id = orders.user_id"
        state["approval_status"] = "approved"

        enabled_config = {
            "enabled": True,
            "timeout_seconds": 300,
            "auto_approve_simple": True,
        }
        with patch.object(hitl_mod, "_load_hitl_config", return_value=enabled_config):
            result = asyncio.run(hitl_mod.hitl_node(state))

        assert result == {}
        mock_interrupt.assert_not_called()

    def test_rejected_resumption_sets_error_log(self):
        """When approval_status='rejected' (resumed), node sets error_log."""
        mock_module, mock_interrupt = _make_langgraph_types_mock()
        hitl_mod = _reload_hitl(mock_module)

        state = make_agent_state()
        state["generated_sql"] = "SELECT * FROM users JOIN orders ON users.id = orders.user_id"
        state["approval_status"] = "rejected"

        enabled_config = {
            "enabled": True,
            "timeout_seconds": 300,
            "auto_approve_simple": True,
        }
        with patch.object(hitl_mod, "_load_hitl_config", return_value=enabled_config):
            result = asyncio.run(hitl_mod.hitl_node(state))

        assert result["approval_status"] == "rejected"
        assert result["error_log"] is not None
        assert result["error_log"]["error_type"] == "user_rejected"
        mock_interrupt.assert_not_called()

    def test_edited_resumption_approves_edited_sql(self):
        """When approval_status='edited' (resumed with edited SQL), returns approved."""
        mock_module, mock_interrupt = _make_langgraph_types_mock()
        hitl_mod = _reload_hitl(mock_module)

        state = make_agent_state()
        state["generated_sql"] = "SELECT id FROM users"  # edited SQL now in state
        state["approval_status"] = "edited"

        enabled_config = {
            "enabled": True,
            "timeout_seconds": 300,
            "auto_approve_simple": True,
        }
        with patch.object(hitl_mod, "_load_hitl_config", return_value=enabled_config):
            result = asyncio.run(hitl_mod.hitl_node(state))

        assert result == {"approval_status": "approved"}
        mock_interrupt.assert_not_called()
