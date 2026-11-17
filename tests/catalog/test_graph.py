"""Relationship graph: bridging, lookups, join hints and naming-convention inference."""
from __future__ import annotations

from catalog.graph import RelationshipGraph, infer_relationships, singular
from catalog.models import Catalog, CatalogColumn, CatalogRelationship, CatalogTable


def table(name, *cols, pk=("id",)):
    """cols: "name" or "name:TYPE". The pk columns are added automatically when missing."""
    columns = []
    names = [c.split(":")[0] for c in cols]
    for key in pk:
        if key not in names:
            columns.append(CatalogColumn(key, "INTEGER", nullable=False, is_primary_key=True))
    for spec in cols:
        col, _, dtype = spec.partition(":")
        columns.append(CatalogColumn(col, dtype or "INTEGER", is_primary_key=col in pk))
    return CatalogTable(name=name, columns=columns, primary_keys=list(pk))


def rel(a, b, kind="foreign_key", conf=1.0):
    ft, fc = a.split(".")
    tt, tc = b.split(".")
    return CatalogRelationship(ft, fc, tt, tc, kind=kind, confidence=conf)


def catalog(tables, rels):
    return Catalog("t1", "c1", "sqlite", "h", tables={t.name: t for t in tables},
                   relationships=list(rels))


def chain():
    tables = [table("a", "b_id"), table("b", "c_id"), table("c", "d_id"), table("d", "name:TEXT")]
    rels = [rel("a.b_id", "b.id"), rel("b.c_id", "c.id"), rel("c.d_id", "d.id")]
    return catalog(tables, rels)


# ------------------------------------------------------------------ bridge
def test_chain_bridging_is_undirected():
    graph = RelationshipGraph(chain())
    assert graph.bridge(["a", "d"]) == ["b", "c"]
    assert graph.bridge(["d", "a"]) == ["c", "b"]
    assert graph.bridge(["a", "c"]) == ["b"]
    assert graph.bridge(["a", "b"]) == []
    assert graph.bridge(["a"]) == []
    assert graph.bridge([]) == []


def test_bridge_respects_max_hops():
    graph = RelationshipGraph(chain())
    assert graph.bridge(["a", "d"], max_hops=2) == []
    assert graph.bridge(["a", "d"], max_hops=3) == ["b", "c"]


def test_star_needs_only_the_hub():
    leaves = [table(f"leaf{i}", "hub_id") for i in range(5)]
    rels = [rel(f"leaf{i}.hub_id", "hub.id") for i in range(5)]
    graph = RelationshipGraph(catalog([table("hub", "name:TEXT"), *leaves], rels))
    assert graph.bridge(["leaf0", "leaf3", "leaf4"]) == ["hub"]
    assert graph.bridge(["leaf1", "hub"]) == []


def test_disconnected_components_stay_unconnected():
    tables = [table("a", "b_id"), table("b"), table("x", "y_id"), table("y", "z_id"), table("z")]
    rels = [rel("a.b_id", "b.id"), rel("x.y_id", "y.id"), rel("y.z_id", "z.id")]
    graph = RelationshipGraph(catalog(tables, rels))
    assert graph.bridge(["a", "z"]) == []
    assert graph.bridge(["a", "x", "z"]) == ["y"]
    assert graph.bridge(["a", "unknown_table", "b"]) == []


def test_declared_path_preferred_over_inferred_of_same_length():
    tables = [table("a", "m_id", "n_id"), table("m", "z_id"), table("n", "z_id"), table("z")]
    rels = [
        rel("a.m_id", "m.id", "inferred", 0.8), rel("m.z_id", "z.id", "inferred", 0.8),
        rel("a.n_id", "n.id"), rel("n.z_id", "z.id"),
    ]
    graph = RelationshipGraph(catalog(tables, rels))
    assert graph.bridge(["a", "z"]) == ["n"]
    # a shorter inferred path still beats a longer declared one
    rels.append(rel("a.id", "z.id", "inferred", 0.6))
    assert RelationshipGraph(catalog(tables, rels)).bridge(["a", "z"]) == []


