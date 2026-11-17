"""Table selection: recall on realistic catalogs, value hints, PII, widening, fusion, speed."""
from __future__ import annotations

import hashlib
import time

import pytest

from catalog.models import Catalog, CatalogColumn, CatalogRelationship, CatalogTable
from catalog.retrieval import get_index, select_context, stem, tokenize

SECRET_EMAIL = "zelda.fitzgerald@example.org"
SECRET_SSN = "078-05-1120"


def make_catalog(spec: dict, *, declared: bool = True) -> Catalog:
    """spec: table -> (description, rows, [column specs]).

    Column spec: "name TYPE [pk] [fk=table.column] [pii] [enum=a|b|c] [eg=a|b] [-- description]".
    """
    tables: dict[str, CatalogTable] = {}
    rels: list[CatalogRelationship] = []
    for name, (description, rows, columns) in spec.items():
        cols, pks = [], []
        for line in columns:
            head, _, desc = line.partition(" -- ")
            parts = head.split()
            col = CatalogColumn(parts[0], parts[1], description=desc or None)
            for flag in parts[2:]:
                key, _, arg = flag.partition("=")
                arg = arg.replace("_", " ")
                if key == "pk":
                    col.is_primary_key, col.nullable = True, False
                    pks.append(col.name)
                elif key == "fk":
                    col.is_foreign_key = True
                    target, target_col = flag.partition("=")[2].split(".")
                    rels.append(CatalogRelationship(name, col.name, target, target_col))
                elif key == "pii":
                    col.is_pii, col.pii_reason = True, "name"
                elif key == "enum":
                    col.enum_values, col.cardinality = arg.split("|"), "low"
                elif key == "eg":
                    col.sample_values = arg.split("|")
            cols.append(col)
        tables[name] = CatalogTable(name, columns=cols, primary_keys=pks, row_count=rows,
                                    description=description or None)
    if not declared:
        for rel in rels:
            rel.kind, rel.confidence = "inferred", 0.85
    return Catalog("tenant-a", "conn-1", "postgresql", "hash-1", tables=tables,
                   relationships=rels, built_at="2026-01-01T00:00:00+00:00")


