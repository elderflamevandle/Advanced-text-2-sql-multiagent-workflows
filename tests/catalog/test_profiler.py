import sqlite3

import pytest
import sqlalchemy as sa

from catalog.models import Catalog, CatalogColumn, CatalogTable
from catalog.profiler import ProfileOptions, _aggregate_sql, _enum_sql, detect_pii, profile_table
from catalog.store import CatalogStore
from database.manager import DatabaseManager
from tests.catalog.conftest_build import SECRET_EMAIL, CountingManager, memory_engine

duckdb = pytest.importorskip("duckdb")

DDL = [
    """CREATE TABLE people (
        id INTEGER PRIMARY KEY, nickname VARCHAR, contact VARCHAR, status VARCHAR,
        level INTEGER, notes VARCHAR, score DOUBLE)""",
    'CREATE TABLE "order items" ("item id" INTEGER, "select" VARCHAR, "we""ird" VARCHAR)',
    "CREATE TABLE empty_table (id INTEGER, label VARCHAR)",
]


def _people_rows():
    rows = []
    for i in range(1, 101):
        rows.append((
            i, f"nick{i}", SECRET_EMAIL if i == 1 else f"user{i}@example.org",
            ["active", "inactive", "banned", "pending"][i % 4], i % 3,
            None if i % 4 == 0 else "x" * 200 + str(i), i * 1.5,
        ))
    return rows


def _item_rows():
    return [(i, ["a", "b"][i % 2], f"w{i}") for i in range(1, 31)]


def _people_table() -> CatalogTable:
    return CatalogTable(name="people", primary_keys=["id"], columns=[
        CatalogColumn(name="id", data_type="INTEGER", is_primary_key=True),
        CatalogColumn(name="nickname", data_type="VARCHAR"),
        CatalogColumn(name="contact", data_type="VARCHAR"),
        CatalogColumn(name="status", data_type="VARCHAR"),
        CatalogColumn(name="level", data_type="INTEGER"),
        CatalogColumn(name="notes", data_type="VARCHAR"),
        CatalogColumn(name="score", data_type="DOUBLE"),
    ])


def _items_table() -> CatalogTable:
    return CatalogTable(name="order items", columns=[
        CatalogColumn(name="item id", data_type="INTEGER"),
        CatalogColumn(name="select", data_type="VARCHAR"),
        CatalogColumn(name='we"ird', data_type="VARCHAR"),
    ])


@pytest.fixture(params=["sqlite", "duckdb"])
def db(request, tmp_path):
    db_type = request.param
    path = str(tmp_path / f"profile.{db_type}")
    conn = sqlite3.connect(path) if db_type == "sqlite" else duckdb.connect(path)
    for statement in DDL:
        conn.execute(statement)
    conn.executemany("INSERT INTO people VALUES (?,?,?,?,?,?,?)", _people_rows())
    conn.executemany('INSERT INTO "order items" VALUES (?,?,?)', _item_rows())
    if db_type == "sqlite":
        conn.commit()
    conn.close()
    manager = DatabaseManager(db_type=db_type, db_path=path)
    yield db_type, CountingManager(manager)
    manager.dispose()


def test_counts_nulls_and_cardinality(db):
    db_type, manager = db
    table = _people_table()
    profile_table(manager, db_type, table, options=ProfileOptions())

    assert table.row_count == 100
    assert table.column("id").distinct_count == 100
    assert table.column("id").null_fraction == 0.0
    assert table.column("id").cardinality == "high"
    assert table.column("id").is_unique
    assert table.column("notes").null_fraction == pytest.approx(0.25)
    assert table.column("status").distinct_count == 4
    assert table.column("status").cardinality == "low"
    assert len(manager.queries) == 3          # aggregate, sample rows, value lists


def test_enum_values_only_for_low_cardinality(db):
    db_type, manager = db
    table = _people_table()
    profile_table(manager, db_type, table, options=ProfileOptions())

    assert table.column("status").enum_values == ["active", "banned", "inactive", "pending"]
    assert table.column("level").enum_values == ["0", "1", "2"]       # small integer
    assert table.column("nickname").enum_values is None               # 100 distinct
    assert table.column("id").enum_values is None
    assert table.column("score").enum_values is None
    assert len(table.column("nickname").sample_values) == 5
    assert table.column("nickname").sample_values[0] == "nick1"


def test_values_are_truncated(db):
    db_type, manager = db
    table = _people_table()
    profile_table(manager, db_type, table, options=ProfileOptions(max_value_chars=20))
    samples = table.column("notes").sample_values
    assert samples and all(len(v) <= 20 for v in samples)


