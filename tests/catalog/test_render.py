"""Prompt rendering: format, PII marking, budget degradation order, legacy schema shape."""
from __future__ import annotations

from catalog.graph import RelationshipGraph
from catalog.models import CatalogRelationship
from catalog.render import render_context, to_legacy_schema
from tests.catalog.test_retrieval import BUSINESS, make_catalog

TABLES = ["orders", "customers", "order_items", "products", "categories", "addresses"]


def build():
    catalog = make_catalog(BUSINESS)
    catalog.tables["customers"].column("email").sample_values = ["leak@example.org"]
    catalog.tables["customers"].column("full_name").enum_values = ["Leaky Name"]
    catalog.relationships.append(
        CatalogRelationship("orders", "order_date", "addresses", "address_id", "inferred", 0.6))
    return catalog


def render(catalog, tables=TABLES, **kwargs):
    hints = RelationshipGraph(catalog).join_hints(tables)
    values = [{"table": "orders", "column": "status", "value": "Shipped"},
              {"table": "customers", "column": "segment", "value": "O'Brien"}]
    return render_context(catalog, tables, hints, values, **kwargs)


def test_full_render_format():
    text = render(build())
    assert "Table orders (90,000 rows): One row per order placed by a customer" in text
    assert "  order_id INTEGER [PK]\n" in text
    assert "  customer_id INTEGER [FK -> customers.customer_id] [nullable]" in text
    assert "  status TEXT [nullable] -- values: Pending, Shipped, Delivered, Cancelled" in text
    assert "  total_amount NUMERIC [nullable] -- order value including tax" in text
    assert "  city TEXT [nullable] -- e.g. Berlin, Austin" in text
    assert "order_date DATE [FK -> addresses.address_id (inferred)]" in text
    assert "Joins:\n" in text and "  orders.customer_id = customers.customer_id\n" in text
    assert "  orders.order_date = addresses.address_id (inferred)" in text
    assert "Value hints (exact stored values):\n  orders.status = 'Shipped'" in text
    assert "customers.segment = 'O''Brien'" in text
    assert text.index("Table orders") < text.index("Table customers") < text.index("Joins:")
    assert text.index("Joins:") < text.index("Value hints")


def test_pii_columns_are_marked_and_never_show_values():
    text = render(build())
    assert "  email TEXT [nullable] [sensitive]\n" in text
    assert "  full_name TEXT [nullable] [sensitive]\n" in text
    assert "leak@example.org" not in text and "Leaky Name" not in text


def test_long_enum_lists_and_values_are_clipped():
    catalog = build()
    col = catalog.tables["orders"].column("status")
    col.enum_values = [f"S{i}" for i in range(30)] + ["x" * 200, "a, b"]
    text = render(catalog)
    assert "S11, … +20 more" in text and "S12" not in text
    col.enum_values = ["x" * 200, "a, b"]
    text = render(catalog)
    assert "x" * 41 not in text and "'a, b'" in text


def test_unknown_tables_and_empty_sections_are_skipped():
    catalog = build()
    text = render_context(catalog, ["coupons", "ghost", "coupons"], [], [])
    assert text.count("Table coupons") == 1 and "ghost" not in text
    assert "Joins" not in text and "Value hints" not in text
    assert render_context(catalog, [], [], []) == ""


def test_budget_degrades_in_order():
    catalog = build()
    full = render(catalog)
    marks = {
        "samples": "e.g. Berlin",
        "column_descriptions": "order value including tax",
        "low_table_columns": "  postal_code TEXT",      # non-key column of the last table
        "top_table_columns": "  order_date DATE",       # non-key column of the first table
        "enum": "values: Pending, Shipped",
    }
    assert all(m in full for m in marks.values())

    def state(text):
        return {k for k, m in marks.items() if m in text}

    seen = [state(full)]
    for budget in range(len(full) - 1, 300, -40):
        text = render(catalog, max_chars=budget)
        assert len(text) <= budget
        assert "Joins:" in text and "Table orders" in text
        if state(text) != seen[-1]:
            seen.append(state(text))
    dropped = [sorted(a - b) for a, b in zip(seen, seen[1:])]
    order = [name for step in dropped for name in step]
    # samples go first, then column descriptions, then columns of the least relevant table
    assert order[:3] == ["samples", "column_descriptions", "low_table_columns"], order
    if "top_table_columns" in order:
        assert order.index("low_table_columns") < order.index("top_table_columns")
    assert all(not (b - a) for a, b in zip(seen, seen[1:]))        # nothing comes back


def test_collapsed_tables_keep_keys_and_say_what_is_missing():
    catalog = build()
    no_descriptions = len(render(catalog, max_chars=10 ** 6)) - 400
    text = render(catalog, max_chars=no_descriptions)
    while "more column" not in text:
        no_descriptions -= 50
        text = render(catalog, max_chars=no_descriptions)
    assert "Table addresses (15,000 rows)" in text
    block = text.split("Table addresses")[1].split("\n\n")[0]
    assert "address_id INTEGER [PK]" in block and "... 4 more columns" in block
    assert "street" not in block
    assert "  order_date DATE" in text                 # the top table is still complete


def test_tiny_budget_drops_tables_from_the_end_and_never_overflows():
    catalog = build()
    for budget in (700, 400, 150, 40, 0):
        text = render(catalog, max_chars=budget)
        assert len(text) <= budget
    text = render(catalog, max_chars=700)
    assert "more table" in text and text.startswith("Table orders")


def test_legacy_schema_shape():
    catalog = build()
    schema = to_legacy_schema(catalog, ["orders", "ghost", "customers", "orders"])
    assert list(schema) == ["orders", "customers"]
    orders = schema["orders"]
    assert set(orders) == {"columns", "primary_keys", "foreign_keys", "sample_rows"}
    assert orders["columns"][0] == {"name": "order_id", "type": "INTEGER", "nullable": False}
    assert orders["primary_keys"] == ["order_id"] and orders["sample_rows"] == []
    assert {"column": "customer_id", "references_table": "customers",
            "references_column": "customer_id"} in orders["foreign_keys"]
    # inferred joins are not declared foreign keys
    assert all(fk["column"] != "order_date" for fk in orders["foreign_keys"])
    assert "leak@example.org" not in str(schema)