def test_bridge_is_deterministic_on_ties():
    tables = [table("a", "p_id", "q_id"), table("p", "z_id"), table("q", "z_id"), table("z")]
    rels = [rel("a.q_id", "q.id"), rel("q.z_id", "z.id"), rel("a.p_id", "p.id"),
            rel("p.z_id", "z.id")]
    first = RelationshipGraph(catalog(tables, rels)).bridge(["a", "z"])
    second = RelationshipGraph(catalog(tables[::-1], rels[::-1])).bridge(["a", "z"])
    assert first == second == ["p"]


# ------------------------------------------------------------------ lookups and hints
def lookup_catalog():
    tables = [
        table("orders", "customer_id", "status_id", "audit_id", "parent_order_id"),
        table("customers", "full_name:TEXT"),
        table("statuses", "code:VARCHAR(10)"),
        table("audits", "created_at:TIMESTAMP", "amount:NUMERIC"),
        table("order_items", "order_id", "product_id"),
        table("products", "title:TEXT"),
    ]
    rels = [
        rel("orders.customer_id", "customers.id"), rel("orders.status_id", "statuses.id"),
        rel("orders.audit_id", "audits.id"), rel("orders.parent_order_id", "orders.id"),
        rel("order_items.order_id", "orders.id"),
        rel("order_items.product_id", "products.id", "inferred", 0.9),
    ]
    return catalog(tables, rels)


def test_lookups_only_label_tables_not_selected_not_self():
    graph = RelationshipGraph(lookup_catalog())
    assert graph.lookups(["orders"]) == ["customers", "statuses"]
    assert graph.lookups(["orders", "customers"]) == ["statuses"]
    assert graph.lookups(["order_items"]) == ["products"]      # orders has no label column
    assert graph.lookups(["customers"]) == []                  # only outgoing references count


def test_join_hints_shape_dedupe_and_declared_preference():
    cat = lookup_catalog()
    cat.relationships += [
        rel("orders.customer_id", "customers.id", "inferred", 0.9),   # duplicate, inferred
        rel("customers.id", "orders.customer_id"),                    # same join, reversed
        rel("order_items.product_id", "products.id"),                 # declared twin
    ]
    graph = RelationshipGraph(cat)
    hints = graph.join_hints(["orders", "customers", "order_items", "products"])
    assert {h["on"] for h in hints} == {
        "orders.customer_id = customers.id", "order_items.order_id = orders.id",
        "order_items.product_id = products.id", "orders.parent_order_id = orders.id",
    }
    assert len(hints) == 4
    assert all(set(h) == {"from", "to", "on", "kind"} for h in hints)
    assert all(h["kind"] == "foreign_key" for h in hints)
    assert graph.join_hints(["customers", "products"]) == []
    assert hints == RelationshipGraph(cat).join_hints(
        ["products", "order_items", "customers", "orders"])


def test_relationships_to_unknown_tables_are_ignored():
    cat = catalog([table("a", "b_id")], [rel("a.b_id", "ghost.id")])
    graph = RelationshipGraph(cat)
    assert graph.join_hints(["a", "ghost"]) == []
    assert graph.lookups(["a"]) == []


# ------------------------------------------------------------------ inference
def infer(tables, existing=()):
    found = infer_relationships({t.name: t for t in tables}, list(existing))
    assert all(r.kind == "inferred" and 0.5 <= r.confidence <= 0.9 for r in found)
    return {(r.from_table, r.from_column, r.to_table, r.to_column) for r in found}


def test_singular():
    assert [singular(w) for w in ("customers", "categories", "addresses", "status", "boxes",
                                  "people", "class")] == [
        "customer", "category", "address", "status", "box", "person", "class"]


