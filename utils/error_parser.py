"""Error taxonomy loading and SQL error classification utilities."""
import json
import re
from pathlib import Path
from typing import Optional


def _load_taxonomy() -> dict:
    """Load the error taxonomy from config/error-taxonomy.json.

    Not cached — reads file fresh each call to allow test patching.
    """
    config_path = Path(__file__).parent.parent / "config" / "error-taxonomy.json"
    with open(config_path, encoding="utf-8") as fh:
        return json.load(fh)


def _unknown_category() -> dict:
    """Return the fallback unknown-error category dict."""
    return {
        "id": "unknown",
        "name": "Unknown Error",
        "severity": "recoverable",
        "strategy": "general_fix",
        "prompt_hint": (
            "The error type is unknown. Analyze the error message carefully "
            "and attempt a general fix."
        ),
    }


def _classify_by_regex(
    error_message: str, dialect: str, taxonomy: dict
) -> Optional[dict]:
    """Iterate taxonomy categories and return the first category whose dialect patterns match.

    Returns the matched category dict, or None if no match found.
    """
    for category in taxonomy.get("categories", []):
        patterns = category.get("patterns", {})
        dialect_patterns = patterns.get(dialect, [])
        for pattern in dialect_patterns:
            if re.search(pattern, error_message, re.IGNORECASE):
                return category
    return None


def classify_error(error_log: dict, taxonomy: dict) -> tuple:
    """Classify a SQL error using regex patterns from the taxonomy.

    Args:
        error_log: Dict with at least 'message' and 'dialect' keys.
        taxonomy: Taxonomy dict as returned by _load_taxonomy().

    Returns:
        (category_dict, confidence) where confidence is 'high' on regex match
        or 'low' when falling back to the unknown category.
    """
    message = error_log.get("message", "")
    dialect = error_log.get("dialect", "sqlite")

    matched = _classify_by_regex(message, dialect, taxonomy)
    if matched is not None:
        return matched, "high"

    return _unknown_category(), "low"


def get_fuzzy_matches(
    name: str, candidates: list, n: int = 3, cutoff: float = 0.6
) -> list:
    """Return fuzzy matches for *name* from *candidates* using difflib.

    Args:
        name: The possibly-misspelled name to look up.
        candidates: List of valid names to search.
        n: Maximum number of matches to return.
        cutoff: Minimum similarity ratio (0.0–1.0).

    Returns:
        List of close matches sorted by similarity (best first).
    """
    from difflib import get_close_matches

    return get_close_matches(name, candidates, n=n, cutoff=cutoff)
