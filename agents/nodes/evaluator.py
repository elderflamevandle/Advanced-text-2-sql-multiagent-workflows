import logging
from graph.state import AgentState

logger = logging.getLogger(__name__)


async def evaluator_node(state: AgentState) -> dict:
    """Scores answer quality using Ragas metrics."""
    logger.info("evaluator_node called for query: %s", state.get("user_query", ""))
    return {}
