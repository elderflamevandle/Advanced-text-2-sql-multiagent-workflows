from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from graph.state import AgentState
from graph.conditions import (
    route_after_executor,
    route_after_gatekeeper,
    route_after_hitl,
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


def build_graph():
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
        route_unless_llm_failed,
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
        route_unless_llm_failed,
        {"continue": "correction_sql_node", "formatter": "formatter"},
    )
    wf.add_conditional_edges(  # correction loop back-edge
        "correction_sql_node",
        route_unless_llm_failed,
        {"continue": "executor", "formatter": "formatter"},
    )
    wf.add_edge("formatter", "evaluator")
    wf.add_edge("evaluator", END)

    return wf.compile(checkpointer=MemorySaver())


compiled_graph = build_graph()