BUSINESS = {
    # ---------------------------------------------------------------- e-commerce
    "customers": ("People who buy from the shop", 12000, [
        "customer_id INTEGER pk", "full_name TEXT pii", "email TEXT pii",
        "segment TEXT enum=Retail|Wholesale|VIP -- marketing segment",
        "signup_date DATE", "address_id INTEGER fk=addresses.address_id"]),
    "addresses": ("Postal addresses", 15000, [
        "address_id INTEGER pk", "street TEXT pii", "city TEXT eg=Berlin|Austin",
        "country TEXT enum=United_States|Germany|India|Brazil", "postal_code TEXT"]),
    "orders": ("One row per order placed by a customer", 90000, [
        "order_id INTEGER pk", "customer_id INTEGER fk=customers.customer_id",
        "order_date DATE", "status TEXT enum=Pending|Shipped|Delivered|Cancelled",
        "total_amount NUMERIC -- order value including tax",
        "employee_id INTEGER fk=employees.employee_id -- sales rep"]),
    "order_items": ("Line items: the products inside each order", 250000, [
        "order_item_id INTEGER pk", "order_id INTEGER fk=orders.order_id",
        "product_id INTEGER fk=products.product_id", "quantity INTEGER",
        "unit_price NUMERIC -- price charged per unit; revenue is quantity * unit_price"]),
    "products": ("Catalogue of things for sale", 4000, [
        "product_id INTEGER pk", "name TEXT eg=Trail_Shoe|Rain_Jacket", "sku TEXT",
        "category_id INTEGER fk=categories.category_id",
        "supplier_id INTEGER fk=suppliers.supplier_id", "list_price NUMERIC",
        "discontinued BOOLEAN"]),
    "categories": ("Product categories", 40, [
        "category_id INTEGER pk", "name TEXT enum=Footwear|Outerwear|Electronics|Toys",
        "parent_category_id INTEGER fk=categories.category_id"]),
    "suppliers": ("Companies that supply products", 300, [
        "supplier_id INTEGER pk", "company_name TEXT", "country TEXT", "contact_email TEXT pii"]),
    "warehouses": ("Storage sites", 12, [
        "warehouse_id INTEGER pk", "name TEXT", "city TEXT", "capacity INTEGER"]),
    "inventory": ("Stock on hand per product and warehouse", 30000, [
        "inventory_id INTEGER pk", "product_id INTEGER fk=products.product_id",
        "warehouse_id INTEGER fk=warehouses.warehouse_id",
        "quantity_on_hand INTEGER -- current stock level", "reorder_level INTEGER"]),
    "carriers": ("Delivery companies", 6, [
        "carrier_id INTEGER pk", "name TEXT enum=FedEx|UPS|DHL|USPS", "tracking_url TEXT"]),
    "shipments": ("Parcels sent for an order", 80000, [
        "shipment_id INTEGER pk", "order_id INTEGER fk=orders.order_id",
        "carrier_id INTEGER fk=carriers.carrier_id", "shipped_date DATE",
        "delivered_date DATE", "freight_cost NUMERIC"]),
    "payment_methods": ("Ways to pay", 5, [
        "payment_method_id INTEGER pk",
        "name TEXT enum=Credit_Card|PayPal|Bank_Transfer|Gift_Card"]),
    "payments": ("Money received for orders", 95000, [
        "payment_id INTEGER pk", "order_id INTEGER fk=orders.order_id",
        "payment_method_id INTEGER fk=payment_methods.payment_method_id",
        "amount NUMERIC", "paid_at TIMESTAMP"]),
    "returns": ("Items sent back by customers", 7000, [
        "return_id INTEGER pk", "order_item_id INTEGER fk=order_items.order_item_id",
        "customer_id INTEGER fk=customers.customer_id",
        "reason TEXT enum=Damaged|Wrong_Size|Changed_Mind", "refund_amount NUMERIC"]),
    "reviews": ("Product reviews written by customers", 60000, [
        "review_id INTEGER pk", "product_id INTEGER fk=products.product_id",
        "customer_id INTEGER fk=customers.customer_id", "rating INTEGER -- 1 to 5 stars",
        "body TEXT"]),
    "coupons": ("Discount codes", 200, [
        "coupon_id INTEGER pk", "code TEXT", "discount_percent NUMERIC", "expires_on DATE"]),
    "order_coupons": ("Which coupon was applied to which order", 9000, [
        "order_id INTEGER fk=orders.order_id", "coupon_id INTEGER fk=coupons.coupon_id"]),
    "departments": ("Company departments", 9, [
        "department_id INTEGER pk", "name TEXT enum=Sales|Support|Logistics|Finance"]),
    "employees": ("Staff of the company", 450, [
        "employee_id INTEGER pk", "full_name TEXT pii", "ssn TEXT pii",
        "department_id INTEGER fk=departments.department_id",
        "manager_id INTEGER fk=employees.employee_id", "hire_date DATE", "salary NUMERIC"]),
    # ---------------------------------------------------------------- healthcare
    "patients": ("People receiving care", 30000, [
        "patient_id INTEGER pk", "full_name TEXT pii", "date_of_birth DATE pii",
        "gender TEXT enum=F|M|X", "insurance_plan_id INTEGER fk=insurance_plans.plan_id"]),
    "specialties": ("Medical specialties", 30, [
        "specialty_id INTEGER pk", "name TEXT enum=Cardiology|Oncology|Pediatrics|Neurology"]),
    "physicians": ("Doctors employed by the clinics", 800, [
        "physician_id INTEGER pk", "full_name TEXT pii",
        "specialty_id INTEGER fk=specialties.specialty_id",
        "clinic_id INTEGER fk=clinics.clinic_id"]),
    "clinics": ("Care sites", 25, ["clinic_id INTEGER pk", "name TEXT", "city TEXT"]),
    "appointments": ("Scheduled visits of a patient with a physician", 400000, [
        "appointment_id INTEGER pk", "patient_id INTEGER fk=patients.patient_id",
        "physician_id INTEGER fk=physicians.physician_id", "scheduled_at TIMESTAMP",
        "status TEXT enum=Booked|Completed|No-Show|Canceled"]),
    "icd_codes": ("Diagnosis code reference", 70000, [
        "icd_code TEXT pk", "description TEXT eg=Type_2_diabetes_mellitus|Asthma"]),
    "diagnoses": ("Conditions diagnosed at an appointment", 600000, [
        "diagnosis_id INTEGER pk", "appointment_id INTEGER fk=appointments.appointment_id",
        "patient_id INTEGER fk=patients.patient_id", "icd_code TEXT fk=icd_codes.icd_code",
        "diagnosed_on DATE"]),
    "medications": ("Drug reference list", 5000, [
        "medication_id INTEGER pk", "generic_name TEXT", "form TEXT enum=Tablet|Injection|Syrup"]),
    "prescriptions": ("Medications prescribed to patients", 900000, [
        "prescription_id INTEGER pk", "patient_id INTEGER fk=patients.patient_id",
        "physician_id INTEGER fk=physicians.physician_id",
        "medication_id INTEGER fk=medications.medication_id", "dosage TEXT",
        "prescribed_on DATE"]),
    "lab_tests": ("Kinds of laboratory test", 400, [
        "lab_test_id INTEGER pk", "name TEXT eg=HbA1c|Lipid_Panel", "unit TEXT"]),
    "lab_results": ("Measured laboratory values per patient", 2000000, [
        "lab_result_id INTEGER pk", "patient_id INTEGER fk=patients.patient_id",
        "lab_test_id INTEGER fk=lab_tests.lab_test_id", "result_value NUMERIC",
        "collected_at TIMESTAMP"]),
    "insurance_plans": ("Health insurance plans", 60, [
        "plan_id INTEGER pk", "plan_name TEXT", "payer TEXT enum=Aetna|Cigna|Medicare"]),
    "claims": ("Insurance claims filed for care", 350000, [
        "claim_id INTEGER pk", "patient_id INTEGER fk=patients.patient_id",
        "plan_id INTEGER fk=insurance_plans.plan_id", "claim_amount NUMERIC",
        "status TEXT enum=Submitted|Approved|Denied"]),
    "wards": ("Hospital wards", 20, [
        "ward_id INTEGER pk", "name TEXT enum=ICU|Maternity|Surgery", "bed_count INTEGER"]),
    "admissions": ("Hospital stays", 45000, [
        "admission_id INTEGER pk", "patient_id INTEGER fk=patients.patient_id",
        "ward_id INTEGER fk=wards.ward_id", "admitted_at TIMESTAMP", "discharged_at TIMESTAMP"]),
}

