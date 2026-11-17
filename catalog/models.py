"""Data model of the schema catalog. Shared by every catalog module: keep it dependency-free."""
from __future__ import annotations

from dataclasses import dataclass, field

# Catalog / build states
MISSING = "missing"      # never built
BUILDING = "building"
READY = "ready"
STALE = "stale"          # built, but the database schema has changed since
ERROR = "error"


@dataclass
class CatalogColumn:
    name: str
    data_type: str = ""
    nullable: bool = True
    is_primary_key: bool = False
    is_foreign_key: bool = False
    is_unique: bool = False
    description: str | None = None
    description_source: str | None = None      # "llm" | "user"
    sample_values: list[str] = field(default_factory=list)   # always empty for PII columns
    enum_values: list[str] | None = None       # full value list, low-cardinality columns only
    distinct_count: int | None = None
    null_fraction: float | None = None
    cardinality: str | None = None             # "low" | "medium" | "high"
    is_pii: bool = False
    pii_reason: str | None = None


@dataclass
class CatalogTable:
    name: str
    schema_name: str | None = None
    columns: list[CatalogColumn] = field(default_factory=list)
    primary_keys: list[str] = field(default_factory=list)
    row_count: int | None = None
    description: str | None = None
    description_source: str | None = None      # "llm" | "user"
    content_hash: str = ""                     # hash of name + columns + keys (not of data)

    def column(self, name: str) -> CatalogColumn | None:
        return next((c for c in self.columns if c.name == name), None)


@dataclass
class CatalogRelationship:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    kind: str = "foreign_key"                  # "foreign_key" | "inferred"
    confidence: float = 1.0

    @property
    def join_condition(self) -> str:
        return f"{self.from_table}.{self.from_column} = {self.to_table}.{self.to_column}"


@dataclass
class Catalog:
    tenant_id: str
    connection_id: str
    db_type: str
    schema_hash: str
    tables: dict[str, CatalogTable] = field(default_factory=dict)
    relationships: list[CatalogRelationship] = field(default_factory=list)
    built_at: str | None = None                # ISO 8601 UTC
    embedder_name: str | None = None           # None: no vectors, lexical search only
    table_vectors: dict[str, list[float]] = field(default_factory=dict)
    column_vectors: dict[tuple[str, str], list[float]] = field(default_factory=dict)


@dataclass
class CatalogStatus:
    state: str = MISSING
    schema_hash: str | None = None
    table_count: int = 0
    column_count: int = 0
    relationship_count: int = 0
    progress: float = 0.0                      # 0..1 while building
    described: bool = False                    # LLM descriptions present
    embedded: bool = False                     # vectors present
    error: str | None = None                   # safe message, never credentials or row data
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class SchemaContext:
    """What the pipeline needs about the database for one question."""
    tables: list[str]
    schema: dict                               # legacy schema dict narrowed to `tables`
    prompt_text: str                           # rendered context for LLM prompts
    join_hints: list[dict] = field(default_factory=list)    # {"from","to","on","kind"}
    value_hints: list[dict] = field(default_factory=list)   # {"table","column","value"}
    scores: dict[str, float] = field(default_factory=dict)
    # table -> matched | bridge | lookup | required | small_schema | fallback
    reasons: dict[str, str] = field(default_factory=dict)
