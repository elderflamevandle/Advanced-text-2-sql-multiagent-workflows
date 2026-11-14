from typing import Annotated, Optional, TypedDict
from langgraph.graph.message import add_messages


def merge_usage_metadata(current: Optional[list], update: Optional[list]) -> list:
    """Reducer for AgentState.usage_metadata.

    - update is a list: its entries are appended to the accumulated list.
    - update is None: the list is reset to []. The UI passes usage_metadata=None in the
      initial state of every new query so totals do not leak across queries that share a
      checkpoint thread.
    """
    if update is None:
        return []
    return list(current or []) + list(update)


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    user_query: str
    resolved_query: Optional[str]
    db_type: str
    # Tenant-scoped database handle (see database/registry.py). Identifiers only:
    # never credentials or manager objects, because state is checkpointed.
    tenant_id: Optional[str]
    connection_id: Optional[str]
    query_type: Optional[str]
    schema: Optional[dict]
    relevant_tables: Optional[list]
    query_plan: Optional[str]
    generated_sql: Optional[str]
    sql_explanation: Optional[str]
    db_results: Optional[list]
    execution_metadata: Optional[dict]
    approval_status: Optional[str]
    error_log: Optional[str]
    correction_plan: Optional[dict]   # structured diagnosis from correction_plan_node
    sql_history: Optional[list]       # [{sql: str, error: dict, attempt_num: int}] audit trail
    # One entry per LLM call: {provider, model, node_name, input_tokens, output_tokens,
    # total_tokens, estimated_cost_usd, timestamp}. Reducer field: nodes return only their
    # NEW entries and they accumulate; an update of None resets the list (new query).
    usage_metadata: Annotated[list, merge_usage_metadata]
    retry_count: int
    final_answer: Optional[str]