# question -> tables that must be selected
QUESTIONS = [
    ("What is the total revenue per product category?", {"order_items", "products", "categories"}),
    ("How many orders were shipped by FedEx last month?", {"orders", "shipments", "carriers"}),
    ("List customers who returned more than 2 items", {"customers", "returns"}),
    ("Which physicians have the most appointments?", {"physicians", "appointments"}),
    ("Average lab result value for patients diagnosed with diabetes",
     {"lab_results", "diagnoses", "patients"}),
    ("Which medications are prescribed most often?", {"prescriptions", "medications"}),
    ("Show inventory levels per warehouse", {"inventory", "warehouses"}),
    ("Total claim amount by insurance plan", {"claims", "insurance_plans"}),
    ("Which suppliers provide products that are low on stock?",
     {"suppliers", "products", "inventory"}),
    ("average review rating for each product", {"reviews", "products"}),
    ("How many employees work in each department?", {"employees", "departments"}),
    ("How many cancelled orders used a coupon?", {"orders", "order_coupons"}),
    ("Sum of payments made by Credit Card", {"payments", "payment_methods"}),
    ("Which patients were admitted to the ICU ward?", {"patients", "admissions", "wards"}),
    ("number of doctors by specialty", {"physicians", "specialties"}),
    ("freight cost per carrier", {"shipments", "carriers"}),
]


@pytest.fixture(scope="module")
def business() -> Catalog:
    catalog = make_catalog(BUSINESS)
    catalog.tables["customers"].column("email").sample_values = [SECRET_EMAIL]   # must not leak
    catalog.tables["employees"].column("ssn").enum_values = [SECRET_SSN]
    return catalog


