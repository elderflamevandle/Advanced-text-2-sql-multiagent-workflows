import asyncio
import pytest
import sys
import types
import importlib
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(input_tokens=100, output_tokens=50, model="llama-3.3-70b-versatile"):
    """Build a mock AIMessage-like response with usage_metadata."""
    resp = MagicMock()
    resp.usage_metadata = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    resp.content = "SELECT 1"
    return resp


def _make_client(groq_llm, openai_llm, ollama_llm, node="test"):
    """Construct a FallbackClient directly with mock providers."""
    from llm.fallback import FallbackClient
    from llm.usage_tracker import UsageTracker
    tracker = UsageTracker()
    return FallbackClient(groq_llm, openai_llm, ollama_llm, tracker, node_name=node), tracker


# ---------------------------------------------------------------------------
# LLM-001: Groq primary path
# ---------------------------------------------------------------------------

def test_groq_primary_success():
    """Groq serves the request; response returned directly."""
    async def _run():
        mock_groq = MagicMock()
        mock_groq.ainvoke = AsyncMock(return_value=_mock_response())
        mock_groq.model_name = "llama-3.3-70b-versatile"
        mock_openai = MagicMock()
        mock_openai.ainvoke = AsyncMock()
        mock_ollama = MagicMock()
        mock_ollama.ainvoke = AsyncMock()
        client, tracker = _make_client(mock_groq, mock_openai, mock_ollama)
        state = {"usage_metadata": None}
        result = await client.ainvoke([{"role": "user", "content": "test"}], state=state)
        assert result.content == "SELECT 1"
        assert mock_openai.ainvoke.call_count == 0  # openai not called

    asyncio.run(_run())


def test_usage_metadata_extracted():
    """After successful ainvoke, usage dict appended to state['usage_metadata']."""
    async def _run():
        mock_groq = MagicMock()
        mock_groq.ainvoke = AsyncMock(return_value=_mock_response(input_tokens=200, output_tokens=80))
        mock_groq.model_name = "llama-3.3-70b-versatile"
        mock_openai = MagicMock()
        mock_ollama = MagicMock()
        client, tracker = _make_client(mock_groq, mock_openai, mock_ollama)
        state = {"usage_metadata": None}
        await client.ainvoke([{"role": "user", "content": "test"}], state=state)
        # UsageTracker.record() was called; check entries
        assert len(tracker.entries) == 1
        entry = tracker.entries[0]
        assert entry["provider"] == "groq"
        assert entry["input_tokens"] == 200
        assert entry["output_tokens"] == 80

    asyncio.run(_run())


def test_groq_rate_limit_triggers_openai():
    """groq.RateLimitError on Groq -> FallbackClient moves to OpenAI."""
    async def _run():
        import groq as groq_module

        mock_groq = MagicMock()
        # Construct a minimal groq.RateLimitError — needs a response kwarg
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        exc = groq_module.RateLimitError("rate limit", response=mock_resp, body={})
        mock_groq.ainvoke = AsyncMock(side_effect=exc)
        mock_groq.model_name = "llama-3.3-70b-versatile"

        mock_openai = MagicMock()
        mock_openai.ainvoke = AsyncMock(return_value=_mock_response())
        mock_openai.model_name = "gpt-4o-mini"
        mock_ollama = MagicMock()

        client, _ = _make_client(mock_groq, mock_openai, mock_ollama)
        result = await client.ainvoke(["test"])
        assert mock_openai.ainvoke.call_count == 1
        assert result.content == "SELECT 1"

    asyncio.run(_run())


def test_groq_timeout_triggers_openai():
    """groq.APITimeoutError on Groq -> FallbackClient moves to OpenAI."""
    async def _run():
        import groq as groq_module

        mock_groq = MagicMock()
        exc = groq_module.APITimeoutError(request=MagicMock())
        mock_groq.ainvoke = AsyncMock(side_effect=exc)
        mock_groq.model_name = "llama-3.3-70b-versatile"

        mock_openai = MagicMock()
        mock_openai.ainvoke = AsyncMock(return_value=_mock_response())
        mock_openai.model_name = "gpt-4o-mini"
        mock_ollama = MagicMock()

        client, _ = _make_client(mock_groq, mock_openai, mock_ollama)
        result = await client.ainvoke(["test"])
        assert mock_openai.ainvoke.call_count == 1

    asyncio.run(_run())


