import logging
from graph.state import AgentState

logger = logging.getLogger(__name__)


async def executor_node(state: AgentState) -> dict:
    """Executes SQL safely via DatabaseManager and captures results."""
    logger.info("executor_node called for query: %s", state.get("user_query", ""))
    return {}
