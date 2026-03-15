"""Unit tests for vector/schema_graph.py (VECTOR-003)."""
from __future__ import annotations

import pytest

from vector.schema_graph import SchemaGraph


def test_adjacency_built_from_fkinfo(sample_schema):
    graph = SchemaGraph(sample_schema)
    # Invoice has FK to Customer
    assert "Invoice" in graph._adj
    assert any(ref == "Customer" for _, ref, _ in graph._adj["Invoice"])
    # InvoiceLine has FK to Invoice
    assert "InvoiceLine" in graph._adj
    assert any(ref == "Invoice" for _, ref, _ in graph._adj["InvoiceLine"])


def test_expand_tables_1hop(sample_schema):
    graph = SchemaGraph(sample_schema)
    expanded = graph.expand_tables(["Invoice"])
    assert "Invoice" in expanded
    assert "Customer" in expanded  # 1-hop FK neighbor


def test_expand_tables_no_further(sample_schema):
    graph = SchemaGraph(sample_schema)
    expanded = graph.expand_tables(["Customer"])
    # Customer has no outgoing FKs — no expansion
    assert set(expanded) == {"Customer"}


def test_expand_tables_multiple_seeds(sample_schema):
    graph = SchemaGraph(sample_schema)
    expanded = graph.expand_tables(["Invoice", "InvoiceLine"])
    assert "Invoice" in expanded
    assert "InvoiceLine" in expanded
    assert "Customer" in expanded  # via Invoice FK


def test_join_hints_format(sample_schema):
    graph = SchemaGraph(sample_schema)
    hints = graph.generate_join_hints(["Invoice"])
    assert len(hints) >= 1
    hint = hints[0]
    assert hint["from"] == "Invoice"
    assert hint["to"] == "Customer"
    assert hint["on"] == "Invoice.CustomerId = Customer.CustomerId"
    assert hint["type"] == "INNER"


def test_join_hints_multiple_fks(sample_schema):
    graph = SchemaGraph(sample_schema)
    hints = graph.generate_join_hints(["InvoiceLine"])
    assert len(hints) >= 1
    assert hints[0]["from"] == "InvoiceLine"
    assert hints[0]["to"] == "Invoice"
    assert "InvoiceId" in hints[0]["on"]


def test_join_hints_empty_for_no_fk_table(sample_schema):
    graph = SchemaGraph(sample_schema)
    hints = graph.generate_join_hints(["Customer"])
    assert hints == []


def test_schema_graph_empty_schema():
    graph = SchemaGraph({})
    assert graph._adj == {}
    assert graph.expand_tables([]) == []
