"""ConnectionRegistry: CRUD, tenant isolation, secrets, validation, persistence."""
import dataclasses
import logging
import sqlite3

import pytest
import sqlalchemy as sa

from database.credentials import (
    CredentialCipher,
    CredentialDecryptError,
    SecretKeyError,
    generate_key,
)
from database.registry import (
    ConnectionInfo,
    ConnectionNotFound,
    ConnectionRegistry,
    ConnectionRejected,
    get_registry,
    set_registry,
)

PASSWORD = "sup3r-S3cret-pw-91xQ"
PG = {"host": "db.example.com", "port": 5433, "database": "sales", "user": "reader",
      "password": PASSWORD, "schema": "public"}


def make_sqlite(path, rows=(("a", 1),)):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE items (owner TEXT, n INTEGER)")
    conn.executemany("INSERT INTO items VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("T2S_ALLOW_FILE_DATABASES", "T2S_ALLOW_PRIVATE_HOSTS",
                 "T2S_FILE_DATABASE_ROOT", "T2S_SECRET_KEY", "DB_PASSWORD", "DB_HOST"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def meta_url(tmp_path):
    return f"sqlite:///{tmp_path / 'meta.db'}"


@pytest.fixture
def key():
    return generate_key()


@pytest.fixture
def make_registry(meta_url, key):
    created = []

    def factory(*, use_key=None, **kwargs):
        engine = sa.create_engine(meta_url)
        kwargs.setdefault("host_validator", lambda host, port: None)
        registry = ConnectionRegistry(engine, cipher=CredentialCipher(use_key or key), **kwargs)
        created.append((registry, engine))
        return registry

    yield factory
    for registry, engine in created:
        registry.close_all()
        engine.dispose()


@pytest.fixture
def registry(make_registry):
    return make_registry()


@pytest.fixture
def allow_files(monkeypatch):
    monkeypatch.setenv("T2S_ALLOW_FILE_DATABASES", "true")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def test_register_list_get_delete(registry):
    info = registry.register("tenant-a", "warehouse", "postgresql", PG, verify=False)
    assert isinstance(info, ConnectionInfo)
    assert len(info.id) == 32 and info.tenant_id == "tenant-a" and info.name == "warehouse"
    assert info.db_type == "postgresql"
    assert info.params_public == {"host": "db.example.com", "port": 5433, "database": "sales",
                                  "user": "reader", "schema": "public"}
    assert info.created_at.endswith("+00:00")

    assert registry.get_info("tenant-a", info.id) == info
    second = registry.register("tenant-a", "other", "mysql",
                               {"host": "m.example.com", "database": "d", "user": "u"},
                               verify=False)
    assert second.params_public["port"] == 3306
    assert [c.id for c in registry.list("tenant-a")] == [info.id, second.id] or \
        {c.id for c in registry.list("tenant-a")} == {info.id, second.id}

    registry.delete("tenant-a", info.id)
    assert [c.id for c in registry.list("tenant-a")] == [second.id]
    with pytest.raises(ConnectionNotFound):
        registry.get_info("tenant-a", info.id)
    with pytest.raises(ConnectionNotFound):
        registry.delete("tenant-a", info.id)


def test_postgres_alias_and_frozen_info(registry):
    info = registry.register("t", "c", "Postgres", PG, verify=False)
    assert info.db_type == "postgresql"
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.name = "x"


def test_duplicate_name_rejected_per_tenant_only(registry):
    registry.register("tenant-a", "main", "postgresql", PG, verify=False)
    with pytest.raises(ConnectionRejected, match="already exists"):
        registry.register("tenant-a", "main", "postgresql", PG, verify=False)
    # the same name is fine for a different tenant
    registry.register("tenant-b", "main", "postgresql", PG, verify=False)
    assert len(registry.list("tenant-a")) == 1 and len(registry.list("tenant-b")) == 1


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

def test_tenant_cannot_see_use_delete_or_list_foreign_connection(registry, allow_files, tmp_path):
    db_b = make_sqlite(tmp_path / "b.db", [("b", 7)])
    b = registry.register("tenant-b", "b-db", "sqlite", {"db_path": db_b})
    registry.get_manager("tenant-b", b.id)          # live and cached for its owner

    assert registry.list("tenant-a") == []
    missing = "0" * 32
    for call in (registry.get_info, registry.get_manager, registry.get_schema, registry.delete):
        with pytest.raises(ConnectionNotFound) as foreign:
            call("tenant-a", b.id)
        with pytest.raises(ConnectionNotFound) as absent:
            call("tenant-a", missing)
        # indistinguishable from a connection that does not exist at all
        assert str(foreign.value) == str(absent.value)
        assert b.id not in str(foreign.value) and "tenant-b" not in str(foreign.value)
    with pytest.raises(ConnectionNotFound):
        with registry.lease("tenant-a", b.id):
            pass

    # the owner is unaffected by the failed attempts
    assert registry.get_info("tenant-b", b.id).name == "b-db"
    assert registry.get_manager("tenant-b", b.id).execute_query("SELECT n FROM items") == [{"n": 7}]


@pytest.mark.parametrize("tenant", [None, "", "   ", 42])
def test_invalid_tenant_is_not_found(registry, tenant):
    info = registry.register("tenant-a", "c", "postgresql", PG, verify=False)
    for call in (registry.get_info, registry.get_manager, registry.get_schema, registry.delete):
        with pytest.raises(ConnectionNotFound):
            call(tenant, info.id)
    assert registry.list(tenant) == []
    with pytest.raises(ConnectionRejected):
        registry.register(tenant, "x", "postgresql", PG, verify=False)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def test_secrets_absent_from_info_repr_logs_and_stored_encrypted(registry, meta_url, caplog,
                                                                 tmp_path):
    with caplog.at_level(logging.DEBUG):
        info = registry.register("tenant-a", "warehouse", "postgresql", PG, verify=False)
        listed = registry.list("tenant-a")
        fetched = registry.get_info("tenant-a", info.id)
        registry.get_manager("tenant-a", info.id)   # decrypts; psycopg2 is only needed on connect

    for obj in (info, fetched, *listed, registry):
        assert PASSWORD not in repr(obj) and PASSWORD not in str(obj)
    assert "password" not in info.params_public
    assert PASSWORD not in str(dataclasses.asdict(info))
    assert PASSWORD not in caplog.text

    engine = sa.create_engine(meta_url)
    with engine.connect() as conn:
        row = conn.execute(sa.text("SELECT * FROM t2s_connections")).mappings().one()
    engine.dispose()
    assert PASSWORD not in "".join(str(v) for v in row.values())
    assert row["secret_ciphertext"] and "password" not in row["params_public"]
    # and the raw database file does not contain the password bytes either
    raw = (tmp_path / "meta.db").read_bytes()
    assert PASSWORD.encode() not in raw


def test_manager_receives_decrypted_secret_and_never_server_env(make_registry, monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "operator-password")
    monkeypatch.setenv("DB_HOST", "operator-host")
    seen = {}

    def factory(db_type, **params):
        seen.update(params, db_type=db_type)
        return object()

    registry = make_registry(manager_factory=factory)
    info = registry.register("t", "c", "postgresql", PG, verify=False)
    registry.get_manager("t", info.id)
    assert seen["password"] == PASSWORD and seen["host"] == "db.example.com"

    seen.clear()
    no_pw = registry.register("t", "nopw", "mysql",
                              {"host": "m.example.com", "database": "d", "user": "u"},
                              verify=False)
    registry.get_manager("t", no_pw.id)
    assert seen["password"] == "" and seen["port"] == 3306   # explicit, so no env fallback


def test_persistence_across_instances_with_same_key(make_registry, allow_files, tmp_path):
    db = make_sqlite(tmp_path / "data.db", [("a", 3)])
    first = make_registry()
    pg = first.register("tenant-a", "pg", "postgresql", PG, verify=False)
    lite = first.register("tenant-a", "lite", "sqlite", {"db_path": db})
    first.close_all()

    seen = {}
    second = make_registry(manager_factory=lambda db_type, **p: seen.update(p) or object())
    assert {c.id for c in second.list("tenant-a")} == {pg.id, lite.id}
    assert second.get_info("tenant-a", pg.id) == pg
    second.get_manager("tenant-a", pg.id)
    assert seen["password"] == PASSWORD

    third = make_registry()
    assert third.get_manager("tenant-a", lite.id).execute_query("SELECT n FROM items") == [{"n": 3}]


def test_wrong_key_fails_cleanly(make_registry, caplog):
    info = make_registry().register("tenant-a", "pg", "postgresql", PG, verify=False)
    other = make_registry(use_key=generate_key())
    assert other.get_info("tenant-a", info.id) == info        # metadata needs no key
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(CredentialDecryptError) as err:
            other.get_manager("tenant-a", info.id)
    assert PASSWORD not in str(err.value) and PASSWORD not in caplog.text
    assert "T2S_SECRET_KEY" in str(err.value)
    assert other.open_count == 0


def test_key_rotation(make_registry, key, meta_url):
    info = make_registry().register("tenant-a", "pg", "postgresql", PG, verify=False)
    new_key = generate_key()
    seen = {}
    rotating = make_registry(use_key=f"{new_key},{key}",
                             manager_factory=lambda db_type, **p: seen.update(p) or object())
    rotating.get_manager("tenant-a", info.id)
    assert seen["password"] == PASSWORD                        # old ciphertext still readable
    assert rotating.reencrypt_all() == 1

    seen.clear()
    new_only = make_registry(use_key=new_key,
                             manager_factory=lambda db_type, **p: seen.update(p) or object())
    new_only.get_manager("tenant-a", info.id)
    assert seen["password"] == PASSWORD
    with pytest.raises(CredentialDecryptError):
        make_registry(use_key=key).get_manager("tenant-a", info.id)


def test_ciphertext_cannot_be_moved_to_another_tenants_row(make_registry, meta_url):
    registry = make_registry()
    a = registry.register("tenant-a", "pg", "postgresql", PG, verify=False)
    b = registry.register("tenant-b", "pg", "postgresql", {**PG, "password": "b-pw"},
                          verify=False)
    engine = sa.create_engine(meta_url)
    with engine.begin() as conn:
        conn.execute(sa.text(
            "UPDATE t2s_connections SET secret_ciphertext = "
            "(SELECT secret_ciphertext FROM t2s_connections WHERE id = :a) WHERE id = :b"),
            {"a": a.id, "b": b.id})
    engine.dispose()
    with pytest.raises(CredentialDecryptError):
        registry.get_manager("tenant-b", b.id)


def test_secret_key_required_for_persistent_store_but_not_in_memory(meta_url, caplog):
    engine = sa.create_engine(meta_url)
    with pytest.raises(SecretKeyError, match="T2S_SECRET_KEY"):
        ConnectionRegistry(engine)
    engine.dispose()

    memory = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool,
                              connect_args={"check_same_thread": False})
    with caplog.at_level(logging.WARNING):
        registry = ConnectionRegistry(memory, host_validator=lambda h, p: None)
    assert any("ephemeral" in r.getMessage() for r in caplog.records)
    info = registry.register("t", "c", "postgresql", PG, verify=False)
    assert registry.get_info("t", info.id) == info
    memory.dispose()


