import logging
from graph.state import AgentState

logger = logging.getLogger(__name__)


async def formatter_node(state: AgentState) -> dict:
    """Converts database results to natural language answer."""
    logger.info("formatter_node called for query: %s", state.get("user_query", ""))
    return {}
