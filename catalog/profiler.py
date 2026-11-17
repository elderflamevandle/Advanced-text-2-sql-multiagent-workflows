"""Data profiling for the schema catalog: row counts, distinct counts, value lists, PII.

Query budget per table (independent of the number of columns):
  1. one aggregate over a LIMITed subquery: COUNT(*), COUNT(col), COUNT(DISTINCT col)
  2. one ``SELECT cols ... LIMIT n`` for sample values
  3. one UNION ALL query listing the values of all low-cardinality columns (only if any)
  4. one ``COUNT(*)`` over the whole table, only when the sample hit max_rows_scanned
All SQL is built with sqlglot so identifiers are quoted for the dialect. Every failure is
contained: the affected statistics stay None and the build goes on.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlglot import exp

from catalog.models import CatalogColumn, CatalogTable
from database.dialects import to_sqlglot_dialect

logger = logging.getLogger(__name__)

_DEFAULT_SKIP_TYPES = (
    "blob", "bytea", "binary", "varbinary", "image", "json", "jsonb", "xml", "clob",
    "longtext", "mediumtext", "geometry", "geography", "tsvector", "bit varying",
    "struct", "map", "list", "array", "union",
)


@dataclass
class ProfileOptions:
    sample_values: int = 5              # sample values kept per column
    enum_max_distinct: int = 25         # at most this many distinct values -> full value list
    max_rows_scanned: int = 100_000     # rows read per table for statistics
    query_timeout: float = 10.0         # seconds per profiling query
    max_value_chars: int = 80           # stored values are truncated to this length
    profile_columns_limit: int = 80     # columns profiled per table
    skip_types: tuple[str, ...] = _DEFAULT_SKIP_TYPES
    sample_rows: int = 50               # rows fetched to derive sample values
    exact_row_count: bool = True        # COUNT(*) over the full table when the sample is cut


# ---------------------------------------------------------------------------
# PII detection
# ---------------------------------------------------------------------------

_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])|([A-Z]+)([A-Z][a-z])")
_SPLIT_RE = re.compile(r"[^a-z0-9]+")

_PERSON_PARTS = {"first", "last", "middle", "full", "sur", "given", "family", "maiden",
                 "nick", "legal", "preferred"}
_PERSON_ENTITIES = {
    "customer", "patient", "employee", "user", "member", "contact", "person", "staff",
    "client", "student", "doctor", "physician", "nurse", "guardian", "spouse", "beneficiary",
    "cardholder", "holder", "owner", "recipient", "sender", "driver", "passenger",
    "applicant", "candidate", "subscriber", "insured", "manager", "author", "teacher",
    "parent", "mother", "father", "child", "tenant", "payer", "payee", "provider",
}
_PERSON_TABLES = {
    "employee", "employees", "patient", "patients", "person", "persons", "people", "user",
    "users", "member", "members", "staff", "student", "students", "doctor", "doctors",
    "physician", "physicians", "nurse", "nurses", "contact", "contacts", "customer",
    "customers", "client", "clients", "applicant", "applicants", "candidate", "candidates",
    "passenger", "passengers", "subscriber", "subscribers", "driver", "drivers",
}
_NUMBER = {"number", "no", "num", "nbr", "nr"}
_IDENT = _NUMBER | {"id", "identifier", "code"}
# A name carrying one of these describes a property of the data, not the data itself
# (email_verified, phone_type, address_count, password_changed_at, token_usage).
_NEUTRAL = {
    "count", "cnt", "type", "flag", "verified", "status", "enabled", "valid", "confirmed",
    "optin", "opt", "template", "format", "length", "len", "score", "category", "kind",
    "required", "has", "is", "at", "usage", "total", "limit", "expiry", "expires",
    "attempts", "version", "strength", "provider", "domain", "preference", "consent",
    "tokens", "max", "min", "avg",
}
_BOOLEAN_TYPES = ("bool", "bit", "tinyint(1)")

_SINGLE = {
    "email": "email address", "emails": "email address", "emailaddress": "email address",
    "mail": "email address",
    "phone": "phone number", "phonenumber": "phone number", "mobile": "phone number",
    "telephone": "phone number", "tel": "phone number", "fax": "phone number",
    "msisdn": "phone number", "cellphone": "phone number",
    "ssn": "national identifier", "nin": "national identifier", "tin": "tax identifier",
    "itin": "tax identifier", "ein": "tax identifier", "aadhaar": "national identifier",
    "nationalid": "national identifier", "socialsecurity": "national identifier",
    "dob": "date of birth", "birth": "date of birth", "birthdate": "date of birth",
    "birthday": "date of birth", "dateofbirth": "date of birth",
    "address": "postal address", "addr": "postal address", "street": "postal address",
    "zip": "postal code", "zipcode": "postal code", "postcode": "postal code",
    "postalcode": "postal code",
    "firstname": "person name", "lastname": "person name", "surname": "person name",
    "fullname": "person name", "middlename": "person name", "givenname": "person name",
    "familyname": "person name", "maidenname": "person name", "username": "user name",
    "login": "user name",
    "passport": "passport number",
    "license": "licence number", "licence": "licence number", "drivers": "licence number",
    "cvv": "payment card", "cvc": "payment card", "cardnumber": "payment card",
    "creditcard": "payment card", "ccnumber": "payment card",
    "iban": "bank account", "routing": "bank account", "accountnumber": "bank account",
    "swift": "bank account", "bic": "bank account",
    "ip": "IP address", "ipaddress": "IP address", "ipv4": "IP address",
    "ipv6": "IP address",
    "password": "credential", "passwd": "credential", "pwd": "credential",
    "secret": "credential", "token": "credential", "apikey": "credential",
    "credential": "credential", "credentials": "credential", "passphrase": "credential",
    "privatekey": "credential", "salt": "credential",
    "mrn": "medical record identifier", "nhs": "medical record identifier",
    "patientid": "patient identifier",
}
# (all of these tokens, reason)
_COMBOS: tuple[tuple[frozenset, str], ...] = tuple(
    (frozenset(tokens), reason) for tokens, reason in (
        (("social", "security"), "national identifier"),
        (("national", "id"), "national identifier"),
        (("national", "insurance"), "national identifier"),
        (("tax", "id"), "tax identifier"),
        (("date", "of", "birth"), "date of birth"),
        (("postal", "code"), "postal code"),
        (("post", "code"), "postal code"),
        (("credit", "card"), "payment card"),
        (("debit", "card"), "payment card"),
        (("bank", "account"), "bank account"),
        (("api", "key"), "credential"),
        (("private", "key"), "credential"),
        (("access", "key"), "credential"),
        (("medical", "record"), "medical record identifier"),
        (("health", "card"), "medical record identifier"),
        (("e", "mail"), "email address"),
    )
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
_SSN_RE = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_CARD_RE = re.compile(r"\d(?:[ \-]?\d){12,18}")
_PHONE_RE = re.compile(r"\+?[\d(][\d\s().\-/]{6,}\d")
_IPV4_RE = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")
_DATE_RE = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}.*|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}.*")


def _tokens(name: str) -> list[str]:
    spaced = _CAMEL_RE.sub(lambda m: f"{m.group(1) or m.group(3)}_{m.group(2) or m.group(4)}",
                           name or "")
    return [t for t in _SPLIT_RE.split(spaced.lower()) if t]


def _name_reason(column_name: str, table_name: str | None) -> str | None:
    tokens = _tokens(column_name)
    if not tokens:
        return None
    token_set = set(tokens)
    if token_set & _NEUTRAL:
        return None
    for token in tokens:
        reason = _SINGLE.get(token)
        if reason is None:
            continue
        # address_id / phone_id point at another table: a key, not the value itself
        if reason in ("postal address", "phone number", "email address", "postal code") \
                and token_set & {"id", "key", "fk"}:
            continue
        if token == "ip" and len(tokens) > 1 and not token_set & {"address", "addr", "client",
                                                                 "remote", "source", "src",
                                                                 "host", "user", "login",
                                                                 "last", "v4", "v6"}:
            continue
        return reason
    for combo, reason in _COMBOS:
        if combo <= token_set:
            return reason
    if "name" in token_set and (token_set & _PERSON_PARTS or token_set & _PERSON_ENTITIES):
        return "person name"
    if "card" in token_set and token_set & (_NUMBER | {"pan"}):
        return "payment card"
    if token_set & {"account", "acct"} and token_set & _NUMBER:
        return "bank account"
    if "patient" in token_set and token_set & _IDENT:
        return "patient identifier"
    if token_set & {"insurance", "policy", "member", "subscriber"} and token_set & _NUMBER:
        return "insurance or membership number"
    if tokens == ["name"] and table_name:
        table_tokens = set(_tokens(table_name))
        if table_tokens & _PERSON_TABLES:
            return "person name"
    return None


def _luhn(digits: str) -> bool:
    total, double = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if double:
            d = d * 2
            if d > 9:
                d -= 9
        total += d
        double = not double
    return total % 10 == 0


def _is_phone(value: str) -> bool:
    value = value.strip()
    if not _PHONE_RE.fullmatch(value) or _DATE_RE.fullmatch(value):
        return False
    digits = sum(ch.isdigit() for ch in value)
    formatted = any(ch in value for ch in "+()-. ")
    return 10 <= digits <= 15 and formatted


def _is_ipv4(value: str) -> bool:
    value = value.strip()
    return bool(_IPV4_RE.fullmatch(value)) and all(int(p) <= 255 for p in value.split("."))


def _value_reason(values) -> str | None:
    texts = [str(v) for v in values if v is not None and not isinstance(v, (bytes, bool))]
    texts = [t for t in texts if t.strip()]
    if not texts:
        return None
    phones = ips = 0
    for text in texts:
        if _EMAIL_RE.search(text):
            return "values look like email addresses"
        if _SSN_RE.search(text):
            return "values look like national identifiers"
        stripped = text.strip()
        if _CARD_RE.fullmatch(stripped):
            digits = re.sub(r"\D", "", stripped)
            if 13 <= len(digits) <= 19 and (" " in stripped or "-" in stripped
                                              or len(digits) >= 15) and _luhn(digits):
                return "values look like payment card numbers"
        phones += _is_phone(text)
        ips += _is_ipv4(text)
    if phones * 2 >= len(texts) and phones:
        return "values look like phone numbers"
    if ips * 2 >= len(texts) and ips:
        return "values look like IP addresses"
    return None


def detect_pii(column_name: str, data_type: str | None = None, sample_values=(),
               *, table_name: str | None = None) -> tuple[bool, str | None]:
    """Decide whether a column holds personal or secret data.

    Uses the column name (whole-word matching, so ``filename`` or ``product_name`` do not
    match) and the shape of the sampled values. Returns ``(is_pii, reason)``.
    """
    dtype = (data_type or "").lower()
    if not any(dtype.startswith(b) for b in _BOOLEAN_TYPES):
        reason = _name_reason(column_name or "", table_name)
        if reason:
            return True, f"column name suggests {reason}"
    reason = _value_reason(sample_values or ())
    if reason:
        return True, reason
    return False, None


# ---------------------------------------------------------------------------
# SQL building
# ---------------------------------------------------------------------------

_TEXT_MARKERS = ("char", "text", "string", "enum", "uuid", "bool", "set(")
_INT_MARKERS = ("int", "integer", "smallint", "tinyint", "bigint", "year")


def _ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


def _table_expr(table: CatalogTable) -> exp.Table:
    name, schema = table.name, table.schema_name
    if schema and name.startswith(schema + "."):
        name = name[len(schema) + 1:]
    return exp.Table(this=_ident(name), db=_ident(schema) if schema else None)


def _sample_subquery(table: CatalogTable, columns: list[str], limit: int) -> exp.Subquery:
    select = exp.select(*[exp.column(_ident(c)) for c in columns]) if columns \
        else exp.select(exp.Literal.number(1).as_("one"))
    return select.from_(_table_expr(table)).limit(int(limit)).subquery("s")


def _sql(expression: exp.Expression, db_type: str) -> str:
    return expression.sql(dialect=to_sqlglot_dialect(db_type))


def _aggregate_sql(table: CatalogTable, columns: list[str], limit: int, db_type: str) -> str:
    projections: list[exp.Expression] = [exp.Count(this=exp.Star()).as_("n")]
    for i, name in enumerate(columns):
        col = exp.column(_ident(name))
        projections.append(exp.Count(this=col.copy()).as_(f"nn_{i}"))
        projections.append(
            exp.Count(this=exp.Distinct(expressions=[col.copy()])).as_(f"nd_{i}"))
    query = exp.select(*projections).from_(_sample_subquery(table, columns, limit))
    return _sql(query, db_type)


def _count_sql(table: CatalogTable, db_type: str) -> str:
    return _sql(exp.select(exp.Count(this=exp.Star()).as_("n")).from_(_table_expr(table)),
                db_type)


def _sample_sql(table: CatalogTable, columns: list[str], limit: int, db_type: str) -> str:
    query = exp.select(*[exp.column(_ident(c)) for c in columns]) \
        .from_(_table_expr(table)).limit(int(limit))
    return _sql(query, db_type)


def _enum_sql(table: CatalogTable, columns: list[tuple[int, str]], limit: int,
              per_column: int, db_type: str) -> str:
    """One statement returning (i, v) for the distinct values of several columns."""
    parts: list[exp.Expression] = []
    for index, name in columns:
        col = exp.column(_ident(name))
        inner = (
            exp.select(col.copy().as_("v")).distinct()
            .from_(_sample_subquery(table, [name], limit))
            .where(col.copy().is_(exp.null()).not_())
            .limit(int(per_column))
            .subquery(f"d_{index}")
        )
        parts.append(
            exp.select(
                exp.Literal.number(index).as_("i"),
                exp.cast(exp.column(_ident("v")), "text").as_("v"),
            ).from_(inner)
        )
    query = parts[0]
    for part in parts[1:]:
        query = exp.union(query, part, distinct=False)
    return _sql(query, db_type)


# ---------------------------------------------------------------------------
# Profiling
# ---------------------------------------------------------------------------

def _row_get(row: dict, key: str):
    if key in row:
        return row[key]
    lowered = key.lower()
    for k, v in row.items():
        if str(k).lower() == lowered:
            return v
    return None


def _as_int(value) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _clip(value, max_chars: int) -> str | None:
    if value is None or isinstance(value, (bytes, bytearray, memoryview)):
        return None
    text = str(value)
    if not text.strip():
        return None
    return text if len(text) <= max_chars else text[:max_chars - 1] + "…"


def _skipped(column: CatalogColumn, options: ProfileOptions) -> bool:
    dtype = (column.data_type or "").lower()
    return any(marker in dtype for marker in options.skip_types)


def _enum_eligible(column: CatalogColumn) -> bool:
    dtype = (column.data_type or "").lower()
    if any(m in dtype for m in _TEXT_MARKERS):
        return True
    if any(m in dtype for m in _INT_MARKERS):
        return not (column.is_primary_key or column.is_foreign_key)
    return False


def _cardinality(distinct: int, non_null: int, options: ProfileOptions) -> str | None:
    if non_null <= 0:
        return None
    ratio = distinct / non_null
    if ratio >= 0.9 and distinct > options.enum_max_distinct:
        return "high"
    if distinct <= options.enum_max_distinct or ratio <= 0.05:
        return "low"
    return "medium"


def _mark_pii(column: CatalogColumn, reason: str | None) -> None:
    column.is_pii = True
    column.pii_reason = reason
    column.sample_values = []
    column.enum_values = None


def _run(manager, sql: str, options: ProfileOptions, max_rows: int):
    return manager.execute_query(sql, timeout=options.query_timeout, max_rows=max_rows)


def profile_table(manager, db_type: str, table: CatalogTable, *,
                  options: ProfileOptions | None = None) -> None:
    """Fill row_count and per-column statistics of ``table`` in place. Never raises."""
    options = options or ProfileOptions()
    try:
        _profile(manager, db_type, table, options)
    except Exception as exc:  # noqa: BLE001 - profiling is best effort
        logger.debug("profile_table: %s failed (%s)", table.name, type(exc).__name__)
        for column in table.columns:
            if column.is_pii:
                column.sample_values, column.enum_values = [], None


def _profile(manager, db_type: str, table: CatalogTable, options: ProfileOptions) -> None:
    # Name-based PII first: such columns are never sampled at all.
    for column in table.columns:
        column.sample_values, column.enum_values = [], None
        flagged, reason = detect_pii(column.name, column.data_type, (), table_name=table.name)
        if flagged:
            _mark_pii(column, reason)
        else:
            column.is_pii, column.pii_reason = False, None

    profiled = [c for c in table.columns if not _skipped(c, options)]
    profiled = profiled[:max(0, options.profile_columns_limit)]
    names = [c.name for c in profiled]
    limit = max(1, int(options.max_rows_scanned))

    # 1. counts
    scanned = None
    try:
        rows = _run(manager, _aggregate_sql(table, names, limit, db_type), options, 1)
        stats = rows[0] if rows else {}
        scanned = _as_int(_row_get(stats, "n"))
        for i, column in enumerate(profiled):
            non_null = _as_int(_row_get(stats, f"nn_{i}"))
            distinct = _as_int(_row_get(stats, f"nd_{i}"))
            if scanned is None or non_null is None:
                continue
            column.distinct_count = distinct
            column.null_fraction = round(1 - non_null / scanned, 6) if scanned else None
            if distinct is not None:
                column.cardinality = _cardinality(distinct, non_null, options)
                if scanned < limit and non_null == scanned and scanned > 1 \
                        and distinct == scanned:
                    column.is_unique = True
    except Exception as exc:  # noqa: BLE001
        logger.debug("profile_table: aggregate on %s failed (%s)", table.name,
                     type(exc).__name__)
        try:
            rows = _run(manager, _aggregate_sql(table, [], limit, db_type), options, 1)
            scanned = _as_int(_row_get(rows[0], "n")) if rows else None
        except Exception as exc2:  # noqa: BLE001
            logger.debug("profile_table: count on %s failed (%s)", table.name,
                         type(exc2).__name__)

    table.row_count = scanned
    if scanned is not None and scanned >= limit and options.exact_row_count:
        try:
            rows = _run(manager, _count_sql(table, db_type), options, 1)
            total = _as_int(_row_get(rows[0], "n")) if rows else None
            if total is not None:
                table.row_count = total
        except Exception as exc:  # noqa: BLE001 - keep the lower bound
            logger.debug("profile_table: full count on %s failed (%s)", table.name,
                         type(exc).__name__)
    if not scanned:
        return          # empty, or not readable at all: nothing to sample

    # 2. sample values (PII columns are not selected)
    sampled = [c for c in profiled if not c.is_pii]
    if sampled and options.sample_values > 0:
        try:
            rows = _run(manager,
                        _sample_sql(table, [c.name for c in sampled],
                                    options.sample_rows, db_type),
                        options, options.sample_rows)
            for column in sampled:
                try:
                    raw = [_row_get(row, column.name) for row in rows]
                    flagged, reason = detect_pii(column.name, column.data_type, raw,
                                                 table_name=table.name)
                    if flagged:
                        _mark_pii(column, reason)
                        continue
                    seen: list[str] = []
                    for value in raw:
                        text = _clip(value, options.max_value_chars)
                        if text is not None and text not in seen:
                            seen.append(text)
                        if len(seen) >= options.sample_values:
                            break
                    column.sample_values = seen
                except Exception as exc:  # noqa: BLE001
                    column.sample_values = []
                    logger.debug("profile_table: samples of %s.%s failed (%s)", table.name,
                                 column.name, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001
            logger.debug("profile_table: sampling %s failed (%s)", table.name,
                         type(exc).__name__)

    # 3. value lists of low-cardinality columns, all in one statement
    enum_columns = [
        (i, c) for i, c in enumerate(profiled)
        if not c.is_pii and c.distinct_count is not None
        and 0 < c.distinct_count <= options.enum_max_distinct and _enum_eligible(c)
    ]
    if not enum_columns:
        return
    try:
        per_column = options.enum_max_distinct + 1
        sql = _enum_sql(table, [(i, c.name) for i, c in enum_columns], limit, per_column,
                        db_type)
        rows = _run(manager, sql, options, per_column * len(enum_columns))
        values: dict[int, list] = {}
        for row in rows:
            index = _as_int(_row_get(row, "i"))
            if index is not None:
                values.setdefault(index, []).append(_row_get(row, "v"))
        for index, column in enum_columns:
            raw = values.get(index) or []
            if not raw or len(raw) > options.enum_max_distinct:
                continue
            flagged, reason = detect_pii(column.name, column.data_type, raw,
                                         table_name=table.name)
            if flagged:
                _mark_pii(column, reason)
                continue
            clipped = [_clip(v, options.max_value_chars) for v in raw]
            column.enum_values = sorted({v for v in clipped if v is not None})
    except Exception as exc:  # noqa: BLE001
        logger.debug("profile_table: value lists of %s failed (%s)", table.name,
                     type(exc).__name__)