def test_get_registry_builds_singleton_from_env(monkeypatch, tmp_path):
    from database import metadata

    monkeypatch.setenv("T2S_METADATA_URL", f"sqlite:///{tmp_path / 'svc' / 'metadata.db'}")
    monkeypatch.setenv("T2S_SECRET_KEY", generate_key())
    metadata.reset_engine()
    set_registry(None)
    try:
        registry = get_registry()
        assert get_registry() is registry
        assert (tmp_path / "svc" / "metadata.db").exists()
        injected = object()
        set_registry(injected)
        assert get_registry() is injected
    finally:
        set_registry(None)
        metadata.reset_engine()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("db_type", ["sqlite", "duckdb"])
def test_file_databases_rejected_unless_allowed(registry, tmp_path, monkeypatch, db_type):
    path = make_sqlite(tmp_path / "x.db")
    with pytest.raises(ConnectionRejected, match="T2S_ALLOW_FILE_DATABASES"):
        registry.register("t", "f", db_type, {"db_path": path}, verify=False)
    monkeypatch.setenv("T2S_ALLOW_FILE_DATABASES", "1")
    info = registry.register("t", "f", db_type, {"db_path": path}, verify=False)
    assert info.params_public == {"db_path": path}
    # switching the flag off again blocks use of the stored connection too
    monkeypatch.setenv("T2S_ALLOW_FILE_DATABASES", "false")
    with pytest.raises(ConnectionRejected):
        registry.get_manager("t", info.id)