def test_groq_connection_error_triggers_openai():
    """groq.APIConnectionError on Groq -> FallbackClient moves to OpenAI."""
    async def _run():
        import groq as groq_module

        mock_groq = MagicMock()
        exc = groq_module.APIConnectionError(request=MagicMock())
        mock_groq.ainvoke = AsyncMock(side_effect=exc)
        mock_groq.model_name = "llama-3.3-70b-versatile"

        mock_openai = MagicMock()
        mock_openai.ainvoke = AsyncMock(return_value=_mock_response())
        mock_openai.model_name = "gpt-4o-mini"
        mock_ollama = MagicMock()

        client, _ = _make_client(mock_groq, mock_openai, mock_ollama)
        result = await client.ainvoke(["test"])
        assert mock_openai.ainvoke.call_count == 1

    asyncio.run(_run())


def test_groq_5xx_triggers_openai():
    """groq.InternalServerError on Groq -> FallbackClient moves to OpenAI."""
    async def _run():
        import groq as groq_module

        mock_groq = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        exc = groq_module.InternalServerError("internal server error", response=mock_resp, body={})
        mock_groq.ainvoke = AsyncMock(side_effect=exc)
        mock_groq.model_name = "llama-3.3-70b-versatile"

        mock_openai = MagicMock()
        mock_openai.ainvoke = AsyncMock(return_value=_mock_response())
        mock_openai.model_name = "gpt-4o-mini"
        mock_ollama = MagicMock()

        client, _ = _make_client(mock_groq, mock_openai, mock_ollama)
        result = await client.ainvoke(["test"])
        assert mock_openai.ainvoke.call_count == 1

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# LLM-002: Fallback chain
# ---------------------------------------------------------------------------

def test_openai_fallback_success():
    """OpenAI serves the request when Groq fails."""
    async def _run():
        mock_groq = MagicMock()
        mock_groq.ainvoke = AsyncMock(side_effect=Exception("groq down"))
        mock_groq.model_name = "llama-3.3-70b-versatile"

        mock_openai = MagicMock()
        mock_openai.ainvoke = AsyncMock(return_value=_mock_response())
        mock_openai.model_name = "gpt-4o-mini"

        mock_ollama = MagicMock()
        mock_ollama.ainvoke = AsyncMock()

        client, tracker = _make_client(mock_groq, mock_openai, mock_ollama)
        result = await client.ainvoke(["test"])
        assert result.content == "SELECT 1"
        assert mock_ollama.ainvoke.call_count == 0  # ollama not called
        # tracker recorded openai
        assert len(tracker.entries) == 1
        assert tracker.entries[0]["provider"] == "openai"

    asyncio.run(_run())


def test_ollama_fallback_on_openai_failure():
    """Ollama serves when both Groq and OpenAI fail."""
    async def _run():
        mock_groq = MagicMock()
        mock_groq.ainvoke = AsyncMock(side_effect=Exception("groq down"))
        mock_groq.model_name = "llama-3.3-70b-versatile"

        mock_openai = MagicMock()
        mock_openai.ainvoke = AsyncMock(side_effect=Exception("openai down"))
        mock_openai.model_name = "gpt-4o-mini"

        mock_ollama = MagicMock()
        mock_ollama.ainvoke = AsyncMock(return_value=_mock_response())
        mock_ollama.model = "qwen3:8b"

        client, tracker = _make_client(mock_groq, mock_openai, mock_ollama)
        result = await client.ainvoke(["test"])
        assert result.content == "SELECT 1"
        assert mock_ollama.ainvoke.call_count == 1
        # tracker recorded ollama
        assert len(tracker.entries) == 1
        assert tracker.entries[0]["provider"] == "ollama"

    asyncio.run(_run())


def test_complex_query_uses_gpt4o():
    """When state['query_plan']['complexity'] == 'complex', OpenAI model is gpt-4o."""
    import sys
    import types
    import importlib

    cfg = {"openai_model_default": "gpt-4o-mini", "openai_model_complex": "gpt-4o"}

    mock_chat = MagicMock()
    mock_instance = MagicMock()
    mock_instance.model_name = "gpt-4o"
    mock_chat.return_value = mock_instance

    mock_module = types.ModuleType("langchain_openai")
    mock_module.ChatOpenAI = mock_chat
    old = sys.modules.get("langchain_openai")
    sys.modules["langchain_openai"] = mock_module

    try:
        import llm.openai_client as oai_mod
        importlib.reload(oai_mod)
        oai_mod._make_openai_llm(cfg, complexity="complex")
        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs.get("model") == "gpt-4o", f"Expected gpt-4o, got {call_kwargs.get('model')}"
    finally:
        if old is None:
            del sys.modules["langchain_openai"]
        else:
            sys.modules["langchain_openai"] = old
        importlib.reload(oai_mod)


