import logging
from graph.state import AgentState

logger = logging.getLogger(__name__)


async def query_planner_node(state: AgentState) -> dict:
    """Generates Chain-of-Thought SQL execution plan."""
    logger.info("query_planner_node called for query: %s", state.get("user_query", ""))
    return {}