def recall(catalog, questions, **kwargs) -> tuple[float, list]:
    hit = total = 0
    misses = []
    for question, expected in questions:
        got = set(select_context(catalog, question, **kwargs).tables)
        hit += len(expected & got)
        total += len(expected)
        if expected - got:
            misses.append((question, sorted(expected - got)))
    return hit / total, misses


# ------------------------------------------------------------------ tokenising
def test_tokenize_splits_identifiers_and_stems():
    assert tokenize("OrderItems") == tokenize("order_items") == ["order", "item"]
    assert tokenize("the categories of shipped orders") == ["category", "ship", "order"]
    assert stem("bookings") != stem("books")
    assert tokenize("unitPrice HTTPStatus") == ["unit", stem("price"), "http", "status"]
    assert tokenize(None) == []


# ------------------------------------------------------------------ recall
def test_business_catalog_recall(business):
    assert len(business.tables) >= 30
    score, misses = recall(business, QUESTIONS)
    assert score == 1.0, misses


def test_selection_is_narrow_and_explained(business):
    ctx = select_context(business, "Which physicians have the most appointments?")
    assert len(ctx.tables) <= 12 < len(business.tables)
    assert ctx.tables[0] in {"physicians", "appointments"}
    assert ctx.reasons["physicians"] == ctx.reasons["appointments"] == "matched"
    assert set(ctx.reasons) == set(ctx.tables) == set(ctx.scores) == set(ctx.schema)
    assert set(ctx.reasons.values()) <= {"matched", "bridge", "lookup", "required"}
    assert not {"orders", "products", "coupons"} & set(ctx.tables)
    ranked = [(-ctx.scores[t], t) for t in ctx.tables]
    assert ranked == sorted(ranked)
    assert {"from": "appointments", "to": "physicians", "kind": "foreign_key",
            "on": "appointments.physician_id = physicians.physician_id"} in ctx.join_hints
    assert "Table appointments" in ctx.prompt_text and "Joins:" in ctx.prompt_text
    assert ctx.schema["appointments"]["sample_rows"] == []


def test_bridge_and_lookup_tables_are_added(business):
    ctx = select_context(business, "coupons by carrier")
    assert {"coupons", "carriers"} <= set(ctx.tables)
    # coupons <- order_coupons -> orders <- shipments -> carriers
    assert ctx.reasons.get("orders") == "bridge"
    assert {"order_coupons", "shipments"} <= set(ctx.tables)
    ctx = select_context(business, "How many employees were hired in 2024?")
    assert ctx.reasons.get("departments") == "lookup"


def test_inferred_relationships_bridge_too():
    catalog = make_catalog(BUSINESS, declared=False)
    ctx = select_context(catalog, "freight cost per order status")
    assert {"shipments", "orders"} <= set(ctx.tables)
    assert any(h["kind"] == "inferred" for h in ctx.join_hints)
    assert "(inferred)" in ctx.prompt_text


# ------------------------------------------------------------------ value hints and PII
def test_value_hints_keep_the_stored_case(business):
    ctx = select_context(business, "how many orders were CANCELLED or shipped via fedex?")
    hints = {(h["table"], h["column"], h["value"]) for h in ctx.value_hints}
    assert ("orders", "status", "Cancelled") in hints
    assert ("orders", "status", "Shipped") in hints
    assert ("carriers", "name", "FedEx") in hints
    assert "carriers.name = 'FedEx'" in ctx.prompt_text


def test_quoted_and_multi_word_values(business):
    ctx = select_context(business, "customers from the United States paying by 'gift card'")
    hints = {(h["table"], h["column"], h["value"]) for h in ctx.value_hints}
    assert ("addresses", "country", "United States") in hints
    assert ("payment_methods", "name", "Gift Card") in hints
    assert {"addresses", "payment_methods"} <= set(ctx.tables)


def test_value_match_alone_selects_the_table(business):
    ctx = select_context(business, "how many Oncology cases this year")
    assert "specialties" in ctx.tables
    assert ctx.value_hints == [{"table": "specialties", "column": "name", "value": "Oncology"}]