def test_infers_conventional_references():
    tables = [
        table("Customers", "name:TEXT"),
        table("orders", "customer_id", "CategoryId", "statusid", "warehouse_key",
              "billing_address_id"),
        table("category", pk=("category_id",)),
        table("Status", "code:TEXT", pk=("StatusId",)),
        table("dim_warehouse", pk=("warehouse_key",)),
        table("addresses", "city:TEXT"),
        table("people", "name:TEXT"),
        table("visits", "person_id"),
    ]
    assert infer(tables) == {
        ("orders", "customer_id", "Customers", "id"),
        ("orders", "CategoryId", "category", "category_id"),
        ("orders", "statusid", "Status", "StatusId"),
        ("orders", "warehouse_key", "dim_warehouse", "warehouse_key"),
        ("orders", "billing_address_id", "addresses", "id"),
        ("visits", "person_id", "people", "id"),
    }


def test_confidence_reflects_how_direct_the_match_is():
    tables = [table("customers"), table("addresses"),
              table("orders", "customer_id", "billing_address_id")]
    found = {r.from_column: r.confidence
             for r in infer_relationships({t.name: t for t in tables}, [])}
    assert found["customer_id"] > found["billing_address_id"] >= 0.5


def test_same_named_primary_key_rule():
    tables = [
        table("tracks", "name:TEXT", pk=("isbn",)),
        table("plays", "isbn", "played_at:TIMESTAMP"),
        table("line_items", pk=("order_no", "isbn")),      # member of a composite key: fine
    ]
    assert infer(tables) == {("plays", "isbn", "tracks", "isbn"),
                             ("line_items", "isbn", "tracks", "isbn")}


def test_self_reference_through_role_prefix_but_never_the_key_itself():
    tables = [table("categories", "parent_category_id", "name:TEXT"),
              table("employees", "manager_id", "employee_id_copy", pk=("employee_id",))]
    assert infer(tables) == {("categories", "parent_category_id", "categories", "id")}


def test_precision_cases_do_not_match():
    tables = [
        # ambiguous: two tables answer to "customer"
        table("customer"), table("customers"), table("orders", "customer_id"),
        # no table for the entity
        table("events", "session_id", "paid", "valid", "android"),
        # incompatible type families
        table("products"), table("reviews", "product_id:TEXT"),
        # target key has an unrelated name
        table("regions", pk=("geo_code",)), table("stores", "region_id"),
        # target has a composite key
        table("enrolments", pk=("student_no", "course_no")), table("grades", "enrolment_id"),
        # generic same-named keys, and a key shared by two tables
        table("alpha", pk=("code",)), table("beta", "code:INTEGER"),
        table("left_t", pk=("shared_no",)), table("right_t", pk=("shared_no",)),
        table("uses", "shared_no"),
    ]
    assert infer(tables) == set()


def test_never_duplicates_existing_relationships():
    tables = [table("customers"), table("orders", "customer_id", "buyer_id"), table("buyers")]
    existing = [rel("orders.customer_id", "customers.id")]
    assert infer(tables, existing) == {("orders", "buyer_id", "buyers", "id")}
    # the reversed declaration is the same join
    assert infer(tables, [rel("customers.id", "orders.customer_id"),
                          rel("buyers.id", "orders.buyer_id")]) == set()
    # a column that already references something is left alone
    assert infer(tables, [rel("orders.customer_id", "buyers.id"),
                          rel("orders.buyer_id", "buyers.id")]) == set()


def test_tables_without_declared_keys_fall_back_to_named_columns():
    orders = CatalogTable("orders", columns=[CatalogColumn("order_id", "INTEGER"),
                                             CatalogColumn("customer_id", "INTEGER")])
    customers = CatalogTable("customers", columns=[CatalogColumn("customer_id", "INTEGER"),
                                                   CatalogColumn("name", "TEXT")])
    found = infer_relationships({"orders": orders, "customers": customers}, [])
    assert [(r.join_condition, r.confidence) for r in found] == [
        ("orders.customer_id = customers.customer_id", 0.55)]


def test_inference_is_deterministic():
    tables = [table("customers"), table("orders", "customer_id"), table("items", "order_id")]
    a = infer_relationships({t.name: t for t in tables}, [])
    b = infer_relationships({t.name: t for t in reversed(tables)}, [])
    assert [r.join_condition for r in a] == [r.join_condition for r in b]
    assert len(a) == 2