def test_identifiers_that_need_quoting(db):
    db_type, manager = db
    table = _items_table()
    profile_table(manager, db_type, table, options=ProfileOptions())
    assert table.row_count == 30
    assert table.column("item id").distinct_count == 30
    assert table.column("select").enum_values == ["a", "b"]
    assert table.column('we"ird').distinct_count == 30
    assert table.column('we"ird').sample_values[:2] == ["w1", "w2"]


def test_empty_table(db):
    db_type, manager = db
    table = CatalogTable(name="empty_table", columns=[
        CatalogColumn(name="id", data_type="INTEGER"),
        CatalogColumn(name="label", data_type="VARCHAR"),
    ])
    profile_table(manager, db_type, table, options=ProfileOptions())
    assert table.row_count == 0
    label = table.column("label")
    assert label.distinct_count == 0 and label.null_fraction is None
    assert label.sample_values == [] and label.enum_values is None
    assert len(manager.queries) == 1


def test_sample_limit_and_exact_count(db):
    db_type, manager = db
    table = _people_table()
    profile_table(manager, db_type, table, options=ProfileOptions(max_rows_scanned=10))
    assert table.row_count == 100                       # full COUNT(*) after a cut sample
    assert table.column("id").distinct_count == 10
    assert not table.column("id").is_unique             # cannot be known from a sample
    table = _people_table()
    profile_table(manager, db_type, table,
                  options=ProfileOptions(max_rows_scanned=10, exact_row_count=False))
    assert table.row_count == 10


def test_pii_by_value_is_never_kept(db):
    db_type, manager = db
    table = _people_table()
    profile_table(manager, db_type, table, options=ProfileOptions())
    contact = table.column("contact")
    assert contact.is_pii and "email" in contact.pii_reason
    assert contact.sample_values == [] and contact.enum_values is None
    assert contact.distinct_count == 100                # statistics are fine, values are not


def test_pii_never_reaches_the_stored_catalog(db):
    db_type, manager = db
    table = _people_table()
    table.columns.append(CatalogColumn(name="email", data_type="VARCHAR"))  # not in the db
    profile_table(manager, db_type, table, options=ProfileOptions())
    engine = memory_engine()
    store = CatalogStore(engine)
    store.save(Catalog("tenant-a", "c1", db_type, "h", tables={"people": table}))
    with engine.connect() as conn:
        names = sa.inspect(conn).get_table_names()
        dump = "".join(repr(conn.execute(sa.text(f'SELECT * FROM "{name}"')).all())
                       for name in names)
    assert "people" in dump and "active" in dump
    assert SECRET_EMAIL not in dump and "@example.org" not in dump
    engine.dispose()


def test_pii_columns_by_name_are_not_selected():
    class Recording:
        def __init__(self):
            self.queries = []

        def execute_query(self, sql, timeout=None, max_rows=None):
            self.queries.append(sql)
            return [{"n": 5, "nn_0": 5, "nd_0": 5, "nn_1": 5, "nd_1": 2}]

    manager = Recording()
    table = CatalogTable(name="people", columns=[
        CatalogColumn(name="email", data_type="VARCHAR"),
        CatalogColumn(name="status", data_type="VARCHAR"),
    ])
    profile_table(manager, "sqlite", table, options=ProfileOptions())
    assert table.column("email").is_pii
    assert len(manager.queries) == 3
    assert "email" in manager.queries[0]                       # counted
    assert all("email" not in sql for sql in manager.queries[1:])   # never read


def test_skip_types_and_column_limit(db):
    db_type, manager = db
    table = _people_table()
    table.column("notes").data_type = "JSON"
    profile_table(manager, db_type, table, options=ProfileOptions(profile_columns_limit=3))
    assert table.column("notes").distinct_count is None
    assert table.column("id").distinct_count == 100
    assert table.column("contact").distinct_count == 100       # third profiled column
    assert table.column("status").distinct_count is None       # beyond the limit
    assert "notes" not in manager.queries[0]


class _Raising:
    def __init__(self, fail_on=lambda sql: True):
        self.fail_on = fail_on
        self.calls = 0

    def execute_query(self, sql, timeout=None, max_rows=None):
        self.calls += 1
        if self.fail_on(sql):
            raise RuntimeError("password=hunter2 connection lost")
        return [{"n": 7}]


def test_failures_are_contained():
    table = _people_table()
    manager = _Raising()
    profile_table(manager, "postgresql", table, options=ProfileOptions())
    assert table.row_count is None
    assert all(c.distinct_count is None and c.sample_values == [] for c in table.columns)
    assert manager.calls == 2                 # aggregate, then the plain count fallback


