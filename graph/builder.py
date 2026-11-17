"""Graph assembly. Importing this module builds nothing: see get_compiled_graph()."""
import threading

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from graph.state import AgentState
from graph.conditions import (
    route_after_correction_plan,
    route_after_executor,
    route_after_gatekeeper,
    route_after_hitl,
    route_after_planner,
    route_unless_llm_failed,
)
from graph.resilience import resilient_node
from agents.nodes import (
    gatekeeper_node,
    schema_linker_node,
    query_planner_node,
    sql_generator_node,
    hitl_node,
    executor_node,
    correction_plan_node,
    correction_sql_node,
    formatter_node,
    evaluator_node,
)


_NODES = {
    "gatekeeper": gatekeeper_node,
    "schema_linker": schema_linker_node,
    "query_planner": query_planner_node,
    "sql_generator": sql_generator_node,
    "hitl": hitl_node,
    "executor": executor_node,
    "correction_plan_node": correction_plan_node,
    "correction_sql_node": correction_sql_node,
    "formatter": formatter_node,
    "evaluator": evaluator_node,
}


def build_graph(checkpointer=None):
    """Compile the pipeline graph.

    ``checkpointer`` is any LangGraph checkpointer, normally the one yielded by
    ``graph.checkpointing.open_checkpointer()``. ``None`` gives a fresh in-process
    MemorySaver, which is what the Streamlit app and the tests use.
    """
    wf = StateGraph(AgentState)

    # Every node is wrapped: LLM outages become an error_log update and usage entries
    # recorded by the node are returned as state updates (see graph/resilience.py).
    for name, fn in _NODES.items():
        wf.add_node(name, resilient_node(fn, name))

    wf.set_entry_point("gatekeeper")

    wf.add_conditional_edges(
        "gatekeeper",
        route_after_gatekeeper,
        {"schema_linker": "schema_linker", "formatter": "formatter"},
    )
    wf.add_edge("schema_linker", "query_planner")
    # After each LLM-calling node: go straight to the formatter if no provider answered.
    wf.add_conditional_edges(
        "query_planner",
        route_after_planner,  # formatter also when the planner asks for clarification
        {"continue": "sql_generator", "formatter": "formatter"},
    )
    wf.add_conditional_edges(
        "sql_generator",
        route_unless_llm_failed,
        {"continue": "hitl", "formatter": "formatter"},
    )
    wf.add_conditional_edges(
        "hitl",
        route_after_hitl,
        {"executor": "executor", "formatter": "formatter"},
    )
    wf.add_conditional_edges(
        "executor",
        route_after_executor,
        {"correction_plan": "correction_plan_node", "formatter": "formatter"},
    )
    wf.add_conditional_edges(
        "correction_plan_node",
        route_after_correction_plan,  # schema_linker: select tables again (at most once)
        {"continue": "correction_sql_node", "schema_linker": "schema_linker",
         "formatter": "formatter"},
    )
    wf.add_conditional_edges(  # correction loop back-edge
        "correction_sql_node",
        route_unless_llm_failed,
        {"continue": "executor", "formatter": "formatter"},
    )
    wf.add_edge("formatter", "evaluator")
    wf.add_edge("evaluator", END)

    return wf.compile(checkpointer=checkpointer if checkpointer is not None else MemorySaver())


_compiled_graph = None
_compiled_lock = threading.Lock()


def get_compiled_graph():
    """Process-wide default graph (MemorySaver), built on first use."""
    global _compiled_graph
    if _compiled_graph is None:
        with _compiled_lock:
            if _compiled_graph is None:
                _compiled_graph = build_graph()
    return _compiled_graph


def reset_compiled_graph() -> None:
    """Drop the default graph so the next access builds a new one (tests)."""
    global _compiled_graph
    with _compiled_lock:
        _compiled_graph = None


def __getattr__(name: str):
    # ``from graph.builder import compiled_graph`` keeps working, without building the
    # graph as a side effect of importing this module.
    if name == "compiled_graph":
        return get_compiled_graph()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