def test_common_words_are_not_value_hints(business):
    ctx = select_context(business, "Sales by department for each employee")
    # "Sales" starts the sentence and is a real value: allowed. Schema words are not values.
    assert all(h["value"] not in {"department", "employee"} for h in ctx.value_hints)
    ctx = select_context(business, "list the surgery appointments")
    assert {"table": "wards", "column": "name", "value": "Surgery"} in ctx.value_hints


def test_pii_values_never_reach_the_prompt(business):
    for question in ("customer email addresses", "employee ssn and salary",
                     f"who has the email {SECRET_EMAIL}", f"employee with ssn '{SECRET_SSN}'"):
        ctx = select_context(business, question, widen=True)
        assert SECRET_EMAIL not in ctx.prompt_text.replace(question, "")
        assert SECRET_SSN not in ctx.prompt_text
        assert all(h["value"] not in (SECRET_EMAIL, SECRET_SSN) for h in ctx.value_hints)
    ctx = select_context(business, "employee ssn and salary")
    assert "ssn TEXT [nullable] [sensitive]" in ctx.prompt_text
    assert SECRET_SSN not in str(ctx.schema)


# ------------------------------------------------------------------ widen, required, cap
def test_widen_returns_more_tables(business):
    question = "total order amount by customer"
    narrow = select_context(business, question)
    wide = select_context(business, question, widen=True)
    assert set(narrow.tables) < set(wide.tables)
    assert len(wide.tables) >= min(len(business.tables), len(narrow.tables) + 3)
    assert len(wide.tables) <= 24


def test_must_include_is_always_present(business):
    ctx = select_context(business, "Which physicians have the most appointments?",
                         must_include=("coupons", "WARDS", "no_such_table"))
    assert ctx.reasons["coupons"] == ctx.reasons["wards"] == "required"
    assert "no_such_table" not in ctx.tables
    assert {"physicians", "appointments"} <= set(ctx.tables)
    ctx = select_context(business, "appointments", must_include=["appointments"])
    assert ctx.reasons["appointments"] == "required"


def test_max_tables_cap_never_drops_matches(business):
    ctx = select_context(business, "orders customers products payments shipments", max_tables=3)
    assert len(ctx.tables) == 3
    assert set(ctx.reasons.values()) == {"matched"}
    ctx = select_context(business, "orders", max_tables=2, must_include=("wards", "coupons"))
    assert {"wards", "coupons"} <= set(ctx.tables)


def test_small_catalog_is_returned_whole():
    small = make_catalog({k: BUSINESS[k] for k in ("orders", "customers", "addresses",
                                                   "employees", "departments", "coupons")})
    ctx = select_context(small, "orders per customer")
    assert set(ctx.tables) == set(small.tables)
    assert ctx.tables[:2] == ["orders", "customers"] or ctx.tables[:2] == ["customers", "orders"]
    assert ctx.reasons["coupons"] == "small_schema"
    assert ctx.reasons["orders"] == "matched"


def test_mid_sized_catalog_is_still_narrowed():
    # Above SMALL_CATALOG (12) tables the selection is narrowed.
    names = list(BUSINESS)[:16]
    mid = make_catalog({k: BUSINESS[k] for k in names})
    ctx = select_context(mid, "freight cost per carrier")
    assert len(mid.tables) == 16
    assert {"shipments", "carriers"} <= set(ctx.tables) and len(ctx.tables) < 16


def test_no_match_falls_back_without_error(business):
    ctx = select_context(business, "zzz qqq")
    assert ctx.tables and set(ctx.reasons.values()) <= {"fallback", "bridge", "lookup"}
    assert select_context(business, "").tables
    empty = Catalog("t", "c", "sqlite", "h")
    ctx = select_context(empty, "anything")
    assert ctx.tables == [] and ctx.schema == {} and ctx.prompt_text == ""


# ------------------------------------------------------------------ llm filter
def test_llm_filter_prunes_candidates_only(business):
    seen = {}

    def keep_one(question, candidates):
        seen["question"] = question
        seen["candidates"] = candidates
        return ["appointments", "coupons", "not_a_table"]     # coupons is not a candidate

    ctx = select_context(business, "Which physicians have the most appointments?",
                         llm_filter=keep_one, must_include=("wards",))
    assert seen["question"].startswith("Which physicians")
    names = [c["name"] for c in seen["candidates"]]
    assert {"physicians", "appointments"} <= set(names)
    assert all({"name", "description", "columns", "score"} <= set(c) for c in seen["candidates"])
    matched = {t for t, r in ctx.reasons.items() if r == "matched"}
    assert matched == {"appointments"}
    assert "coupons" not in ctx.tables and ctx.reasons["wards"] == "required"


