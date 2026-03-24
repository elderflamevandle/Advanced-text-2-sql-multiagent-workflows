from typing import Annotated, Optional, TypedDict
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    user_query: str
    resolved_query: Optional[str]
    db_type: str
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
    usage_metadata: Optional[list]    # list[dict] — one entry per LLM call: {provider, model, node_name, input_tokens, output_tokens, total_tokens, estimated_cost_usd, timestamp}
    retry_count: int
    final_answer: Optional[str]
