"""Request-scoped LLM keys: llm.context.use_llm_credentials + get_llm."""
import asyncio

import pytest

import llm.fallback as fallback
from llm import groq_client, openai_client
from llm.context import get_llm_credential, use_llm_credentials


class FakeLLM:
    def __init__(self, provider, api_key):
        self.provider = provider
        self.api_key = api_key      # None means "taken from the environment"
        self.model_name = f"fake-{provider}"


@pytest.fixture(autouse=True)
def fake_providers(monkeypatch):
    monkeypatch.setattr(fallback, "_load_llm_config", lambda: {"ollama_enabled": False})
    monkeypatch.setattr(groq_client, "_make_groq_llm",
                        lambda cfg, api_key=None: FakeLLM("groq", api_key))
    monkeypatch.setattr(openai_client, "_make_openai_llm",
                        lambda cfg, complexity="simple", api_key=None: FakeLLM("openai", api_key))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fallback.clear_llm_cache()
    yield
    fallback.clear_llm_cache()


def providers(node="test"):
    return dict(fallback.get_llm(node)._providers)


def test_falls_back_to_env_when_unset(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "env-groq")
    assert get_llm_credential("groq") is None
    llms = providers()
    assert list(llms) == ["groq"]
    assert llms["groq"].api_key is None          # built the legacy way, from the env


def test_no_keys_anywhere_means_no_providers():
    assert providers() == {}


def test_scoped_key_wins_over_env_and_is_restored(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "env-groq")
    env_client = providers()["groq"]
    with use_llm_credentials(groq_api_key="tenant-groq"):
        scoped = providers()["groq"]
        assert scoped.api_key == "tenant-groq"
        assert scoped is not env_client
    assert get_llm_credential("groq") is None
    assert providers()["groq"] is env_client


def test_nested_contexts(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai")
    with use_llm_credentials(groq_api_key="outer-groq"):
        assert providers()["groq"].api_key == "outer-groq"
        assert providers()["openai"].api_key is None           # env
        with use_llm_credentials(openai_api_key="inner-openai"):
            llms = providers()
            assert llms["groq"].api_key == "outer-groq"        # inherited
            assert llms["openai"].api_key == "inner-openai"
            with use_llm_credentials(groq_api_key="innermost-groq"):
                assert providers()["groq"].api_key == "innermost-groq"
            assert providers()["groq"].api_key == "outer-groq"
        assert get_llm_credential("openai") is None
        assert providers()["openai"].api_key is None
    assert get_llm_credential("groq") is None
    assert "groq" not in providers()


def test_context_restored_after_exception():
    with pytest.raises(RuntimeError):
        with use_llm_credentials(groq_api_key="k"):
            raise RuntimeError("boom")
    assert get_llm_credential("groq") is None


def test_empty_string_disables_provider_without_env_fallback(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "env-groq")
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai")
    with use_llm_credentials(groq_api_key=""):
        assert list(providers()) == ["openai"]
    assert list(providers()) == ["groq", "openai"]


def test_different_keys_never_share_a_client_same_key_reuses_it():
    with use_llm_credentials(groq_api_key="tenant-a", openai_api_key="oa-a"):
        a1, a2 = providers(), providers()
    with use_llm_credentials(groq_api_key="tenant-b", openai_api_key="oa-b"):
        b = providers()
    assert a1["groq"] is a2["groq"] and a1["openai"] is a2["openai"]
    assert a1["groq"] is not b["groq"] and a1["openai"] is not b["openai"]
    assert (a1["groq"].api_key, b["groq"].api_key) == ("tenant-a", "tenant-b")


def test_isolation_between_concurrent_tasks(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "env-groq")

    async def request(i):
        key = f"tenant-{i}"
        seen = []
        with use_llm_credentials(groq_api_key=key):
            for _ in range(5):
                await asyncio.sleep(0)            # let the other requests interleave
                seen.append(providers()["groq"].api_key)
        await asyncio.sleep(0)
        after = providers()["groq"].api_key       # back on the env key
        return key, seen, after

    async def no_credentials():
        out = []
        for _ in range(5):
            await asyncio.sleep(0)
            out.append(providers()["groq"].api_key)
        return out

    async def main():
        tenants = asyncio.gather(*(request(i) for i in range(25)))
        plain = asyncio.gather(*(no_credentials() for _ in range(5)))
        return await tenants, await plain

    results, plain = asyncio.run(main())
    for key, seen, after in results:
        assert seen == [key] * 5
        assert after is None
    assert all(v is None for row in plain for v in row)


def test_key_follows_awaits_spawned_tasks_and_threads():
    async def child():
        await asyncio.sleep(0)
        return providers()["groq"].api_key

    async def main():
        with use_llm_credentials(groq_api_key="req-key"):
            await asyncio.sleep(0)
            direct = providers()["groq"].api_key
            task = asyncio.create_task(child())
            gathered = await asyncio.gather(child(), child())
            threaded = await asyncio.to_thread(get_llm_credential, "groq")
        # the task was created inside the block: it keeps the request's key
        return direct, await task, gathered, threaded

    direct, spawned, gathered, threaded = asyncio.run(main())
    assert direct == spawned == threaded == "req-key"
    assert gathered == ["req-key", "req-key"]
