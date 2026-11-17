# Phase 3 contracts (schema catalog, retrieval, query memory)

Fixed interfaces so three work packages can be built in parallel. Follow the signatures
exactly; tell the integrator before changing one. Everything is synchronous and
thread-safe (callers use `asyncio.to_thread`), tenant-scoped in the SQL itself, and stored
in the shared metadata database (`database.metadata.get_engine()`, SQLAlchemy Core,
portable to SQLite and PostgreSQL). Data model: `catalog/models.py` (done, do not edit).
Embedder: `catalog/embedding.py` (done): `get_embedder()` may return `None`.

## Package A: build side
```python
# catalog/store.py
class CatalogStore:
    def __init__(self, engine=None): ...
    def save(self, catalog: Catalog) -> None                 # replaces the connection's catalog atomically
    def load(self, tenant_id, connection_id) -> Catalog | None   # None unless a built catalog exists
    def status(self, tenant_id, connection_id) -> CatalogStatus
    def set_status(self, tenant_id, connection_id, **fields) -> None
    def set_description(self, tenant_id, connection_id, table, column, text) -> None
        # column=None for a table; source becomes "user"; survives rebuilds; KeyError if unknown
    def delete(self, tenant_id, connection_id) -> None
    def version(self, tenant_id, connection_id) -> str | None   # changes whenever the catalog changes (cache key)
def get_catalog_store() -> CatalogStore ; def set_catalog_store(store | None) -> None

# catalog/profiler.py
def profile_table(manager, db_type: str, table: CatalogTable, *, options: ProfileOptions) -> None
    # fills row_count and column stats in place through manager.execute_query (read-only)

# catalog/describer.py
def describe_tables(catalog: Catalog, tables: list[str], *, llm=None) -> int   # returns tables described

# catalog/builder.py
def build_catalog(tenant_id, connection_id, *, registry=None, store=None, embedder=..., describe=None,
                  force=False, progress=None) -> CatalogStatus
def schema_hash(schema: dict) -> str     # legacy schema dict -> stable hash (structure only, no sample rows)

# catalog/jobs.py
def start_build(tenant_id, connection_id, *, force=False) -> CatalogStatus   # returns at once; one build per connection
def wait_for_build(tenant_id, connection_id, timeout=None) -> CatalogStatus  # tests
```

## Package B: read side
```python
# catalog/graph.py
class RelationshipGraph:
    def __init__(self, catalog: Catalog)
    def bridge(self, tables: list[str], *, max_hops=4) -> list[str]     # extra tables needed to connect them
    def lookups(self, tables: list[str]) -> list[str]                   # referenced tables that give names for ids
    def join_hints(self, tables: list[str]) -> list[dict]               # {"from","to","on","kind"}
def infer_relationships(tables: dict[str, CatalogTable], existing: list[CatalogRelationship]) -> list[CatalogRelationship]
    # naming-convention joins for databases without declared foreign keys; kind="inferred"

# catalog/retrieval.py
def select_context(catalog: Catalog, question: str, *, embedder=None, llm_filter=None,
                   max_tables=12, widen=False, must_include=()) -> SchemaContext

# catalog/render.py
def render_context(catalog: Catalog, tables: list[str], join_hints, value_hints, *, max_chars=24000) -> str
def to_legacy_schema(catalog: Catalog, tables: list[str]) -> dict

# catalog/service.py   (the only catalog entry point for graph nodes and the API)
class CatalogService:
    def start_build(self, tenant_id, connection_id, *, force=False) -> CatalogStatus
    def status(self, tenant_id, connection_id) -> CatalogStatus
    def select_context(self, tenant_id, connection_id, question, *, widen=False, must_include=()) -> SchemaContext | None
        # None when no READY/STALE catalog exists: the caller falls back to the legacy path
    def describe(self, tenant_id, connection_id) -> dict      # tables, columns, descriptions for the API (no samples for PII)
    def set_description(self, tenant_id, connection_id, table, column, text) -> None
    def delete(self, tenant_id, connection_id) -> None
def get_catalog_service() -> CatalogService ; def set_catalog_service(service | None) -> None

# memory/service.py
@dataclass
class Example: question: str; sql: str; verified: bool; score: float
class MemoryService:
    def log_query(self, tenant_id, connection_id, *, question, sql, status, error_type=None,
                  tables=(), thread_id=None) -> str                      # returns query_id; never stores result rows
    def record_feedback(self, tenant_id, query_id, *, rating: str, comment=None, corrected_sql=None) -> None
        # rating "up" | "down"; LookupError when the query is not this tenant's
    def similar_examples(self, tenant_id, connection_id, question, *, k=3) -> list[Example]
    def add_lesson(self, tenant_id, connection_id, text, *, source: str) -> None   # source "correction" | "feedback"
    def lessons(self, tenant_id, connection_id, *, limit=8) -> list[str]
    def delete_connection(self, tenant_id, connection_id) -> None
def get_memory_service() -> MemoryService ; def set_memory_service(service | None) -> None
```

## Package C: pipeline and API (uses only catalog.service and memory.service)
New state fields: `schema_context: str`, `join_hints: list`, `examples: list`, `lessons: list`,
`query_id: str`, `clarification_question: str`, `reselect_count: int`, `required_tables: list`.

## Rules for all packages
- PII: columns flagged `is_pii` never have sample or enum values stored, embedded, rendered
  into prompts, or returned by the API.
- Nothing may log or store credentials or result rows.
- Another tenant's connection is indistinguishable from a missing one.
- Tests never touch `data/chinook.db` for writing and never create files inside the repo.