def test_llm_filter_failure_or_empty_answer_keeps_candidates(business):
    question = "Which physicians have the most appointments?"

    def boom(question, candidates):
        raise RuntimeError("model unavailable")

    for bad in (boom, lambda q, c: [], lambda q, c: None, lambda q, c: ["nonsense"]):
        ctx = select_context(business, question, llm_filter=bad)
        assert {"physicians", "appointments"} <= set(ctx.tables)


# ------------------------------------------------------------------ vectors
class FakeEmbedder:
    """Deterministic bag-of-concepts embedder: synonyms share a dimension."""
    name = "fake-embedder"
    CONCEPTS = {"earning": "salary", "wage": "salary", "medic": "physicians",
                "parcel": "shipments"}          # keyed by stem
    DIM = 96

    def __init__(self):
        self.queries = 0

    def _vec(self, text):
        vec = [0.0] * self.DIM
        for token in tokenize(text):
            token = stem(self.CONCEPTS.get(token, token))
            vec[int(hashlib.md5(token.encode()).hexdigest(), 16) % self.DIM] += 1.0
        return vec

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        self.queries += 1
        return self._vec(text)


def with_vectors(catalog: Catalog, embedder) -> Catalog:
    spec_free = Catalog(catalog.tenant_id, catalog.connection_id, catalog.db_type,
                        catalog.schema_hash, tables=catalog.tables,
                        relationships=catalog.relationships, built_at=catalog.built_at)
    names = sorted(catalog.tables)
    docs = [f"{n} {catalog.tables[n].description or ''}" for n in names]
    spec_free.table_vectors = dict(zip(names, embedder.embed_documents(docs)))
    for n in names:
        for col in catalog.tables[n].columns:
            spec_free.column_vectors[(n, col.name)] = embedder.embed_query(
                f"{col.name} {col.description or ''}")
    spec_free.embedder_name = embedder.name
    embedder.queries = 0
    return spec_free


def test_vector_fusion_finds_what_lexical_misses(business):
    embedder = FakeEmbedder()
    embedded = with_vectors(business, embedder)
    question = "average earnings of medics"
    lexical_only = select_context(business, question)
    assert "matched" not in lexical_only.reasons.values()      # no lexical overlap at all
    fused = select_context(embedded, question, embedder=embedder)
    assert {"employees", "physicians"} <= set(fused.tables)
    assert embedder.queries == 1
    assert max(fused.scores.values()) == 1.0
    score, misses = recall(embedded, QUESTIONS, embedder=embedder)
    assert score == 1.0, misses


def test_embedder_is_not_called_without_matching_vectors(business):
    embedder = FakeEmbedder()
    baseline = select_context(business, "freight cost per carrier")
    ctx = select_context(business, "freight cost per carrier", embedder=embedder)
    assert embedder.queries == 0 and ctx.tables == baseline.tables
    embedded = with_vectors(business, embedder)
    embedded.embedder_name = "some-other-model"
    select_context(embedded, "freight cost per carrier", embedder=embedder)
    assert embedder.queries == 0


def test_broken_embedder_falls_back_to_lexical(business):
    embedder = FakeEmbedder()
    embedded = with_vectors(business, embedder)

    class Broken(FakeEmbedder):
        def embed_query(self, text):
            raise RuntimeError("embedding service down")

    ctx = select_context(embedded, "freight cost per carrier", embedder=Broken())
    assert {"shipments", "carriers"} <= set(ctx.tables)


# ------------------------------------------------------------------ determinism
def test_output_is_deterministic(business):
    question = "total revenue per product category for shipped orders"
    first = select_context(business, question)
    again = select_context(business, question)
    shuffled = make_catalog(dict(reversed(list(BUSINESS.items()))))
    other = select_context(shuffled, question)
    assert first == again
    assert first.tables == other.tables and first.join_hints == other.join_hints
    assert first.scores == other.scores


