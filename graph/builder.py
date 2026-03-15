from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from graph.state import AgentState
from graph.conditions import route_after_gatekeeper, route_after_hitl, route_after_executor
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


def build_graph():
    wf = StateGraph(AgentState)

    wf.add_node("gatekeeper", gatekeeper_node)
    wf.add_node("schema_linker", schema_linker_node)
    wf.add_node("query_planner", query_planner_node)
    wf.add_node("sql_generator", sql_generator_node)
    wf.add_node("hitl", hitl_node)
    wf.add_node("executor", executor_node)
    wf.add_node("correction_plan", correction_plan_node)
    wf.add_node("correction_sql", correction_sql_node)
    wf.add_node("formatter", formatter_node)
    wf.add_node("evaluator", evaluator_node)

    wf.set_entry_point("gatekeeper")

    wf.add_conditional_edges(
        "gatekeeper",
        route_after_gatekeeper,
        {"schema_linker": "schema_linker", "formatter": "formatter"},
    )
    wf.add_edge("schema_linker", "query_planner")
    wf.add_edge("query_planner", "sql_generator")
    wf.add_edge("sql_generator", "hitl")
    wf.add_conditional_edges(
        "hitl",
        route_after_hitl,
        {"executor": "executor", "formatter": "formatter"},
    )
    wf.add_conditional_edges(
        "executor",
        route_after_executor,
        {"correction_plan": "correction_plan", "formatter": "formatter"},
    )
    wf.add_edge("correction_plan", "correction_sql")
    wf.add_edge("correction_sql", "executor")  # correction loop back-edge
    wf.add_edge("formatter", "evaluator")
    wf.add_edge("evaluator", END)

    return wf.compile(checkpointer=MemorySaver())


compiled_graph = build_graph()
