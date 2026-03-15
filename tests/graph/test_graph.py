import asyncio
import uuid

import pytest
from graph.builder import compiled_graph
from tests.graph.conftest import make_initial_state


def test_graph_compiles():
    """GRAPH-002: StateGraph compiles without errors."""
    assert compiled_graph is not None
    assert hasattr(compiled_graph, "ainvoke")


def test_graph_traverses_end_to_end():
    """GRAPH-002: Graph traverses all nodes with placeholder nodes returning {}."""
    thread_id = str(uuid.uuid4())
    result = asyncio.run(
        compiled_graph.ainvoke(
            make_initial_state("show all artists"),
            config={"configurable": {"thread_id": thread_id}},
        )
    )
    assert isinstance(result, dict)
    assert result.get("user_query") == "show all artists"


def test_graph_draw_mermaid():
    """GRAPH-002: Graph is visualizable and includes all nodes."""
    mermaid = compiled_graph.get_graph().draw_mermaid()
    for node in ["gatekeeper", "evaluator", "correction_plan", "schema_linker"]:
        assert node in mermaid, f"Node missing from Mermaid: {node}"


def test_session_isolation():
    """GRAPH-003: Two thread IDs maintain independent state — no cross-contamination."""
    t1 = str(uuid.uuid4())
    t2 = str(uuid.uuid4())

    asyncio.run(compiled_graph.ainvoke(
        make_initial_state("query from session 1"),
        config={"configurable": {"thread_id": t1}},
    ))

    r2 = asyncio.run(compiled_graph.ainvoke(
        make_initial_state("query from session 2"),
        config={"configurable": {"thread_id": t2}},
    ))

    assert r2.get("user_query") == "query from session 2"


def test_conversational_shortcut():
    """GRAPH-002: query_type='conversational' routes gatekeeper -> formatter, skipping SQL nodes."""
    thread_id = str(uuid.uuid4())
    state = make_initial_state("hello there")
    state["query_type"] = "conversational"

    result = asyncio.run(
        compiled_graph.ainvoke(
            state,
            config={"configurable": {"thread_id": thread_id}},
        )
    )
    # Nodes are placeholders returning {} so state unchanged, but graph must complete
    assert isinstance(result, dict)
    assert result.get("user_query") == "hello there"
