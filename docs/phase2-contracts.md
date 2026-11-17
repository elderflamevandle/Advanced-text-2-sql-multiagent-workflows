# Phase 2 contracts (service, tenancy, state)

These interfaces are fixed so the three work packages can be built in parallel.
Do not change a signature here without telling the integrator.

## Environment variables
| Name | Default | Meaning |
|---|---|---|
| `T2S_SECRET_KEY` | none | Fernet key used to encrypt stored database credentials. Required when the metadata store is persistent; an ephemeral key (with a warning) is allowed only for in-memory stores. |
| `T2S_METADATA_URL` | `sqlite:///.t2s/metadata.db` | SQLAlchemy URL for tenants, API keys and connections. |
| `T2S_CHECKPOINT_URL` | `memory://` | `memory://`, `sqlite:///path/to/file.db` or `postgresql://...` for LangGraph checkpoints. |
| `T2S_REDIS_URL` | none | When set, cache and rate limiting use Redis; otherwise in-process. |
| `T2S_ADMIN_API_KEY` | none | Bootstrap key for `/v1/admin/*`. Admin routes are disabled when unset. |
| `T2S_RATE_LIMIT_PER_MINUTE` | `60` | Per-tenant request budget for query endpoints. |
| `T2S_MAX_CONCURRENT_QUERIES` | `8` | Per-tenant in-flight query cap. |
| `T2S_ALLOW_FILE_DATABASES` | `false` | Whether tenants may register sqlite/duckdb file paths (single-tenant/dev only). |

## database/registry.py (package A)
```python
class ConnectionNotFound(LookupError): ...
class ConnectionRejected(ValueError): ...      # bad params / connection test failed

@dataclass(frozen=True)
class ConnectionInfo:
    id: str; tenant_id: str; name: str; db_type: str
    params_public: dict        # host/port/database/user/db_path/schema, never secrets
    created_at: str            # ISO 8601 UTC

class ConnectionRegistry:      # thread-safe, all methods synchronous
    def register(self, tenant_id: str, name: str, db_type: str, params: dict,
                 *, verify: bool = True) -> ConnectionInfo
    def list(self, tenant_id: str) -> list[ConnectionInfo]
    def get_info(self, tenant_id: str, connection_id: str) -> ConnectionInfo
    def delete(self, tenant_id: str, connection_id: str) -> None
    def get_manager(self, tenant_id: str, connection_id: str) -> DatabaseManager
    def get_schema(self, tenant_id: str, connection_id: str, *, refresh: bool = False) -> dict
    def close_all(self) -> None

def get_registry() -> ConnectionRegistry     # process-wide singleton built from env
def set_registry(registry: ConnectionRegistry | None) -> None   # tests / app startup
```
A connection owned by another tenant must be indistinguishable from a missing one:
every method raises `ConnectionNotFound`.

## Graph state (already added to graph/state.py)
`connection_id: Optional[str]` and `tenant_id: Optional[str]`. When `connection_id` is
set, nodes resolve the database through `get_registry().get_manager(tenant_id, connection_id)`.
When it is absent they fall back to the legacy `database.manager.get_active_manager()`.
No secrets and no manager objects ever go into state (state is checkpointed).

## database/metadata.py (integrator, done)
`get_engine() -> sqlalchemy.Engine` (cached, from `T2S_METADATA_URL`), `reset_engine()` for tests.

## graph/checkpointing.py and graph/builder.py (package C)
```python
@asynccontextmanager
async def open_checkpointer(url: str | None = None):   # yields a LangGraph checkpointer
    ...
def build_graph(checkpointer=None)    # None -> MemorySaver (current behaviour)
```

## infra/ratelimit.py and infra/cache.py (package C)
```python
class RateLimiter(Protocol):
    async def allow(self, key: str, limit: int, window_seconds: float) -> tuple[bool, float]
        # (allowed, retry_after_seconds)
def get_rate_limiter() -> RateLimiter          # Redis when T2S_REDIS_URL is set, else in-memory

class Cache(Protocol):
    async def get(self, key: str) -> Any | None          # JSON-serialisable values
    async def set(self, key: str, value: Any, ttl_seconds: float | None = None) -> None
    async def delete(self, key: str) -> None
def get_cache() -> Cache
```

## llm/context.py (package A)
```python
@contextmanager
def use_llm_credentials(groq_api_key: str | None = None, openai_api_key: str | None = None): ...
```
Sets request-scoped provider keys (contextvars). `get_llm` prefers them over `os.environ`.

## api/ (package B)
`api.main:create_app() -> FastAPI`, importable as `api.main:app`. Routes under `/v1`.
Auth: `X-API-Key` header -> tenant. The API obtains collaborators through
`app.state.registry`, `app.state.rate_limiter`, `app.state.graph` so tests can inject fakes.