@pytest.mark.parametrize("path", [":memory:", "file::memory:?cache=shared",
                                  "file:x.db?mode=memory", "file:/etc/passwd?mode=rwc", "", None])
def test_memory_and_uri_paths_always_rejected(registry, allow_files, path):
    for db_type in ("sqlite", "duckdb"):
        with pytest.raises(ConnectionRejected):
            registry.register("t", "m", db_type, {"db_path": path}, verify=False)


def test_file_database_root_confines_paths(registry, allow_files, tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    inside = make_sqlite(root / "in.db")
    outside = make_sqlite(tmp_path / "out.db")
    monkeypatch.setenv("T2S_FILE_DATABASE_ROOT", str(root))
    registry.register("t", "in", "sqlite", {"db_path": inside})
    with pytest.raises(ConnectionRejected, match="outside"):
        registry.register("t", "out", "sqlite", {"db_path": outside})
    with pytest.raises(ConnectionRejected, match="outside"):
        registry.register("t", "dots", "sqlite", {"db_path": str(root / ".." / "out.db")})


@pytest.mark.parametrize("db_type,params", [
    ("oracle", PG),
    ("", PG),
    (None, PG),
    ("postgresql", {"database": "d", "user": "u"}),                       # host missing
    ("postgresql", {"host": "h.example.com", "user": "u"}),               # database missing
    ("mysql", {"host": "h.example.com", "database": "d"}),                # user missing
    ("postgresql", {**PG, "port": "not-a-port"}),
    ("postgresql", {**PG, "port": 70000}),
    ("postgresql", {**PG, "host": "/var/run/postgresql"}),                # unix socket path
    ("postgresql", {**PG, "host": "h.example.com/x?y"}),
    ("postgresql", {**PG, "database": "d host=evil.example.com"}),
    ("postgresql", {**PG, "schema": "public; DROP TABLE x"}),
    ("postgresql", {**PG, "sslrootcert": "/etc/passwd"}),                 # not on the allow-list
    ("mysql", {**PG}),                                                    # schema is pg-only
    ("postgresql", {**PG, "password": 12345}),
    ("postgresql", "host=h"),
])
def test_invalid_parameters_rejected_without_echoing_secrets(registry, db_type, params):
    with pytest.raises(ConnectionRejected) as err:
        registry.register("t", "c", db_type, params, verify=False)
    assert PASSWORD not in str(err.value)
    assert registry.list("t") == []


def test_bad_names_rejected(registry):
    for name in ("", "   ", None, "x" * 101, "a\nb"):
        with pytest.raises(ConnectionRejected):
            registry.register("t", name, "postgresql", PG, verify=False)


def test_verify_failure_raises_connection_rejected(registry, allow_files, tmp_path):
    with pytest.raises(ConnectionRejected, match="Could not connect"):
        registry.register("t", "gone", "sqlite", {"db_path": str(tmp_path / "missing.db")})
    assert registry.list("t") == []          # nothing stored
    ok = registry.register("t", "ok", "sqlite", {"db_path": make_sqlite(tmp_path / "ok.db")})
    assert registry.get_manager("t", ok.id).test_connection()


def test_verify_failure_message_never_contains_driver_text(make_registry, caplog):
    disposed = []

    class Exploding:
        def test_connection(self):
            raise RuntimeError(f"FATAL: password authentication failed (password={PASSWORD})")

        def dispose(self):
            disposed.append(True)

    registry = make_registry(manager_factory=lambda db_type, **p: Exploding())
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ConnectionRejected) as err:
            registry.register("t", "c", "postgresql", PG)
    assert PASSWORD not in str(err.value) and PASSWORD not in caplog.text
    # no chained driver exception that a traceback could print
    assert err.value.__cause__ is None and err.value.__context__ is None
    assert disposed == [True]                 # the probe manager is always closed

    def broken_factory(db_type, **params):
        raise RuntimeError(f"cannot build with {params['password']}")

    registry = make_registry(manager_factory=broken_factory)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ConnectionRejected) as err:
            registry.register("t", "c2", "postgresql", PG)
    assert PASSWORD not in str(err.value) and PASSWORD not in caplog.text


