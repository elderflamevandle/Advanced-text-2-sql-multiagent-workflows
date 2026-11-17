"""Typed errors raised by the LLM layer."""

LLM_FAILURE_ERROR_TYPE = "llm_all_providers_failed"
LLM_FAILURE_HINT = "Check GROQ_API_KEY, OPENAI_API_KEY, and Ollama server status."


class LLMUnavailableError(RuntimeError):
    """Raised when no LLM provider could serve a request.

    Covers both "every configured provider failed" and "no provider is configured".
    """

    def __init__(self, message: str, last_error: Exception | None = None,
                 providers_tried: list[str] | None = None):
        super().__init__(message)
        self.last_error = last_error
        self.providers_tried = list(providers_tried or [])

    def to_error_log(self, dialect: str | None = None) -> dict:
        """Return the structured error_log dict used across the graph."""
        return {
            "error_type": LLM_FAILURE_ERROR_TYPE,
            "message": str(self),
            "dialect": dialect,
            "failed_sql": None,
            "hint": LLM_FAILURE_HINT,
        }
