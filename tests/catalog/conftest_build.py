"""Shared helpers for the catalog build-side tests (imported explicitly, not a conftest)."""
from __future__ import annotations

import json
import sqlite3

import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from catalog.models import (
    Catalog,
    CatalogColumn,
    CatalogRelationship,
    CatalogTable,
)
from database.credentials import CredentialCipher, generate_key
from database.registry import ConnectionRegistry

SECRET_EMAIL = "margaret.park@example.org"


def memory_engine():
    return sa.create_engine("sqlite://", connect_args={"check_same_thread": False},
                            poolclass=StaticPool)


def make_registry(engine):
    return ConnectionRegistry(engine, cipher=CredentialCipher(generate_key()),
                              host_validator=lambda host, port: None)


def make_shop_db(path) -> str:
    """Small SQLite database with foreign keys, an enum-like column and PII."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            full_name TEXT NOT NULL,
            contact TEXT,
            country TEXT,
            segment TEXT
        );
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT,
            price REAL
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            status TEXT NOT NULL,
            quantity INTEGER
        );
    """)
    conn.executemany(
        "INSERT INTO customers VALUES (?,?,?,?,?)",
        [(i, f"Person {i}", SECRET_EMAIL if i == 1 else f"user{i}@example.org",
          ["US", "DE", "IN"][i % 3], ["retail", "wholesale"][i % 2]) for i in range(1, 41)],
    )
    conn.executemany(
        "INSERT INTO products VALUES (?,?,?,?)",
        [(i, f"Product {i}", ["tools", "toys", "books"][i % 3], 1.5 * i) for i in range(1, 61)],
    )
    conn.executemany(
        "INSERT INTO orders VALUES (?,?,?,?,?)",
        [(i, 1 + i % 40, 1 + i % 60, ["new", "paid", "shipped", "returned"][i % 4],
          None if i % 5 == 0 else i % 7) for i in range(1, 201)],
    )
    conn.commit()
    conn.close()
    return str(path)


def sample_catalog(tenant_id="tenant-a", connection_id="conn-1") -> Catalog:
    customers = CatalogTable(
        name="customers", primary_keys=["id"], row_count=40, content_hash="h1",
        description="People who buy things.", description_source="llm",
        columns=[
            CatalogColumn(name="id", data_type="INTEGER", nullable=False, is_primary_key=True,
                          is_unique=True, distinct_count=40, null_fraction=0.0,
                          cardinality="high"),
            CatalogColumn(name="segment", data_type="TEXT", sample_values=["retail"],
                          enum_values=["retail", "wholesale"], distinct_count=2,
                          null_fraction=0.25, cardinality="low", description="Kind of buyer.",
                          description_source="llm"),
            CatalogColumn(name="email", data_type="TEXT", is_pii=True,
                          pii_reason="column name suggests email address"),
        ],
    )
    orders = CatalogTable(
        name="orders", primary_keys=["id"], row_count=200, content_hash="h2",
        columns=[
            CatalogColumn(name="id", data_type="INTEGER", is_primary_key=True),
            CatalogColumn(name="customer_id", data_type="INTEGER", is_foreign_key=True),
        ],
    )
    return Catalog(
        tenant_id=tenant_id, connection_id=connection_id, db_type="sqlite", schema_hash="s1",
        tables={"customers": customers, "orders": orders},
        relationships=[
            CatalogRelationship("orders", "customer_id", "customers", "id"),
            CatalogRelationship("orders", "id", "customers", "id", kind="inferred",
                                confidence=0.4),
        ],
        embedder_name="fake-embedder",
        table_vectors={"customers": [0.25, -0.5, 1.0], "orders": [0.0, 1.0, 0.0]},
        column_vectors={("customers", "segment"): [1.0, 0.0, 0.5]},
    )


class FakeEmbedder:
    name = "fake-embedder"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_documents(self, texts):
        self.calls.append(list(texts))
        return [[float(len(t)), float(sum(map(ord, t)) % 97), 1.0] for t in texts]

    def embed_query(self, text):
        return self.embed_documents([text])[0]

    @property
    def texts(self) -> list[str]:
        return [t for call in self.calls for t in call]


class FakeResponse:
    def __init__(self, content) -> None:
        self.content = content


class FakeLLM:
    """Async LLM double. ``answer`` is a string, an exception, or a callable(prompt)."""

    def __init__(self, answer=None) -> None:
        self.answer = answer
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def ainvoke(self, messages, state=None):
        self.systems.append(messages[0].content)
        prompt = messages[-1].content
        self.prompts.append(prompt)
        answer = self.answer
        if callable(answer):
            answer = answer(prompt)
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            answer = good_answer(prompt)
        return FakeResponse(answer)


def table_of(prompt: str) -> str:
    return next(line.split(": ", 1)[1] for line in prompt.splitlines()
                if line.startswith("Table: "))


def columns_of(prompt: str) -> list[str]:
    return [line[2:].split(" (", 1)[0] for line in prompt.splitlines()
            if line.startswith("- ")]


def good_answer(prompt: str) -> str:
    table = table_of(prompt)
    return json.dumps({
        "table_description": f"Rows of {table}.",
        "columns": {name: f"The {name} of {table}." for name in columns_of(prompt)},
    })


class CountingManager:
    """Wraps a DatabaseManager and records the SQL it is asked to run."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.queries: list[str] = []

    def execute_query(self, sql, timeout=None, max_rows=None):
        self.queries.append(sql)
        return self._inner.execute_query(sql, timeout=timeout, max_rows=max_rows)

    def __getattr__(self, name):
        return getattr(self._inner, name)
