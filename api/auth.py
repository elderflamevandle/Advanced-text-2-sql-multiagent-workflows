"""Tenants and API keys, stored in the shared metadata engine (database/metadata.py).

Tables: ``t2s_tenants`` and ``t2s_api_keys``. A key is generated server-side, returned once
and stored only as its SHA-256 hex digest plus a short display prefix. Keys carry 256 bits
of randomness, so an unsalted fast hash is appropriate (nothing to brute-force).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

KEY_PREFIX = "t2s_"
DISPLAY_PREFIX_LEN = 12
MAX_KEY_LENGTH = 200
_LAST_USED_GRANULARITY = timedelta(seconds=60)

metadata = sa.MetaData()

tenants_table = sa.Table(
    "t2s_tenants", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("disabled_at", sa.String(40), nullable=True),
)

api_keys_table = sa.Table(
    "t2s_api_keys", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("tenant_id", sa.String(32), sa.ForeignKey("t2s_tenants.id"),
              nullable=False, index=True),
    sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("prefix", sa.String(16), nullable=False),
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("revoked_at", sa.String(40), nullable=True),
    sa.Column("last_used_at", sa.String(40), nullable=True),
)


@dataclass(frozen=True)
class Tenant:
    id: str
    name: str
    created_at: str


@dataclass(frozen=True)
class ApiKeyRecord:
    id: str
    tenant_id: str
    prefix: str
    created_at: str
    revoked_at: str | None = None
    last_used_at: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


class AuthStore:
    """Synchronous and thread-safe; the API calls it through ``asyncio.to_thread``."""

    def __init__(self, engine_factory=None):
        if engine_factory is None:
            from database.metadata import get_engine
            engine_factory = get_engine
        self._engine_factory = engine_factory
        self._lock = threading.RLock()
        self._schema_engine = None

    def _engine(self) -> sa.Engine:
        engine = self._engine_factory()
        if self._schema_engine is not engine:
            metadata.create_all(engine)
            self._schema_engine = engine
        return engine

    def ensure_schema(self) -> None:
        with self._lock:
            self._engine()

    def ping(self) -> None:
        with self._lock, self._engine().connect() as conn:
            conn.execute(sa.text("SELECT 1"))

    # -- tenants ---------------------------------------------------------------

    def create_tenant(self, name: str) -> Tenant:
        tenant = Tenant(id=uuid.uuid4().hex, name=name, created_at=_iso(_now()))
        with self._lock, self._engine().begin() as conn:
            conn.execute(tenants_table.insert().values(
                id=tenant.id, name=tenant.name, created_at=tenant.created_at))
        return tenant

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        with self._lock, self._engine().connect() as conn:
            row = conn.execute(
                sa.select(tenants_table).where(tenants_table.c.id == tenant_id)).first()
        return Tenant(id=row.id, name=row.name, created_at=row.created_at) if row else None

    def list_tenants(self) -> list[Tenant]:
        with self._lock, self._engine().connect() as conn:
            rows = conn.execute(
                sa.select(tenants_table).order_by(tenants_table.c.created_at,
                                                  tenants_table.c.id)).all()
        return [Tenant(id=r.id, name=r.name, created_at=r.created_at) for r in rows]

    # -- keys ------------------------------------------------------------------

    def create_key(self, tenant_id: str) -> tuple[ApiKeyRecord, str]:
        """Create a key for the tenant. Returns (record, plaintext); plaintext is not stored."""
        plaintext = generate_api_key()
        record = ApiKeyRecord(id=uuid.uuid4().hex, tenant_id=tenant_id,
                              prefix=plaintext[:DISPLAY_PREFIX_LEN], created_at=_iso(_now()))
        with self._lock, self._engine().begin() as conn:
            conn.execute(api_keys_table.insert().values(
                id=record.id, tenant_id=tenant_id, key_hash=hash_api_key(plaintext),
                prefix=record.prefix, created_at=record.created_at))
        return record, plaintext

    def list_keys(self, tenant_id: str | None = None) -> list[ApiKeyRecord]:
        query = sa.select(api_keys_table).order_by(api_keys_table.c.created_at,
                                                   api_keys_table.c.id)
        if tenant_id is not None:
            query = query.where(api_keys_table.c.tenant_id == tenant_id)
        with self._lock, self._engine().connect() as conn:
            rows = conn.execute(query).all()
        return [ApiKeyRecord(id=r.id, tenant_id=r.tenant_id, prefix=r.prefix,
                             created_at=r.created_at, revoked_at=r.revoked_at,
                             last_used_at=r.last_used_at) for r in rows]

    def revoke_key(self, key_id: str) -> bool:
        """Revoke a key. False when the id is unknown; revoking twice is a no-op."""
        with self._lock, self._engine().begin() as conn:
            row = conn.execute(sa.select(api_keys_table.c.revoked_at)
                               .where(api_keys_table.c.id == key_id)).first()
            if row is None:
                return False
            if row.revoked_at is None:
                conn.execute(api_keys_table.update()
                             .where(api_keys_table.c.id == key_id)
                             .values(revoked_at=_iso(_now())))
        return True

    def authenticate(self, presented: str | None) -> Tenant | None:
        """Return the tenant for a valid, unrevoked key, else None."""
        if not presented or len(presented) > MAX_KEY_LENGTH \
                or not presented.startswith(KEY_PREFIX):
            return None
        digest = hash_api_key(presented)
        now = _now()
        with self._lock, self._engine().begin() as conn:
            row = conn.execute(
                sa.select(api_keys_table.c.id, api_keys_table.c.key_hash,
                          api_keys_table.c.revoked_at, api_keys_table.c.last_used_at,
                          tenants_table.c.id.label("tenant_id"), tenants_table.c.name,
                          tenants_table.c.created_at, tenants_table.c.disabled_at)
                .select_from(api_keys_table.join(tenants_table))
                .where(api_keys_table.c.key_hash == digest)).first()
            # The lookup is by digest; the comparison below keeps the final check constant-time.
            stored = row.key_hash if row is not None else "0" * 64
            matches = constant_time_equals(stored, digest)
            if row is None or not matches or row.revoked_at or row.disabled_at:
                return None
            stale = True
            if row.last_used_at:
                try:
                    stale = now - datetime.fromisoformat(row.last_used_at) \
                        >= _LAST_USED_GRANULARITY
                except ValueError:
                    stale = True
            if stale:  # at most one write per key per minute
                conn.execute(api_keys_table.update()
                             .where(api_keys_table.c.id == row.id)
                             .values(last_used_at=_iso(now)))
            return Tenant(id=row.tenant_id, name=row.name, created_at=row.created_at)
