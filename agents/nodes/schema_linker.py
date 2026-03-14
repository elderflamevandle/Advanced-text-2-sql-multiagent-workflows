import logging
from graph.state import AgentState

logger = logging.getLogger(__name__)


async def schema_linker_node(state: AgentState) -> dict:
    """Retrieves relevant schema tables via vector search."""
    logger.info("schema_linker_node called for query: %s", state.get("user_query", ""))
    return {}
