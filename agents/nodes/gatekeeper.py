import logging
from graph.state import AgentState

logger = logging.getLogger(__name__)


async def gatekeeper_node(state: AgentState) -> dict:
    """Validates query and routes to SQL or conversational path."""
    logger.info("gatekeeper_node called for query: %s", state.get("user_query", ""))
    return {}