def test_simple_query_uses_gpt4o_mini():
    """When complexity != 'complex', OpenAI model is gpt-4o-mini."""
    import sys
    import types
    import importlib

    cfg = {"openai_model_default": "gpt-4o-mini", "openai_model_complex": "gpt-4o"}

    mock_chat = MagicMock()
    mock_instance = MagicMock()
    mock_instance.model_name = "gpt-4o-mini"
    mock_chat.return_value = mock_instance

    mock_module = types.ModuleType("langchain_openai")
    mock_module.ChatOpenAI = mock_chat
    old = sys.modules.get("langchain_openai")
    sys.modules["langchain_openai"] = mock_module

    try:
        import llm.openai_client as oai_mod
        importlib.reload(oai_mod)
        oai_mod._make_openai_llm(cfg, complexity="simple")
        call_kwargs = mock_chat.call_args[1]
        assert call_kwargs.get("model") == "gpt-4o-mini", f"Expected gpt-4o-mini, got {call_kwargs.get('model')}"
    finally:
        if old is None:
            del sys.modules["langchain_openai"]
        else:
            sys.modules["langchain_openai"] = old
        importlib.reload(oai_mod)


def test_all_providers_fail_returns_error_dict():
    """All 3 providers fail -> returns structured error dict (not raises)."""
    async def _run():
        mock_groq = MagicMock()
        mock_groq.ainvoke = AsyncMock(side_effect=Exception("groq down"))
        mock_groq.model_name = "llama-3.3-70b-versatile"

        mock_openai = MagicMock()
        mock_openai.ainvoke = AsyncMock(side_effect=Exception("openai down"))
        mock_openai.model_name = "gpt-4o-mini"

        mock_ollama = MagicMock()
        mock_ollama.ainvoke = AsyncMock(side_effect=Exception("ollama down"))
        mock_ollama.model = "qwen3:8b"

        client, _ = _make_client(mock_groq, mock_openai, mock_ollama)
        result = await client.ainvoke(["test"])
        assert isinstance(result, dict), "Expected dict on all-fail"
        assert result.get("error_type") == "llm_all_providers_failed"
        assert "message" in result
        assert "hint" in result

    asyncio.run(_run())


def test_provider_recorded_in_state():
    """Successful provider name written into tracker entries."""
    async def _run():
        mock_groq = MagicMock()
        mock_groq.ainvoke = AsyncMock(return_value=_mock_response())
        mock_groq.model_name = "llama-3.3-70b-versatile"
        mock_openai = MagicMock()
        mock_ollama = MagicMock()

        client, tracker = _make_client(mock_groq, mock_openai, mock_ollama)
        state = {"usage_metadata": []}
        await client.ainvoke(["test"], state=state)
        assert len(tracker.entries) == 1
        assert tracker.entries[0]["provider"] == "groq"

    asyncio.run(_run())


# ---- LLM-003: Cost calculation (NOT skipped — usage_tracker.py is Wave 1) ----

def test_cost_calculation_groq():
    """calculate_cost for llama-3.3-70b-versatile is correct."""
    from llm.usage_tracker import calculate_cost
    cost = calculate_cost("llama-3.3-70b-versatile", 1000, 1000)
    assert abs(cost - 0.00138) < 0.00001, f"Expected ~0.00138, got {cost}"


def test_cost_calculation_openai():
    """calculate_cost for gpt-4o-mini is correct."""
    from llm.usage_tracker import calculate_cost
    cost = calculate_cost("gpt-4o-mini", 1000, 1000)
    assert abs(cost - 0.00075) < 0.00001, f"Expected ~0.00075, got {cost}"


def test_cost_ollama_is_zero():
    """Ollama qwen3:8b always returns $0.00 cost."""
    from llm.usage_tracker import calculate_cost
    assert calculate_cost("qwen3:8b", 9999, 9999) == 0.0


def test_cost_unknown_model_is_zero():
    """Unknown model returns $0.00 cost (safe default)."""
    from llm.usage_tracker import calculate_cost
    assert calculate_cost("unknown-model-xyz", 1000, 1000) == 0.0


