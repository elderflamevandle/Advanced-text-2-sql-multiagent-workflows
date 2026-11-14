"""Tenant-scoped connection registry (interface: docs/phase2-contracts.md).

Stores each tenant's database connections in the metadata database, with the
secret part of the parameters encrypted (database/credentials.py), and hands
out cached ``DatabaseManager`` objects for them.

Security defaults
-----------------
* File databases (sqlite/duckdb) are refused unless ``T2S_ALLOW_FILE_DATABASES``
  is truthy; ``:memory:`` and ``file:`` URIs are always refused. When
  ``T2S_FILE_DATABASE_ROOT`` is set, paths must resolve inside that directory.
* Network hosts that are (or resolve to) loopback, link-local (including cloud
  metadata endpoints), private, CGNAT, multicast or otherwise non-global
  addresses are refused unless ``T2S_ALLOW_PRIVATE_HOSTS`` is truthy. The check
  runs at registration and again whenever a manager is built, which narrows
  (but cannot fully close) the DNS-rebinding window.
* Parameters are an allow-list per database type. Values never fall back to
  the server's own ``DB_*`` environment variables.
* Secrets never appear in ``ConnectionInfo``, log records or exception text.

Tenant isolation
----------------
Every statement filters on ``tenant_id`` in SQL and the live-manager cache is
keyed by ``(tenant_id, connection_id)``, so a foreign connection id behaves
exactly like a missing one: ``ConnectionNotFound``.

Live managers and eviction
--------------------------
Managers are created lazily and kept in an LRU (``max_open``) with idle expiry
(``idle_ttl_seconds``). Eviction is opportunistic (on registry calls, or call
``evict_idle()`` from a periodic task); there is no background thread.
A manager is never closed under running work: ``DatabaseManager`` counts its
in-flight calls and ``dispose_if_idle()`` refuses while that count is non-zero,
and callers that hold a manager across several calls can pin it with
``registry.lease()``. A disposed manager is not broken, only closed: its
connector reopens lazily, so a caller that obtained it just before eviction
still gets a correct result.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from database.credentials import CredentialCipher, CredentialDecryptError, cipher_from_env

logger = logging.getLogger(__name__)

ALLOW_FILE_DATABASES_ENV = "T2S_ALLOW_FILE_DATABASES"
ALLOW_PRIVATE_HOSTS_ENV = "T2S_ALLOW_PRIVATE_HOSTS"
FILE_DATABASE_ROOT_ENV = "T2S_FILE_DATABASE_ROOT"

DEFAULT_MAX_OPEN = 50
DEFAULT_IDLE_TTL_SECONDS = 900.0
DEFAULT_REVALIDATE_SECONDS = 60.0


class ConnectionNotFound(LookupError):
    """The connection does not exist or belongs to another tenant."""


class ConnectionRejected(ValueError):
    """The connection parameters are invalid or the connection test failed."""


@dataclass(frozen=True)
class ConnectionInfo:
    id: str
    tenant_id: str
    name: str
    db_type: str
    params_public: dict = field(default_factory=dict)
    created_at: str = ""


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_metadata = sa.MetaData()

connections_table = sa.Table(
    "t2s_connections",
    _metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("tenant_id", sa.String(255), nullable=False, index=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("db_type", sa.String(32), nullable=False),
    sa.Column("params_public", sa.Text, nullable=False),
    sa.Column("secret_ciphertext", sa.Text, nullable=False),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.UniqueConstraint("tenant_id", "name", name="uq_t2s_connections_tenant_name"),
)


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------

_TRUTHY = {"1", "true", "yes", "on", "y"}
_DB_TYPE_ALIASES = {"postgres": "postgresql", "pg": "postgresql"}
_FILE_TYPES = {"sqlite", "duckdb"}
_SECRET_KEYS = frozenset({"password"})

# required / optional parameter names per type. Anything else is rejected.
_PARAM_SPECS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "sqlite": (("db_path",), ()),
    "duckdb": (("db_path",), ()),
    "postgresql": (
        ("host", "database", "user"),
        ("port", "password", "schema", "statement_timeout_ms"),
    ),
    "mysql": (("host", "database", "user"), ("port", "password", "statement_timeout_ms")),
}
_DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306}

_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-\[\]]{1,253}$")
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_NAME_MAX = 100


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _reject(message: str) -> ConnectionRejected:
    return ConnectionRejected(message)


def _clean_text(key: str, value: object, *, max_len: int = 255, forbid: str = "") -> str:
    if not isinstance(value, str) or not value.strip():
        raise _reject(f"Parameter '{key}' must be a non-empty string.")
    value = value.strip()
    if len(value) > max_len or _CONTROL_RE.search(value):
        raise _reject(f"Parameter '{key}' is too long or contains control characters.")
    if any(ch in value for ch in forbid):
        raise _reject(f"Parameter '{key}' contains characters that are not allowed.")
    return value


def _clean_int(key: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise _reject(f"Parameter '{key}' must be an integer.")
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise _reject(f"Parameter '{key}' must be an integer.") from None
    if not low <= number <= high:
        raise _reject(f"Parameter '{key}' must be between {low} and {high}.")
    return number


def _is_forbidden_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not ip.is_global or ip.is_multicast


def default_host_validator(host: str, port: int) -> None:
    """Refuse hosts that are, or resolve to, non-public addresses (SSRF guard).

    Disabled when ``T2S_ALLOW_PRIVATE_HOSTS`` is truthy (single-tenant installs,
    docker-compose networks). Raises ``ConnectionRejected``.
    """
    if _env_flag(ALLOW_PRIVATE_HOSTS_ENV):
        return
    literal = host.strip("[]")
    try:
        addresses = [ipaddress.ip_address(literal)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            raise _reject("The database host could not be resolved.") from None
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0].split("%", 1)[0]))
            except ValueError:
                continue
        if not addresses:
            raise _reject("The database host could not be resolved.")
    if any(_is_forbidden_address(ip) for ip in addresses):
        raise _reject(
            "The database host is on a private, loopback or link-local network, "
            "which is not allowed."
        )


def _validate_file_path(raw: object) -> str:
    path = _clean_text("db_path", raw, max_len=1024)
    lowered = path.lower()
    if ":memory:" in lowered or lowered.startswith("file:") or "mode=memory" in lowered:
        raise _reject("In-memory databases and file: URIs cannot be registered.")
    resolved = Path(path).expanduser().resolve()
    root = os.environ.get(FILE_DATABASE_ROOT_ENV, "").strip()
    if root:
        root_path = Path(root).expanduser().resolve()
        if resolved != root_path and root_path not in resolved.parents:
            raise _reject("The database file is outside the allowed directory.")
    return str(resolved)


def _split_params(db_type: str, params: dict) -> tuple[dict, dict]:
    """Validate ``params`` and return ``(public, secret)``. No I/O."""
    if not isinstance(params, dict):
        raise _reject("Connection parameters must be an object.")
    required, optional = _PARAM_SPECS[db_type]
    unknown = sorted(str(k) for k in params if k not in required and k not in optional)
    if unknown:
        # Only names are echoed, and only if they look like identifiers.
        safe = [k for k in unknown if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,40}", k)]
        detail = f": {', '.join(safe)}" if len(safe) == len(unknown) else ""
        raise _reject(f"Unsupported parameter(s) for {db_type}{detail}.")
    missing = [k for k in required if params.get(k) in (None, "")]
    if missing:
        raise _reject(f"Missing required parameter(s) for {db_type}: {', '.join(missing)}.")

    public: dict = {}
    secret: dict = {}
    if db_type in _FILE_TYPES:
        if not _env_flag(ALLOW_FILE_DATABASES_ENV):
            raise _reject(
                f"File databases ({db_type}) are disabled. "
                f"Set {ALLOW_FILE_DATABASES_ENV}=true to allow them (single-tenant/dev only)."
            )
        public["db_path"] = _validate_file_path(params["db_path"])
        return public, secret

    host = _clean_text("host", params["host"], max_len=253)
    if not _HOST_RE.match(host):
        raise _reject("Parameter 'host' must be a host name or IP address.")
    public["host"] = host
    public["port"] = _clean_int("port", params.get("port") or _DEFAULT_PORTS[db_type], 1, 65535)
    public["database"] = _clean_text("database", params["database"], forbid="=/?\\ ")
    public["user"] = _clean_text("user", params["user"])
    if db_type == "postgresql":
        schema = _clean_text("schema", params.get("schema") or "public")
        names = [part.strip() for part in schema.split(",") if part.strip()]
        if not names or not all(_SCHEMA_RE.match(n) for n in names):
            raise _reject("Parameter 'schema' must be one or more plain schema names.")
        public["schema"] = ",".join(names)
    if params.get("statement_timeout_ms") is not None:
        public["statement_timeout_ms"] = _clean_int(
            "statement_timeout_ms", params["statement_timeout_ms"], 1, 3_600_000
        )
    password = params.get("password")
    if password is None:
        password = ""
    if not isinstance(password, str) or len(password) > 4096:
        raise _reject("Parameter 'password' must be a string.")
    secret["password"] = password
    return public, secret


def _normalise_db_type(db_type: object) -> str:
    if not isinstance(db_type, str):
        raise _reject("db_type must be a string.")
    value = db_type.strip().lower()
    value = _DB_TYPE_ALIASES.get(value, value)
    if value not in _PARAM_SPECS:
        raise _reject("Unsupported db_type. Supported: sqlite, duckdb, postgresql, mysql.")
    return value


def _valid_tenant(tenant_id: object) -> bool:
    return isinstance(tenant_id, str) and bool(tenant_id.strip()) and len(tenant_id) <= 255


# ---------------------------------------------------------------------------
# Live-manager cache
# ---------------------------------------------------------------------------

class _Entry:
    """One cached manager. ``build_lock`` serialises creation and schema loading."""

    __slots__ = ("manager", "build_lock", "leases", "last_used", "validated_at",
                 "schema_loaded", "dead")

    def __init__(self, now: float) -> None:
        self.manager = None
        self.build_lock = threading.Lock()
        self.leases = 0
        self.last_used = now
        self.validated_at = now
        self.schema_loaded = False
        self.dead = False


def _default_manager_factory(db_type: str, **params):
    from database.manager import DatabaseManager  # noqa: PLC0415
    return DatabaseManager(db_type=db_type, **params)


def _engine_is_persistent(engine: Engine) -> bool:
    url = engine.url
    if url.get_backend_name() != "sqlite":
        return True
    database = url.database or ""
    return not (database in ("", ":memory:") or "mode=memory" in str(url))


class ConnectionRegistry:
    """Thread-safe, synchronous registry of tenant database connections.

    Args:
        engine: metadata engine; defaults to ``database.metadata.get_engine()``.
        cipher: credential cipher; defaults to ``cipher_from_env`` (which
            raises ``SecretKeyError`` for a persistent store without a key).
        max_open: maximum number of live managers kept (LRU).
        idle_ttl_seconds: managers unused for this long are disposed.
        revalidate_seconds: how often a cached manager is re-checked against
            the metadata store, so a delete in another process takes effect.
        host_validator: ``(host, port) -> None`` raising ``ConnectionRejected``;
            defaults to ``default_host_validator``. Pass ``lambda h, p: None``
            to disable, or your own allow/deny policy.
        manager_factory: ``(db_type, **params) -> DatabaseManager`` (tests).
        clock: monotonic time source (tests).
    """

    def __init__(
        self,
        engine: Engine | None = None,
        *,
        cipher: CredentialCipher | None = None,
        max_open: int = DEFAULT_MAX_OPEN,
        idle_ttl_seconds: float = DEFAULT_IDLE_TTL_SECONDS,
        revalidate_seconds: float = DEFAULT_REVALIDATE_SECONDS,
        host_validator: Callable[[str, int], None] | None = None,
        manager_factory: Callable[..., object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if engine is None:
            from database.metadata import get_engine  # noqa: PLC0415
            engine = get_engine()
        self._engine = engine
        self._cipher = cipher or cipher_from_env(persistent=_engine_is_persistent(engine))
        self._max_open = max(1, int(max_open))
        self._idle_ttl = float(idle_ttl_seconds)
        self._revalidate = float(revalidate_seconds)
        self._host_validator = host_validator or default_host_validator
        self._manager_factory = manager_factory or _default_manager_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
        # Cache hits sweep for idle managers at most this often (misses always sweep).
        self._sweep_interval = min(30.0, self._idle_ttl / 4) if self._idle_ttl > 0 else 30.0
        self._last_sweep = clock()
        _metadata.create_all(self._engine)

    def __repr__(self) -> str:
        return (f"ConnectionRegistry(backend={self._engine.url.get_backend_name()!r}, "
                f"open={len(self._entries)}, max_open={self._max_open})")

    # ------------------------------------------------------------------
    # Rows
    # ------------------------------------------------------------------

    @staticmethod
    def _to_info(row) -> ConnectionInfo:
        try:
            public = json.loads(row.params_public)
        except (TypeError, ValueError):
            public = {}
        return ConnectionInfo(
            id=row.id, tenant_id=row.tenant_id, name=row.name, db_type=row.db_type,
            params_public=public if isinstance(public, dict) else {},
            created_at=row.created_at,
        )

    def _fetch_row(self, tenant_id: object, connection_id: object):
        """Return the row for this tenant's connection, or raise ConnectionNotFound."""
        if not _valid_tenant(tenant_id) or not isinstance(connection_id, str) or not connection_id:
            raise ConnectionNotFound("Connection not found.")
        t = connections_table
        stmt = sa.select(t).where(t.c.tenant_id == tenant_id, t.c.id == connection_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            raise ConnectionNotFound("Connection not found.")
        return row

    def _secrets_for(self, row) -> dict:
        data = self._cipher.decrypt(row.secret_ciphertext)
        # The ciphertext is bound to its row, so it cannot be transplanted
        # onto another tenant's connection by someone with database access.
        if data.get("cid") != row.id or data.get("tid") != row.tenant_id:
            raise CredentialDecryptError("Stored credentials do not belong to this connection.")
        secrets = data.get("secrets")
        return secrets if isinstance(secrets, dict) else {}

    def _build_manager(self, db_type: str, public: dict, secrets: dict):
        """Create a DatabaseManager. Raises ConnectionRejected with a safe message."""
        if db_type in _FILE_TYPES:
            if not _env_flag(ALLOW_FILE_DATABASES_ENV):
                raise _reject(f"File databases ({db_type}) are disabled.")
        else:
            self._host_validator(public["host"], int(public["port"]))
        kwargs = {**public, **secrets}
        try:
            return self._manager_factory(db_type, **kwargs)
        except ImportError:
            raise _reject(f"The {db_type} driver is not installed on this server.") from None
        except ConnectionRejected:
            raise
        except Exception as exc:  # noqa: BLE001 - driver text may contain credentials
            logger.warning("Could not create %s manager (%s)", db_type, type(exc).__name__)
            raise _reject("The connection could not be initialised with these parameters.") \
                from None

    @staticmethod
    def _dispose(manager) -> None:
        dispose = getattr(manager, "dispose", None)
        if dispose is None:
            return
        try:
            dispose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error while disposing a database manager (%s)", type(exc).__name__)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def register(self, tenant_id: str, name: str, db_type: str, params: dict,
                 *, verify: bool = True) -> ConnectionInfo:
        if not _valid_tenant(tenant_id):
            raise _reject("tenant_id is required.")
        if not isinstance(name, str) or not name.strip():
            raise _reject("Connection name is required.")
        name = name.strip()
        if len(name) > _NAME_MAX or _CONTROL_RE.search(name):
            raise _reject(f"Connection name must be at most {_NAME_MAX} printable characters.")
        db_type = _normalise_db_type(db_type)
        public, secret = _split_params(db_type, params)
        if db_type not in _FILE_TYPES:
            self._host_validator(public["host"], public["port"])

        if verify:
            self._verify(db_type, public, secret)

        connection_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ciphertext = self._cipher.encrypt(
            {"cid": connection_id, "tid": tenant_id, "secrets": secret}
        )
        try:
            with self._engine.begin() as conn:
                conn.execute(sa.insert(connections_table).values(
                    id=connection_id, tenant_id=tenant_id, name=name, db_type=db_type,
                    params_public=json.dumps(public, sort_keys=True),
                    secret_ciphertext=ciphertext, created_at=created_at,
                ))
        except IntegrityError:
            raise _reject("A connection with this name already exists.") from None
        logger.info("Registered connection id=%s tenant=%s db_type=%s",
                    connection_id, tenant_id, db_type)
        return ConnectionInfo(id=connection_id, tenant_id=tenant_id, name=name,
                              db_type=db_type, params_public=dict(public),
                              created_at=created_at)

    def _verify(self, db_type: str, public: dict, secret: dict) -> None:
        manager = self._build_manager(db_type, public, secret)
        try:
            try:
                ok = bool(manager.test_connection())
            except ImportError:
                raise _reject(f"The {db_type} driver is not installed on this server.") \
                    from None
            except Exception as exc:  # noqa: BLE001
                logger.info("Connection test raised %s for db_type=%s",
                            type(exc).__name__, db_type)
                ok = False
            if not ok:
                raise _reject(
                    "Could not connect to the database with the supplied parameters. "
                    "Check host, port, database name and credentials."
                )
        finally:
            self._dispose(manager)

    def list(self, tenant_id: str) -> list[ConnectionInfo]:
        if not _valid_tenant(tenant_id):
            return []
        t = connections_table
        stmt = sa.select(t).where(t.c.tenant_id == tenant_id).order_by(t.c.created_at, t.c.id)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [self._to_info(row) for row in rows]

    def get_info(self, tenant_id: str, connection_id: str) -> ConnectionInfo:
        return self._to_info(self._fetch_row(tenant_id, connection_id))

    def delete(self, tenant_id: str, connection_id: str) -> None:
        if not _valid_tenant(tenant_id) or not isinstance(connection_id, str):
            raise ConnectionNotFound("Connection not found.")
        t = connections_table
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.delete(t).where(t.c.tenant_id == tenant_id, t.c.id == connection_id)
            )
        if not result.rowcount:
            raise ConnectionNotFound("Connection not found.")
        self._drop_entry((tenant_id, connection_id))
        logger.info("Deleted connection id=%s tenant=%s", connection_id, tenant_id)

    # ------------------------------------------------------------------
    # Live managers
    # ------------------------------------------------------------------

    def _drop_entry(self, key: tuple[str, str]) -> None:
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is not None:
                entry.dead = True
        if entry is not None and entry.manager is not None:
            # DatabaseManager.dispose() defers itself while work is in flight.
            self._dispose(entry.manager)

    def _entry_idle(self, entry: _Entry) -> bool:
        if entry.leases > 0 or entry.build_lock.locked():
            return False
        return not bool(getattr(entry.manager, "in_use", False))

    def _try_close(self, entry: _Entry) -> bool:
        """Dispose an entry's manager unless it is busy. Returns True when closed."""
        manager = entry.manager
        if manager is None:
            return True
        closer = getattr(manager, "dispose_if_idle", None)
        if closer is not None:
            try:
                return bool(closer())
            except Exception as exc:  # noqa: BLE001
                logger.warning("Error while disposing a database manager (%s)",
                               type(exc).__name__)
                return True
        self._dispose(manager)
        return True

    def _evict(self, keep: tuple[str, str] | None = None) -> int:
        """Close idle-expired managers, then the least recently used over ``max_open``."""
        closed = 0
        with self._lock:
            now = self._last_sweep = self._clock()
            for key in list(self._entries):
                entry = self._entries[key]
                over = len(self._entries) > self._max_open
                expired = self._idle_ttl > 0 and now - entry.last_used >= self._idle_ttl
                if key == keep or not (over or expired) or not self._entry_idle(entry):
                    continue
                # Closing happens under the registry lock so the entry cannot be
                # handed out in between; dispose_if_idle() re-checks in-flight work.
                if self._try_close(entry):
                    entry.dead = True
                    del self._entries[key]
                    closed += 1
            if len(self._entries) > self._max_open:
                logger.warning(
                    "Connection registry holds %d live managers (max_open=%d): "
                    "all remaining ones are in use.", len(self._entries), self._max_open)
        return closed

    def evict_idle(self) -> int:
        """Dispose managers idle for longer than ``idle_ttl_seconds``. Returns the count."""
        return self._evict()

    def _acquire_entry(self, tenant_id: str, connection_id: str, *, lease: bool) -> _Entry:
        key = (tenant_id, connection_id)
        while True:
            with self._lock:
                now = self._clock()
                entry = self._entries.get(key)
                fresh = (entry is not None and entry.manager is not None
                         and now - entry.validated_at < self._revalidate)
                if fresh:
                    entry.last_used = now
                    self._entries.move_to_end(key)
                    if lease:
                        entry.leases += 1
                    sweep = (len(self._entries) > self._max_open
                             or now - self._last_sweep >= self._sweep_interval)
            if fresh:
                if sweep:
                    self._evict(keep=key)
                return entry
            # Miss or stale: the tenant-filtered SQL lookup is the authority.
            try:
                row = self._fetch_row(tenant_id, connection_id)
            except ConnectionNotFound:
                self._drop_entry(key)
                raise
            with self._lock:
                entry = self._entries.get(key)
                if entry is None:
                    entry = self._entries[key] = _Entry(self._clock())
                entry.leases += 1          # pin while building
                self._entries.move_to_end(key)
            try:
                with entry.build_lock:
                    if entry.manager is None and not entry.dead:
                        public = self._to_info(row).params_public
                        entry.manager = self._build_manager(
                            row.db_type, public, self._secrets_for(row))
            except BaseException:
                with self._lock:
                    entry.leases -= 1
                    if entry.manager is None and self._entries.get(key) is entry:
                        del self._entries[key]
                raise
            with self._lock:
                if not lease:
                    entry.leases -= 1
                dead = entry.dead          # deleted while we were building
                if dead and lease:
                    entry.leases -= 1
                if not dead:
                    entry.last_used = entry.validated_at = self._clock()
            if dead:
                if entry.manager is not None:
                    self._dispose(entry.manager)
                continue
            self._evict(keep=key)
            return entry

    def get_manager(self, tenant_id: str, connection_id: str):
        """Return the live ``DatabaseManager`` for this tenant's connection.

        Raises ``ConnectionNotFound`` (missing or foreign), ``ConnectionRejected``
        (policy now forbids it) or ``CredentialDecryptError`` (wrong key).
        """
        if not _valid_tenant(tenant_id) or not isinstance(connection_id, str) \
                or not connection_id:
            raise ConnectionNotFound("Connection not found.")
        return self._acquire_entry(tenant_id, connection_id, lease=False).manager

    @contextmanager
    def lease(self, tenant_id: str, connection_id: str) -> Iterator[object]:
        """Like ``get_manager`` but pins the manager against eviction for the block."""
        if not _valid_tenant(tenant_id) or not isinstance(connection_id, str) \
                or not connection_id:
            raise ConnectionNotFound("Connection not found.")
        entry = self._acquire_entry(tenant_id, connection_id, lease=True)
        try:
            yield entry.manager
        finally:
            with self._lock:
                entry.leases -= 1
                entry.last_used = self._clock()

    def get_schema(self, tenant_id: str, connection_id: str, *, refresh: bool = False) -> dict:
        """Return the (cached) schema; concurrent first calls introspect only once."""
        if not _valid_tenant(tenant_id) or not isinstance(connection_id, str) \
                or not connection_id:
            raise ConnectionNotFound("Connection not found.")
        entry = self._acquire_entry(tenant_id, connection_id, lease=True)
        try:
            manager = entry.manager
            if entry.schema_loaded and not refresh:
                return manager.get_schema()
            with entry.build_lock:
                if refresh:
                    schema = manager.refresh_schema()
                else:
                    schema = manager.get_schema()
                entry.schema_loaded = True
                return schema
        finally:
            with self._lock:
                entry.leases -= 1
                entry.last_used = self._clock()

    def close_all(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
            for entry in entries:
                entry.dead = True
        for entry in entries:
            if entry.manager is not None:
                self._dispose(entry.manager)

    @property
    def open_count(self) -> int:
        with self._lock:
            return sum(1 for e in self._entries.values() if e.manager is not None)

    # ------------------------------------------------------------------
    # Key rotation
    # ------------------------------------------------------------------

    def reencrypt_all(self) -> int:
        """Re-encrypt every stored secret with the primary key. Returns the row count.

        Run once after prepending a new key to ``T2S_SECRET_KEY``; afterwards
        the old key can be removed from the list.
        """
        t = connections_table
        count = 0
        with self._engine.begin() as conn:
            for row in conn.execute(sa.select(t.c.id, t.c.secret_ciphertext)).all():
                conn.execute(sa.update(t).where(t.c.id == row.id).values(
                    secret_ciphertext=self._cipher.rotate(row.secret_ciphertext)))
                count += 1
        return count


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_registry: ConnectionRegistry | None = None
_registry_lock = threading.Lock()


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        logger.warning("%s is not a number; using %s", name, default)
        return default


def get_registry() -> ConnectionRegistry:
    """Return the process-wide registry, building it from the environment on first use."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ConnectionRegistry(
                    max_open=int(_env_number("T2S_MAX_OPEN_CONNECTIONS", DEFAULT_MAX_OPEN)),
                    idle_ttl_seconds=_env_number("T2S_CONNECTION_IDLE_TTL",
                                                 DEFAULT_IDLE_TTL_SECONDS),
                )
    return _registry


def set_registry(registry: ConnectionRegistry | None) -> None:
    global _registry
    _registry = registry
