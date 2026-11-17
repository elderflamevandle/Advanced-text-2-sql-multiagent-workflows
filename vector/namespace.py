"""Vector-store namespace for one database schema.

The namespace is ``{connection id or 'local'}-{first 12 hex chars of sha1(schema)}``:

* the connection id keeps two databases of the same type apart (the namespace used to be
  just the db_type, so a second SQLite database was served the first one's tables);
* the schema hash changes when a table, column, key or foreign key changes, which makes
  the next question embed the new schema instead of searching the stale one.

Sample rows are left out of the hash on purpose: they change with the data, not with the
schema, and re-embedding on every data change would be wasted work.

This module has no third-party imports so the schema linker can use it even when the
vector libraries are not installed.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

LOCAL_ID = "local"
_HASH_CHARS = 12
# Chroma collection names are limited to 63 characters of [A-Za-z0-9._-] and must start and
# end with an alphanumeric; the retriever appends "_tables"/"_columns" (8 chars). Pinecone
# namespaces are less strict, so the Chroma rules are the ones to satisfy.
_MAX_ID_CHARS = 40
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def schema_fingerprint(schema: dict[str, Any] | None) -> str:
    """Stable sha1 hex digest of the schema structure (tables in any order, no sample rows)."""
    structure = {}
    for table, data in (schema or {}).items():
        if isinstance(data, dict):
            structure[str(table)] = {k: v for k, v in data.items() if k != "sample_rows"}
        else:
            structure[str(table)] = data
    payload = json.dumps(structure, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _safe_id(connection_id: str | None) -> str:
    raw = str(connection_id).strip() if connection_id else ""
    if not raw:
        return LOCAL_ID
    safe = _UNSAFE.sub("-", raw).strip("-_")
    if safe == raw and len(safe) <= _MAX_ID_CHARS:
        return safe
    # The id had to be altered or shortened: add a hash of the original so two different
    # ids can never end up with the same namespace.
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    head = safe[: _MAX_ID_CHARS - len(digest) - 1].rstrip("-_")
    return f"{head}-{digest}" if head else f"c{digest}"


def schema_namespace(connection_id: str | None, schema: dict[str, Any] | None) -> str:
    """Namespace for *schema* of connection *connection_id* (``None`` -> ``'local'``)."""
    return f"{_safe_id(connection_id)}-{schema_fingerprint(schema)[:_HASH_CHARS]}"