def test_verify_real_postgres_unreachable_is_rejected(registry):
    # No driver / no server in the test environment: must be a clean rejection either way.
    with pytest.raises(ConnectionRejected) as err:
        registry.register("t", "pg", "postgresql", {**PG, "host": "192.0.2.1"})
    assert PASSWORD not in str(err.value)


# ---------------------------------------------------------------------------
# SSRF guard (default host validator)
# ---------------------------------------------------------------------------

@pytest.fixture
def strict_registry(meta_url, key):
    engine = sa.create_engine(meta_url)
    registry = ConnectionRegistry(engine, cipher=CredentialCipher(key))
    yield registry
    registry.close_all()
    engine.dispose()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "169.254.169.254", "10.1.2.3",
                                  "192.168.1.10", "172.16.0.1", "0.0.0.0", "::1", "[::1]",
                                  "fd00::1", "::ffff:127.0.0.1", "100.64.0.1"])
def test_private_and_metadata_hosts_rejected_by_default(strict_registry, host):
    with pytest.raises(ConnectionRejected, match="private|resolved"):
        strict_registry.register("t", "c", "postgresql", {**PG, "host": host}, verify=False)


def test_public_ip_accepted_and_private_allowed_with_flag(strict_registry, monkeypatch):
    strict_registry.register("t", "public", "postgresql", {**PG, "host": "8.8.8.8"},
                             verify=False)
    monkeypatch.setenv("T2S_ALLOW_PRIVATE_HOSTS", "true")
    info = strict_registry.register("t", "local", "postgresql", {**PG, "host": "127.0.0.1"},
                                    verify=False)
    # the guard is applied again when the manager is built (DNS may have changed)
    monkeypatch.delenv("T2S_ALLOW_PRIVATE_HOSTS")
    with pytest.raises(ConnectionRejected):
        strict_registry.get_manager("t", info.id)


def test_custom_host_validator_hook(make_registry):
    def deny_internal(host, port):
        if host.endswith(".internal"):
            raise ConnectionRejected("Host is not allowed.")

    registry = make_registry(host_validator=deny_internal)
    with pytest.raises(ConnectionRejected, match="not allowed"):
        registry.register("t", "c", "mysql",
                          {"host": "db.internal", "database": "d", "user": "u"}, verify=False)
    registry.register("t", "c", "mysql",
                      {"host": "db.example.com", "database": "d", "user": "u"}, verify=False)
