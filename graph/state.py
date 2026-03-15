from typing import Annotated, Optional, TypedDict
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    user_query: str
    resolved_query: Optional[str]
    db_type: str
    db_manager: Optional[object]
    query_type: Optional[str]
    schema: Optional[dict]
    relevant_tables: Optional[list]
    query_plan: Optional[str]
    generated_sql: Optional[str]
    db_results: Optional[list]
    error_log: Optional[str]
    retry_count: int
    final_answer: Optional[str]
