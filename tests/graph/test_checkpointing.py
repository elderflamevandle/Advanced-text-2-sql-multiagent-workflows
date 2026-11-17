"""Checkpointer factory and lazy graph construction (graph/checkpointing.py, graph/builder.py)."""
import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from graph.builder import build_graph
from graph.checkpointing import (
    CheckpointConfigError,
    checkpoint_backend,
    open_checkpointer,
    postgres_conninfo,
    redact_url,
    resolve_checkpoint_url,
    sqlite_path_from_url,
)
from tests.graph.test_graph_e2e import ROWS, _fake_get_llm, _initial_state

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _hitl_disabled(monkeypatch):
    import agents.nodes.hitl as hitl
    monkeypatch.setattr(hitl, "_load_hitl_config", lambda: {"enabled": False})


@pytest.fixture
def manager():
    mgr = MagicMock()
    mgr.execute_query.return_value = list(ROWS)
    return mgr


def _patches(manager):
    return (
        patch("llm.fallback.get_llm", _fake_get_llm()),
        patch("database.manager.get_active_manager", return_value=manager),
        patch("vector.retriever.get_retriever", side_effect=RuntimeError("no vector store")),
    )


# --- URL handling -----------------------------------------------------------

def test_default_url_is_memory(monkeypatch):
    monkeypatch.delenv("T2S_CHECKPOINT_URL", raising=False)
    assert resolve_checkpoint_url() == "memory://"
    assert checkpoint_backend() == "memory"


def test_url_comes_from_env_and_argument_wins(monkeypatch):
    monkeypatch.setenv("T2S_CHECKPOINT_URL", "sqlite:///a.db")
    assert checkpoint_backend() == "sqlite"
    assert checkpoint_backend("memory://") == "memory"


@pytest.mark.parametrize("url,backend", [
    ("memory://", "memory"),
    ("sqlite:///x/y.db", "sqlite"),
    ("postgresql://u:p@h:5432/d", "postgres"),
    ("postgres://u:p@h/d", "postgres"),
    ("postgresql+psycopg://u:p@h/d", "postgres"),
    ("sqlite+aiosqlite:///x.db", "sqlite"),
    ("SQLite:///x.db", "sqlite"),
])
def test_backend_for_scheme(url, backend):
    assert checkpoint_backend(url) == backend


def test_sqlite_paths():
    assert sqlite_path_from_url("sqlite:///rel/file.db") == "rel/file.db"
    assert sqlite_path_from_url("sqlite:////abs/file.db") == "/abs/file.db"
    assert sqlite_path_from_url("sqlite:///:memory:") == ":memory:"
    with pytest.raises(CheckpointConfigError):
        sqlite_path_from_url("sqlite://file.db")
    with pytest.raises(CheckpointConfigError):
        sqlite_path_from_url("sqlite:///")


def test_postgres_conninfo_and_redaction():
    assert postgres_conninfo("postgres://u:p@h/d") == "postgresql://u:p@h/d"
    assert postgres_conninfo("postgresql+psycopg://u:p@h/d") == "postgresql://u:p@h/d"
    assert redact_url("postgresql://u:secret@h:5432/d") == "postgresql://u:***@h:5432/d"
    assert redact_url("memory://") == "memory://"


@pytest.mark.parametrize("url", ["mysql://u:hunter2@h/d", "redis://h", "nonsense", "file.db"])
def test_bad_url_raises_clear_error_without_leaking_password(url):
    async def _open():
        async with open_checkpointer(url):
            pass

    with pytest.raises(CheckpointConfigError) as exc:
        asyncio.run(_open())
    assert "memory://" in str(exc.value) and "sqlite:///" in str(exc.value)
    assert "hunter2" not in str(exc.value)


# --- memory -----------------------------------------------------------------

def test_memory_checkpointer_and_build_graph_default(manager):
    from langgraph.checkpoint.memory import MemorySaver

    assert isinstance(build_graph().checkpointer, MemorySaver)
    assert build_graph().checkpointer is not build_graph().checkpointer

    async def _run():
        async with open_checkpointer("memory://") as saver:
            assert isinstance(saver, MemorySaver)
            graph = build_graph(saver)
            assert graph.checkpointer is saver
            config = {"configurable": {"thread_id": "t1"}}
            await graph.ainvoke(_initial_state(), config=config)
            return (await graph.aget_state(config)).values

    p1, p2, p3 = _patches(manager)
    with p1, p2, p3:
        values = asyncio.run(_run())
    assert values["db_results"] == ROWS


# --- sqlite -----------------------------------------------------------------

