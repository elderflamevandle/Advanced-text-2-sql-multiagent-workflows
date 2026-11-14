"""Gatekeeper agent node: classifies queries and rewrites follow-ups.

Read-only safety is enforced on the generated SQL (database.safety.scan_sql), not on the
wording of the question, so questions such as "which customers dropped off last month"
are answered normally.
"""
import json
import logging

from graph.state import AgentState

logger = logging.getLogger(__name__)

_GATEKEEPER_PROMPT = """You are a query classification assistant for a Text-to-SQL pipeline.

Given a user query and optional chat history, classify the query into exactly one of these categories:
- sql: The user wants to query a database (retrieve, aggregate, filter, join data)
- conversational: The user is chatting, asking about the system, or making non-data requests.
  This system is read-only: if the user asks to change the database itself (insert, update,
  delete or drop data or tables), use this category and explain that only read-only queries
  are supported. Questions that merely mention such words ("deleted accounts",
  "customers who dropped off", "removed items") are ordinary sql questions.
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

Chat history:
{chat_history}

User query: {user_query}"""

_HISTORY_LIMIT = 6
_HISTORY_MAX_CHARS = 500
_ROLE_NAMES = {"human": "user", "ai": "assistant"}


def _message_role_content(message) -> tuple[str, str]:
    """Extract (role, content) from a LangChain message, dict, (role, content) pair or str."""
    if isinstance(message, dict):
        role = message.get("role") or message.get("type") or "unknown"
        content = message.get("content", "")
    elif isinstance(message, (tuple, list)) and len(message) == 2:
        role, content = message
    elif hasattr(message, "content"):
        role = getattr(message, "type", None) or getattr(message, "role", None) or "unknown"
        content = message.content
    else:
        role, content = "unknown", message

    if isinstance(content, list):  # multi-part content blocks
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        content = " ".join(p for p in parts if p)

    role = str(role).lower()
    return _ROLE_NAMES.get(role, role), " ".join(str(content or "").split())


def _format_chat_history(messages, user_query: str = "", limit: int = _HISTORY_LIMIT) -> str:
    """Render the last `limit` messages as readable "role: content" lines."""
    turns = [_message_role_content(m) for m in (messages or [])]
    turns = [(role, content) for role, content in turns if content]
    # The current question is passed separately; do not repeat it as history.
    if turns and turns[-1] == ("user", " ".join(user_query.split())):
        turns.pop()
    if not turns:
        return "(none)"
    lines = []
    for role, content in turns[-limit:]:
        if len(content) > _HISTORY_MAX_CHARS:
            content = content[:_HISTORY_MAX_CHARS] + "..."
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


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
    """Classifies user query, rewrites follow-ups, checks db connection."""
    user_query = state.get("user_query", "")

    # 1. DB connection check — schema presence confirms a database is connected.
    # db_manager is not stored in state (not msgpack-serializable); use schema instead.
    if state.get("schema") is None:
        logger.info("gatekeeper_node: no schema in state, returning early")
        return {
            "final_answer": "Please connect to a database first using the sidebar.",
            "query_type": "conversational",
        }

    # 2. No keyword filtering of the question: words like "dropped" or "deleted" are
    # common in legitimate questions. Read-only enforcement happens on the generated SQL.

    # 3. Lazy import get_llm (FallbackClient factory)
    from llm.fallback import get_llm
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = get_llm(node="gatekeeper", state=state)

    # 4. Build messages for classification
    chat_history = _format_chat_history(state.get("messages"), user_query)
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
            SystemMessage(content=(
                "Rewrite the following follow-up query as a complete standalone question, "
                "incorporating any necessary context from the chat history. "
                "Return only the rewritten query text, nothing else."
            )),
            HumanMessage(
                content=f"Chat history:\n{chat_history}\n\nFollow-up query: {user_query}"
            ),
        ]
        rewrite_response = await llm.ainvoke(rewrite_messages)
        result["resolved_query"] = rewrite_response.content.strip()

    # 7. Conversational / ambiguous → set final_answer
    if category in ("conversational", "ambiguous"):
        result["final_answer"] = response_text

    logger.info("gatekeeper_node: category=%s intent=%s", category, intent)
    return result
