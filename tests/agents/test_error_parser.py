"""TDD RED tests for utils/error_parser.py — ERROR-001 taxonomy and classification."""
import pytest


# ---------------------------------------------------------------------------
# test_load_taxonomy_returns_20_categories
# ---------------------------------------------------------------------------

def test_load_taxonomy_returns_20_categories():
    """_load_taxonomy() returns dict with 'categories' key containing 20 dicts."""
    from utils.error_parser import _load_taxonomy

    taxonomy = _load_taxonomy()
    assert isinstance(taxonomy, dict), "Expected a dict"
    assert "categories" in taxonomy, "Missing 'categories' key"
    cats = taxonomy["categories"]
    assert isinstance(cats, list), "categories must be a list"
    assert len(cats) == 20, f"Expected 20 categories, got {len(cats)}"


def test_each_category_has_required_keys():
    """Each category must have id, name, severity, strategy, prompt_hint, patterns."""
    from utils.error_parser import _load_taxonomy

    taxonomy = _load_taxonomy()
    required_keys = {"id", "name", "severity", "prompt_hint", "strategy", "patterns"}
    dialect_keys = {"postgres", "mysql", "sqlite", "duckdb"}

    for cat in taxonomy["categories"]:
        missing = required_keys - set(cat.keys())
        assert not missing, f"Category {cat.get('id', '?')} missing keys: {missing}"
        patterns = cat["patterns"]
        missing_dialects = dialect_keys - set(patterns.keys())
        assert not missing_dialects, (
            f"Category {cat['id']} missing dialect patterns: {missing_dialects}"
        )


def test_classify_postgres_syntax_error():
    """classify_error with postgres syntax error returns ('syntax_error' category, 'high')."""
    from utils.error_parser import _load_taxonomy, classify_error

    taxonomy = _load_taxonomy()
    error_log = {
        "message": "syntax error at or near 'SELCT'",
        "dialect": "postgres",
    }
    category, confidence = classify_error(error_log, taxonomy)
    assert category["id"] == "syntax_error", f"Expected 'syntax_error', got {category['id']}"
    assert confidence == "high"


def test_classify_sqlite_missing_table():
    """classify_error with sqlite 'no such table' returns ('missing_table' category, 'high')."""
    from utils.error_parser import _load_taxonomy, classify_error

    taxonomy = _load_taxonomy()
    error_log = {
        "message": "no such table: invoices",
        "dialect": "sqlite",
    }
    category, confidence = classify_error(error_log, taxonomy)
    assert category["id"] == "missing_table", f"Expected 'missing_table', got {category['id']}"
    assert confidence == "high"


def test_classify_unknown_returns_low_confidence():
    """classify_error with unknown error returns category id='unknown' and confidence='low'."""
    from utils.error_parser import _load_taxonomy, classify_error

    taxonomy = _load_taxonomy()
    error_log = {
        "message": "some completely unrecognizable error that matches nothing",
        "dialect": "sqlite",
    }
    category, confidence = classify_error(error_log, taxonomy)
    assert category["id"] == "unknown", f"Expected 'unknown', got {category['id']}"
    assert confidence == "low"


def test_get_fuzzy_matches_returns_close_names():
    """get_fuzzy_matches('Inovice', [...]) returns ['Invoice', 'InvoiceLine']."""
    from utils.error_parser import get_fuzzy_matches

    candidates = ["Invoice", "InvoiceLine", "Artist"]
    matches = get_fuzzy_matches("Inovice", candidates)
    assert "Invoice" in matches, f"Expected 'Invoice' in matches, got {matches}"