# ---------------------------------------------------------------------------
# Phase 8: astream() token-level streaming behavior
# ---------------------------------------------------------------------------

def test_astream_yields_string_chunks():
    """astream() yields at least one string chunk (not an AIMessage object) on LLM success."""
    async def _run():
        mock_groq = MagicMock()
        mock_groq.model_name = "llama-3.3-70b-versatile"

        # Simulate llm.astream() yielding two chunks
        async def _fake_astream(messages):
            chunk1 = MagicMock()
            chunk1.content = "Hello"
            chunk1.usage_metadata = None
            chunk2 = MagicMock()
            chunk2.content = " world"
            chunk2.usage_metadata = {"input_tokens": 10, "output_tokens": 5}
            yield chunk1
            yield chunk2

        mock_groq.astream = _fake_astream
        mock_openai = MagicMock()
        mock_ollama = MagicMock()
        client, _ = _make_client(mock_groq, mock_openai, mock_ollama)

        chunks = []
        async for chunk in client.astream([{"role": "user", "content": "test"}]):
            chunks.append(chunk)

        assert len(chunks) >= 1, "astream() must yield at least one chunk"
        for c in chunks:
            assert isinstance(c, str), f"Expected str chunk, got {type(c).__name__}: {c!r}"

    asyncio.run(_run())


def test_astream_full_content_equals_concatenated_chunks():
    """astream() yields chunks whose concatenation equals the full response text."""
    async def _run():
        mock_groq = MagicMock()
        mock_groq.model_name = "llama-3.3-70b-versatile"

        async def _fake_astream(messages):
            for word in ["SELECT", " ", "1"]:
                chunk = MagicMock()
                chunk.content = word
                chunk.usage_metadata = None
                yield chunk

        mock_groq.astream = _fake_astream
        mock_openai = MagicMock()
        mock_ollama = MagicMock()
        client, _ = _make_client(mock_groq, mock_openai, mock_ollama)

        chunks = []
        async for chunk in client.astream(["test"]):
            chunks.append(chunk)

        full = "".join(chunks)
        assert full == "SELECT 1", f"Expected 'SELECT 1', got {full!r}"

    asyncio.run(_run())


def test_astream_records_usage_after_streaming():
    """astream() records usage_metadata via tracker.record() after streaming completes."""
    async def _run():
        mock_groq = MagicMock()
        mock_groq.model_name = "llama-3.3-70b-versatile"

        async def _fake_astream(messages):
            chunk1 = MagicMock()
            chunk1.content = "result"
            chunk1.usage_metadata = None
            yield chunk1
            chunk2 = MagicMock()
            chunk2.content = ""
            chunk2.usage_metadata = {"input_tokens": 50, "output_tokens": 20}
            yield chunk2

        mock_groq.astream = _fake_astream
        mock_openai = MagicMock()
        mock_ollama = MagicMock()
        client, tracker = _make_client(mock_groq, mock_openai, mock_ollama)

        async for _ in client.astream(["test"]):
            pass  # consume all chunks

        assert len(tracker.entries) == 1, "tracker.record() must be called once after streaming"
        entry = tracker.entries[0]
        assert entry["provider"] == "groq"
        assert entry["input_tokens"] == 50
        assert entry["output_tokens"] == 20

    asyncio.run(_run())


def test_astream_error_dict_yields_string_not_dict():
    """All providers fail -> astream() yields a string error message, not a raw dict."""
    async def _run():
        mock_groq = MagicMock()
        mock_groq.model_name = "llama-3.3-70b-versatile"
        mock_groq.astream = MagicMock(side_effect=Exception("groq astream down"))
        mock_openai = MagicMock()
        mock_openai.model_name = "gpt-4o-mini"
        mock_openai.astream = MagicMock(side_effect=Exception("openai astream down"))
        mock_ollama = MagicMock()
        mock_ollama.model = "qwen3:8b"
        mock_ollama.astream = MagicMock(side_effect=Exception("ollama astream down"))
        client, _ = _make_client(mock_groq, mock_openai, mock_ollama)

        chunks = []
        async for chunk in client.astream(["test"]):
            chunks.append(chunk)

        assert len(chunks) == 1, "Exactly one error chunk expected"
        assert isinstance(chunks[0], str), f"Error must be str, got {type(chunks[0]).__name__}"
        assert "llm_all_providers_failed" in chunks[0], f"Error dict marker missing: {chunks[0]}"

    asyncio.run(_run())