def test_aggregate_failure_falls_back_to_a_row_count():
    table = _people_table()
    manager = _Raising(fail_on=lambda sql: "DISTINCT" in sql)
    profile_table(manager, "mysql", table, options=ProfileOptions())
    assert table.row_count == 7
    assert table.column("status").distinct_count is None


def test_garbage_rows_do_not_raise():
    class Weird:
        def execute_query(self, sql, timeout=None, max_rows=None):
            return [{"unexpected": object()}]

    table = _people_table()
    profile_table(Weird(), "sqlite", table, options=ProfileOptions())
    assert table.row_count is None


@pytest.mark.parametrize("dialect,quote", [("postgresql", '"'), ("mysql", "`"),
                                           ("sqlite", '"'), ("duckdb", '"')])
def test_sql_is_quoted_for_every_dialect(dialect, quote):
    import sqlglot

    from database.dialects import to_sqlglot_dialect

    table = _items_table()
    table.schema_name = "sales" if dialect == "postgresql" else None
    names = [c.name for c in table.columns]
    aggregate = _aggregate_sql(table, names, 1000, dialect)
    enums = _enum_sql(table, [(0, "select"), (2, 'we"ird')], 1000, 26, dialect)
    for sql in (aggregate, enums):
        sqlglot.parse_one(sql, read=to_sqlglot_dialect(dialect))      # valid for the dialect
        assert f"{quote}order items{quote}" in sql
        assert f"{quote}select{quote}" in sql
    assert "LIMIT 1000" in aggregate
    if dialect == "postgresql":
        assert '"sales"."order items"' in aggregate
    if dialect == "mysql":
        assert "CAST(`v` AS CHAR)" in enums


@pytest.mark.parametrize("name", [
    "email", "Email", "customer_email", "EmailAddress", "e_mail", "phone", "PhoneNumber",
    "mobile_phone", "Fax", "ssn", "social_security_number", "national_id", "tax_id", "dob",
    "BirthDate", "date_of_birth", "Address", "street_address", "BillingAddress", "zip_code",
    "PostalCode", "FirstName", "last_name", "full_name", "customer_name", "patient_name",
    "passport_number", "drivers_license", "card_number", "credit_card", "cvv", "iban",
    "account_number", "bank_account", "ip_address", "client_ip", "password", "password_hash",
    "api_key", "secret", "access_token", "mrn", "patient_id", "medical_record_number",
    "policy_number", "username",
])
def test_pii_by_name(name):
    flagged, reason = detect_pii(name, "varchar", [])
    assert flagged and reason


@pytest.mark.parametrize("name", [
    "filename", "file_name", "product_name", "company_name", "table_name", "name", "title",
    "description", "email_verified", "is_email_valid", "phone_type", "address_id",
    "address_count", "token_count", "card_type", "status", "country", "city", "state",
    "unit_price", "skip", "shipping_method", "membership_level", "account_id", "tips",
    "mailing_list_id", "birth_rate_category", "password_changed_at", "ship_ip_count",
    "license_type", "ssn_verified", "created_at", "nameplate", "phonetic_code",
])
def test_pii_name_false_positives(name):
    assert detect_pii(name, "varchar", []) == (False, None)


def test_pii_name_depends_on_the_table_for_a_bare_name():
    assert detect_pii("name", "text", [], table_name="employees")[0]
    assert not detect_pii("name", "text", [], table_name="products")[0]
    assert not detect_pii("email", "boolean", [])[0]


@pytest.mark.parametrize("values,expected", [
    (["note", "ask bob.smith@example.com about it"], "email"),
    (["123-45-6789"], "national"),
    (["4111 1111 1111 1111"], "card"),
    (["4111111111111111"], "card"),
    (["+1 (555) 010-9999", "555-010-8888 x", "+44 20 7946 0958"], "phone"),
    (["10.0.0.1", "192.168.1.20"], "IP"),
])
def test_pii_by_value(values, expected):
    flagged, reason = detect_pii("misc", "text", values)
    assert flagged and expected in reason


@pytest.mark.parametrize("values", [
    ["2024-01-31", "2023-12-01 10:00:00"],
    [1234567890, 9876543210],
    ["active", "inactive"],
    ["1234567812345678"],              # 16 digits, fails the Luhn check
    ["v1.2.3.4000"],
    [None, "", "  "],
    ["12.5", "13.75"],
    ["order 12 of 40", "+1 (555) 010-9999"][:1],
])
def test_pii_value_false_positives(values):
    assert detect_pii("misc", "text", values) == (False, None)
