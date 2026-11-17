"""Unit tests for agents/nodes/formatter.py."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agents.nodes.formatter as formatter
from llm.errors import LLMUnavailableError
from tests.agents.conftest import make_agent_state


def _llm_mock(content="Rock has the most tracks (1,297)."):
    response = MagicMock()
    response.content = content
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=response)
    return llm


def _state(**overrides):
    state = make_agent_state(user_query="Which genre has the most tracks?")
    state["generated_sql"] = "SELECT Genre, COUNT(*) AS n FROM Track GROUP BY Genre"
    state.update(overrides)
    return state


def _run(state, llm=None, flag=True):
    with patch.object(formatter, "_summarize_with_llm_enabled", return_value=flag), \
         patch("llm.fallback.get_llm", return_value=llm) as get_llm:
        result = asyncio.run(formatter.formatter_node(state))
    return result["final_answer"], get_llm


def _prompt_text(llm):
    messages = llm.ainvoke.call_args.args[0]
    return "\n".join(m.content for m in messages)


ROWS = [{"Genre": "Rock", "n": 1297}, {"Genre": "Latin", "n": 579}]


# --- Path A: LLM summary -----------------------------------------------------

def test_llm_answer_is_returned_and_grounded_in_rows():
    llm = _llm_mock()
    answer, get_llm = _run(_state(db_results=ROWS), llm)

    assert answer == "Rock has the most tracks (1,297)."
    assert get_llm.call_args.kwargs["node"] == "formatter"
    prompt = _prompt_text(llm)
    assert "Which genre has the most tracks?" in prompt
    assert "SELECT Genre, COUNT(*) AS n FROM Track GROUP BY Genre" in prompt
    assert '"Genre": "Rock"' in prompt and "1297" in prompt
    assert "Rows returned: 2" in prompt
    assert "Never invent" in prompt


def test_llm_prompt_caps_rows_and_cell_length():
    rows = [{"id": i, "note": "x" * 1000} for i in range(50)]
    llm = _llm_mock("Showing the first rows.")
    _run(_state(db_results=rows), llm)

    prompt = _prompt_text(llm)
    assert "Rows returned: 50" in prompt
    assert f"Rows shown: {formatter.MAX_ROWS_FOR_LLM}" in prompt
    assert '"id": 19' in prompt and '"id": 20' not in prompt
    assert "x" * (formatter.MAX_CELL_CHARS + 1) not in prompt


def test_truncation_is_passed_to_llm_and_always_mentioned():
    meta = {"truncated": True, "row_count": 1000}
    llm = _llm_mock("Rock leads with 1,297 tracks.")  # model forgot to mention truncation
    answer, _ = _run(_state(db_results=ROWS, execution_metadata=meta), llm)

    prompt = _prompt_text(llm)
    assert "Result truncated: yes" in prompt
    assert "Rows returned: 1000" in prompt
    assert answer.startswith("Rock leads with 1,297 tracks.")
    assert "truncated" in answer and "1000" in answer


def test_truncation_note_not_duplicated_when_llm_mentions_it():
    meta = {"truncated": True, "row_count": 1000}
    llm = _llm_mock("Rock leads. Note the result was truncated at 1000 rows.")
    answer, _ = _run(_state(db_results=ROWS, execution_metadata=meta), llm)
    assert answer.lower().count("truncated") == 1


def test_non_json_values_do_not_break_the_llm_path():
    import datetime
    import decimal
    rows = [{"d": datetime.date(2024, 1, 2), "amount": decimal.Decimal("9.99"), "b": b"\x00"}]
    llm = _llm_mock("One row.")
    answer, _ = _run(_state(db_results=rows), llm)
    assert answer == "One row."
    assert "2024-01-02" in _prompt_text(llm)


# --- Path A: deterministic fallback -------------------------------------------

@pytest.mark.parametrize("failure", [
    LLMUnavailableError("all providers failed"),
    RuntimeError("unexpected"),
])
def test_llm_failure_falls_back_to_deterministic_summary(failure):
    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=failure)
    answer, _ = _run(_state(db_results=ROWS), llm)
    assert "The query returned 2 rows." in answer
    assert "Genre: Rock" in answer and "n: 1297" in answer


def test_get_llm_itself_failing_falls_back():
    with patch.object(formatter, "_summarize_with_llm_enabled", return_value=True), \
         patch("llm.fallback.get_llm", side_effect=RuntimeError("config broken")):
        result = asyncio.run(formatter.formatter_node(_state(db_results=ROWS)))
    assert "The query returned 2 rows." in result["final_answer"]


def test_empty_llm_reply_falls_back():
    answer, _ = _run(_state(db_results=ROWS), _llm_mock("   "))
    assert "The query returned 2 rows." in answer


def test_flag_off_uses_deterministic_summary_without_llm_call():
    llm = _llm_mock()
    answer, get_llm = _run(_state(db_results=ROWS), llm, flag=False)
    assert get_llm.call_count == 0
    assert llm.ainvoke.call_count == 0
    assert "The query returned 2 rows." in answer


def test_flag_is_read_from_config(monkeypatch):
    import llm.config as llm_config
    monkeypatch.setattr(llm_config, "load_config",
                        lambda: {"app": {"summarize_results_with_llm": False}})
    assert formatter._summarize_with_llm_enabled() is False
    monkeypatch.setattr(llm_config, "load_config", lambda: {"app": {}})
    assert formatter._summarize_with_llm_enabled() is True


def test_repo_config_enables_llm_summary_by_default():
    from llm.config import get_section
    assert get_section("app").get("summarize_results_with_llm") is True


def test_no_rows_needs_no_llm_call():
    llm = _llm_mock()
    answer, get_llm = _run(_state(db_results=[]), llm)
    assert get_llm.call_count == 0
    assert answer == "The query ran successfully but returned no rows."


def test_deterministic_single_value():
    answer, _ = _run(_state(db_results=[{"total": 2328.6}]), None, flag=False)
    assert answer == "total: 2328.6"


def test_deterministic_preview_is_capped_and_mentions_truncation():
    rows = [{"id": i} for i in range(1000)]
    meta = {"truncated": True, "row_count": 1000}
    answer, _ = _run(_state(db_results=rows, execution_metadata=meta), None, flag=False)
    assert "The query returned 1000 rows. Here are the first 5:" in answer
    assert "- id: 4" in answer and "- id: 5" not in answer
    assert "truncated" in answer


# --- Path B: failures ----------------------------------------------------------

def _error(error_type, message="details", hint=""):
    return {"error_type": error_type, "message": message, "dialect": "sqlite",
            "failed_sql": None, "hint": hint}


def test_blocked_query_message():
    state = _state(error_log=_error("blocked_query", "DROP is not allowed"))
    answer, _ = _run(state)
    assert "safety check blocked it" in answer
    assert "DROP is not allowed" in answer
    assert "attempt(s)" not in answer


def test_user_rejected_message():
    state = _state(error_log=_error("user_rejected"), approval_status="rejected")
    answer, _ = _run(state)
    assert "rejected during review" in answer
    assert "attempt(s)" not in answer


def test_llm_outage_message():
    state = _state(error_log=LLMUnavailableError("boom").to_error_log("sqlite"))
    answer, get_llm = _run(state)
    assert "no language model provider is reachable" in answer
    assert "GROQ_API_KEY" in answer
    assert "attempt(s)" not in answer
    assert get_llm.call_count == 0


def test_no_connection_message():
    answer, _ = _run(_state(error_log=_error("no_connection")))
    assert "No database is connected" in answer


def test_exhausted_corrections_keep_the_attempt_report():
    state = _state(
        error_log=_error("syntax_error", "near FROM"),
        sql_history=[{"sql": "SELEC 1", "error": {"message": "near SELEC"}, "attempt_num": 0}],
        generated_sql="SELECT 1 FROM",
    )
    answer, _ = _run(state)
    assert "couldn't generate a working query after 1 attempt(s)" in answer
    assert "near FROM" in answer
    assert "Attempt 1: SELEC 1" in answer
    assert "SELECT 1 FROM" in answer


def test_string_error_log_is_handled():
    answer, _ = _run(_state(error_log="Unsafe or unsupported SQL statement"))
    assert "Unsafe or unsupported SQL statement" in answer


# --- Path C: conversational ------------------------------------------------------

def test_conversational_answer_is_kept():
    answer, get_llm = _run(_state(final_answer="Hi! Ask me about your data."))
    assert answer == "Hi! Ask me about your data."
    assert get_llm.call_count == 0


def test_conversational_default_when_nothing_set():
    answer, _ = _run(_state())
    assert answer == "I cannot answer that with the available database."