def test_sqlite_checkpointer_persists_thread_across_graphs(tmp_path, manager):
    db_file = tmp_path / "nested" / "dir" / "checkpoints.db"   # parent dirs do not exist yet
    url = f"sqlite:///{db_file}"
    thread = {"configurable": {"thread_id": str(uuid.uuid4())}}
    other = {"configurable": {"thread_id": str(uuid.uuid4())}}

    async def _first():
        async with open_checkpointer(url) as saver:
            graph = build_graph(saver)
            return await graph.ainvoke(_initial_state(), config=thread)

    async def _second():
        # A separate saver, connection, graph and event loop: only the file is shared.
        async with open_checkpointer(url) as saver:
            graph = build_graph(saver)
            mine = await graph.aget_state(thread)
            theirs = await graph.aget_state(other)
            history = [s async for s in graph.aget_state_history(thread)]
            return mine, theirs, history

    p1, p2, p3 = _patches(manager)
    with p1, p2, p3:
        result = asyncio.run(_first())
    assert db_file.exists()

    mine, theirs, history = asyncio.run(_second())
    assert mine.values["final_answer"] == result["final_answer"]
    assert mine.values["db_results"] == ROWS
    assert mine.values["user_query"] == "list all artists"
    assert mine.next == ()
    assert len(history) > 1, "one checkpoint per step is expected"
    assert theirs.values == {}, "an unknown thread has no state"


def test_sqlite_setup_is_idempotent_on_existing_file(tmp_path):
    url = f"sqlite:///{tmp_path / 'c.db'}"

    async def _open_twice():
        for _ in range(2):
            async with open_checkpointer(url) as saver:
                assert type(saver).__name__ == "AsyncSqliteSaver"

    asyncio.run(_open_twice())


# --- postgres (no server: driver objects are replaced by fakes) --------------

def test_postgres_uses_pool_runs_setup_once_and_closes(monkeypatch):
    import langgraph.checkpoint.postgres.aio as pg_aio
    import psycopg_pool

    events = []

    class FakePool:
        def __init__(self, conninfo, **kwargs):
            self.conninfo, self.kwargs = conninfo, kwargs
            events.append("init")

        async def open(self):
            events.append("open")

        async def close(self):
            events.append("close")

    class FakeSaver:
        def __init__(self, conn):
            self.conn = conn

        async def setup(self):
            events.append("setup")

    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", FakePool)
    monkeypatch.setattr(pg_aio, "AsyncPostgresSaver", FakeSaver)
    monkeypatch.setenv("T2S_CHECKPOINT_POOL_SIZE", "4")

    async def _run():
        async with open_checkpointer("postgres://app:pw@db:5432/t2s") as saver:
            events.append("body")
            return saver

    saver = asyncio.run(_run())
    assert events == ["init", "open", "setup", "body", "close"]
    pool = saver.conn
    assert pool.conninfo == "postgresql://app:pw@db:5432/t2s"
    assert pool.kwargs["open"] is False and pool.kwargs["max_size"] == 4
    assert pool.kwargs["kwargs"]["autocommit"] is True
    assert pool.kwargs["kwargs"]["prepare_threshold"] == 0


def test_postgres_pool_is_closed_when_setup_fails(monkeypatch):
    import langgraph.checkpoint.postgres.aio as pg_aio
    import psycopg_pool

    closed = []

    class FakePool:
        def __init__(self, conninfo, **kwargs):
            pass

        async def open(self):
            pass

        async def close(self):
            closed.append(True)

    class FailingSaver:
        def __init__(self, conn):
            pass

        async def setup(self):
            raise RuntimeError("permission denied for schema public")

    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", FakePool)
    monkeypatch.setattr(pg_aio, "AsyncPostgresSaver", FailingSaver)

    async def _run():
        async with open_checkpointer("postgresql://app:pw@db/t2s"):
            pass

    with pytest.raises(RuntimeError, match="permission denied"):
        asyncio.run(_run())
    assert closed == [True]


# --- lazy import -------------------------------------------------------------

def test_importing_builder_builds_nothing_until_compiled_graph_is_used():
    code = (
        "import graph.builder as b\n"
        "assert b._compiled_graph is None, 'graph built at import time'\n"
        "assert 'compiled_graph' not in vars(b)\n"
        "from graph.builder import compiled_graph\n"
        "assert compiled_graph is b.get_compiled_graph() is b.compiled_graph\n"
        "assert hasattr(compiled_graph, 'ainvoke')\n"
        "try:\n"
        "    b.no_such_name\n"
        "except AttributeError:\n"
        "    print('ok')\n"
    )
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("ok")


def test_reset_compiled_graph_builds_a_new_default():
    import graph.builder as b

    first = b.get_compiled_graph()
    assert b.compiled_graph is first
    b.reset_compiled_graph()
    try:
        assert b.get_compiled_graph() is not first
    finally:
        b._compiled_graph = first   # other test modules imported this instance
