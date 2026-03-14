import logging
from graph.state import AgentState

logger = logging.getLogger(__name__)


async def sql_generator_node(state: AgentState) -> dict:
    """Translates query plan to dialect-specific SQL."""
    logger.info("sql_generator_node called for query: %s", state.get("user_query", ""))
    return {}