def test_index_is_cached_on_the_catalog_and_refreshed_on_change():
    catalog = make_catalog(BUSINESS)
    index = get_index(catalog)
    assert get_index(catalog) is index
    catalog.tables = dict(catalog.tables)
    catalog.tables["gadgets"] = CatalogTable("gadgets", columns=[CatalogColumn("id", "INT")])
    assert get_index(catalog) is not index
    assert "gadgets" in select_context(catalog, "gadgets").tables


# ------------------------------------------------------------------ 500 tables
DOMAINS = ["billing", "hr", "logistics", "marketing", "support", "legal", "fleet", "energy",
           "retail", "clinical", "claims", "lending", "trading", "gaming", "travel", "hotel",
           "school", "library", "farm", "mining", "telecom", "media", "audit", "factory",
           "research"]
ENTITIES = ["invoices", "payslips", "parcels", "campaigns", "tickets", "contracts", "vehicles",
            "meters", "stores", "trials", "adjusters", "loans", "positions", "tournaments",
            "bookings", "rooms", "courses", "books", "harvests", "shafts"]
FILLER = ["created_at TIMESTAMP", "updated_at TIMESTAMP", "status TEXT", "amount NUMERIC",
          "quantity INTEGER", "notes TEXT", "owner_ref INTEGER", "region TEXT", "priority INTEGER",
          "is_active BOOLEAN", "external_ref TEXT", "score NUMERIC", "started_on DATE",
          "ended_on DATE", "source TEXT", "version INTEGER", "currency TEXT"]

BIG_QUESTIONS = [
    ("total amount of hr payslips per region", {"hr_payslips"}),
    ("how many logistics parcels were created this week", {"logistics_parcels"}),
    ("open support tickets by priority", {"support_tickets"}),
    ("which marketing campaigns have the highest score", {"marketing_campaigns"}),
    ("hotel rooms that are active", {"hotel_rooms"}),
    ("school courses started in 2025", {"school_courses"}),
    ("lending loans amount by currency", {"lending_loans"}),
    ("farm harvests quantity per region", {"farm_harvests"}),
    ("fleet vehicles and their energy meters", {"fleet_vehicles", "energy_meters"}),
    ("gaming tournaments joined with travel bookings", {"gaming_tournaments", "travel_bookings"}),
    ("legal contracts linked to billing invoices", {"legal_contracts", "billing_invoices"}),
    ("library books", {"library_books"}),
]


def big_spec() -> dict:
    spec = {}
    for d, domain in enumerate(DOMAINS):
        for e, entity in enumerate(ENTITIES):
            name = f"{domain}_{entity}"
            columns = [f"{entity[:-1]}_id INTEGER pk", "label TEXT"]
            if e:                                   # chain inside the domain
                previous = ENTITIES[e - 1]
                key = f"{previous[:-1]}_id"
                columns.append(f"{key} INTEGER fk={domain}_{previous}.{key}")
            columns += FILLER
            description = f"{entity} of the {domain} unit" if (d + e) % 2 else ""
            spec[name] = (description, 1000 + e, columns)
    return spec


@pytest.fixture(scope="module")
def big() -> Catalog:
    return make_catalog(big_spec())


def test_big_catalog_recall(big):
    assert len(big.tables) == 500
    assert all(len(t.columns) == 20 for t in list(big.tables.values())[1:5])
    score, misses = recall(big, BIG_QUESTIONS)
    assert score == 1.0, misses
    for question, expected in BIG_QUESTIONS:
        ctx = select_context(big, question)
        assert len(ctx.tables) <= 12
        assert ctx.tables[0] in expected
        assert len(ctx.prompt_text) <= 24000


def test_big_catalog_is_fast():
    catalog = make_catalog(big_spec())
    start = time.perf_counter()
    select_context(catalog, BIG_QUESTIONS[0][0])            # builds the index
    cold = time.perf_counter() - start
    start = time.perf_counter()
    for question, _ in BIG_QUESTIONS:
        select_context(catalog, question)
    warm = (time.perf_counter() - start) / len(BIG_QUESTIONS)
    assert cold < 1.0, cold
    assert warm < 0.1, warm
