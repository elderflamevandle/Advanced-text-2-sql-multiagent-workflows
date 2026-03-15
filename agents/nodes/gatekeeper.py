"""Gatekeeper agent node: classifies queries, rewrites follow-ups, enforces NL safety."""
import json
import logging
from graph.state import AgentState

logger = logging.getLogger(__name__)

_GATEKEEPER_PROMPT = """You are a query classification assistant for a Text-to-SQL pipeline.

Given a user query and optional chat history, classify the query into exactly one of these categories:
- sql: The user wants to query a database (retrieve, aggregate, filter, join data)
- conversational: The user is chatting, asking about the system, or making non-data requests
- follow_up: The user is following up on a previous query and the question only makes sense in context
- ambiguous: The query is too vague to determine intent without clarification

Also extract:
- intent: A short phrase describing what the user wants
- response: For conversational/ambiguous categories, provide a helpful response message. For sql/follow_up, leave empty.

Respond with ONLY valid JSON in this exact format:
{{
  "category": "<sql|conversational|follow_up|ambiguous>",
  "intent": "<short description>",
  "response": "<response text or empty string>"
}}

Chat history: {chat_history}
User query: {user_query}"""

_DESTRUCTIVE_PATTERNS = ["delete", "drop", "truncate", "remove all", "destroy", "erase"]


def _parse_json_response(content: str) -> dict:
    """Strip markdown fences and parse JSON. Falls back to sql category on error."""
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
    try:
        return json.loads(text)
    except (ValueError, json.JSONDecodeError):
        logger.warning("Failed to parse LLM JSON response: %s", content[:200])
        return {"category": "sql", "intent": "unknown", "response": ""}


async def gatekeeper_node(state: AgentState) -> dict:
    """Classifies user query, rewrites follow-ups, blocks destructive NL, checks db connection."""
    user_query = state.get("user_query", "")

    # 1. DB connection check
    if state.get("db_manager") is None:
        logger.info("gatekeeper_node: no db_manager, returning early")
        return {
            "final_answer": "Please connect to a database first.",
            "query_type": "conversational",
        }

    # 2. NL safety check — block destructive intent before LLM call
    query_lower = user_query.lower()
    if any(pattern in query_lower for pattern in _DESTRUCTIVE_PATTERNS):
        logger.info("gatekeeper_node: blocked destructive query: %s", user_query)
        return {
            "final_answer": "I can only run read-only queries. I cannot delete or modify data.",
            "query_type": "conversational",
        }

    # 3. Lazy import ChatGroq
    from langchain_groq import ChatGroq
    from langchain_core.messages import SystemMessage, HumanMessage

    llm = ChatGroq(model="llama-3.3-70b-versatile", temperature=0, max_tokens=1024)

    # 4. Build messages for classification
    chat_history = state.get("messages", [])
    system_prompt = _GATEKEEPER_PROMPT.format(
        chat_history=chat_history,
        user_query=user_query,
    )
    messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_query)]

    # 5. Classify
    response = await llm.ainvoke(messages)
    parsed = _parse_json_response(response.content)

    category = parsed.get("category", "sql")
    intent = parsed.get("intent", "")
    response_text = parsed.get("response", "")

    result: dict = {"query_type": category, "intent": intent}

    # 6. Follow-up rewrite
    if category == "follow_up":
        rewrite_messages = [
            SystemMessage(content="Rewrite the following follow-up query as a complete standalone question, incorporating any necessary context from the chat history. Return only the rewritten query text, nothing else."),
            HumanMessage(content=f"Chat history: {chat_history}\nFollow-up query: {user_query}"),
        ]
        rewrite_response = await llm.ainvoke(rewrite_messages)
        result["resolved_query"] = rewrite_response.content.strip()

    # 7. Conversational / ambiguous → set final_answer
    if category in ("conversational", "ambiguous"):
        result["final_answer"] = response_text

    logger.info("gatekeeper_node: category=%s intent=%s", category, intent)
    return result
