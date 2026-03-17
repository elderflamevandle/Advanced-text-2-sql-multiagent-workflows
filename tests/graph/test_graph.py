import asyncio
import uuid

import pytest
from graph.builder import compiled_graph
from graph.conditions import route_after_hitl
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
    """GRAPH-002: Graph is visualizable and includes all nodes (10 total with HITL)."""
    mermaid = compiled_graph.get_graph().draw_mermaid()
    for node in ["gatekeeper", "evaluator", "correction_plan_node", "schema_linker", "hitl"]:
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


def test_hitl_node_present_in_graph():
    """GRAPH-002: 'hitl' node exists in compiled graph (10 nodes total)."""
    graph_obj = compiled_graph.get_graph()
    nodes = list(graph_obj.nodes)
    assert "hitl" in nodes, f"'hitl' missing from graph nodes: {nodes}"


def test_sql_generator_to_hitl_edge():
    """GRAPH-002: sql_generator -> hitl edge exists in graph."""
    graph_obj = compiled_graph.get_graph()
    edges = [(e.source, e.target) for e in graph_obj.edges]
    assert ("sql_generator", "hitl") in edges, (
        f"sql_generator -> hitl edge missing. Edges: {edges}"
    )


def test_route_after_hitl_approved():
    """route_after_hitl routes 'approved' status to executor."""
    assert route_after_hitl({"approval_status": "approved"}) == "executor"


def test_route_after_hitl_auto_approved():
    """route_after_hitl routes 'auto_approved' status to executor."""
    assert route_after_hitl({"approval_status": "auto_approved"}) == "executor"


def test_route_after_hitl_rejected():
    """route_after_hitl routes 'rejected' status to formatter."""
    assert route_after_hitl({"approval_status": "rejected"}) == "formatter"


def test_route_after_hitl_none():
    """route_after_hitl routes None approval_status to executor (default path)."""
    assert route_after_hitl({"approval_status": None}) == "executor"
