"""Tests that verify the project directory and file structure (INFRA-002)."""
import os
import json
import sqlite3
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

REQUIRED_DIRS = [
    "agents", "database", "database/connectors",
    "graph", "llm", "vector", "evaluation",
    "memory", "utils", "streamlit_app",
    "tests", "tests/database", "config", "data",
]

REQUIRED_INIT_FILES = [
    "agents/__init__.py", "database/__init__.py",
    "database/connectors/__init__.py", "graph/__init__.py",
    "llm/__init__.py", "vector/__init__.py",
    "evaluation/__init__.py", "memory/__init__.py",
    "utils/__init__.py", "streamlit_app/__init__.py",
]


def test_required_directories():
    for d in REQUIRED_DIRS:
        path = os.path.join(REPO_ROOT, d)
        assert os.path.isdir(path), f"Required directory missing: {d}"


def test_required_init_files():
    for f in REQUIRED_INIT_FILES:
        path = os.path.join(REPO_ROOT, f)
        assert os.path.isfile(path), f"Required __init__.py missing: {f}"


def test_env_example():
    path = os.path.join(REPO_ROOT, ".env.example")
    assert os.path.isfile(path), ".env.example not found"
    content = open(path).read()
    required_keys = [
        "GROQ_API_KEY", "OPENAI_API_KEY", "PINECONE_API_KEY",
        "DB_TYPE", "DB_HOST", "DB_PORT", "DB_NAME",
        "DB_USER", "DB_PASSWORD", "DB_TIMEOUT", "MAX_RETRIES",
    ]
    for key in required_keys:
        assert key in content, f"Missing key in .env.example: {key}"


def test_config_yaml():
    import yaml
    path = os.path.join(REPO_ROOT, "config", "config.yaml")
    assert os.path.isfile(path), "config/config.yaml not found"
    with open(path) as f:
        data = yaml.safe_load(f)
    assert "database" in data
    assert "llm" in data
    assert "retry" in data


def test_error_taxonomy_json():
    path = os.path.join(REPO_ROOT, "config", "error-taxonomy.json")
    assert os.path.isfile(path), "config/error-taxonomy.json not found"
    with open(path) as f:
        data = json.load(f)
    assert "categories" in data


def test_chinook_db():
    path = os.path.join(REPO_ROOT, "data", "chinook.db")
    assert os.path.isfile(path), "data/chinook.db not found"
    assert os.path.getsize(path) > 1000, "chinook.db appears empty or corrupt"
    con = sqlite3.connect(path)
    tables = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    con.close()
    assert len(tables) >= 4, f"Expected at least 4 tables, got {len(tables)}"
