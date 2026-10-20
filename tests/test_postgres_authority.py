"""Fast boundary tests; these do not claim PostgreSQL/provider execution."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app import postgres_authority as module
from app.store import WorkerPoolStore
from app.work import WorkOrchestrator

AUTHORITY = "51f8d7cb-133a-4e57-9cd9-d22b2e224aff"


def parameters():
    return {"host": "authority.example", "dbname": "fixture", "user": "fixture",
            "password": "synthetic-only", "sslmode": "verify-full",
            "sslrootcert": "system", "connect_timeout": "10"}


def authority(monkeypatch, **kwargs):
    # Construction performs no connection. The real driver is required by the
    # separate, explicitly invoked PostgreSQL contract suite.
    if module.psycopg is None:
        monkeypatch.setattr(module, "psycopg", object())
    return module.PostgresAuthority(parameters(), authority_id=AUTHORITY, **kwargs)


def test_pool_and_work_business_methods_are_inherited():
    pool_hooks = {"__init__", "_db", "_init"}
    work_hooks = {"__init__", "_db", "_init"}
    for name, value in WorkerPoolStore.__dict__.items():
        if callable(value) and name not in pool_hooks:
            assert getattr(module.PostgresWorkerPoolStore, name) is getattr(WorkerPoolStore, name)
    for name, value in WorkOrchestrator.__dict__.items():
        if callable(value) and name not in work_hooks:
            assert getattr(module.PostgresWorkOrchestrator, name) is getattr(WorkOrchestrator, name)


def test_lock_is_schema_bound_and_not_authority_identity_bound(monkeypatch):
    first = authority(monkeypatch, schema="ewp_test_one")
    other_id = module.PostgresAuthority(
        parameters(), schema="ewp_test_one",
        authority_id="91f8d7cb-133a-4e57-9cd9-d22b2e224aff")
    other_schema = authority(monkeypatch, schema="ewp_test_two")
    assert first.lock_key == other_id.lock_key
    assert first.lock_key != other_schema.lock_key
    assert first.connect_timeout == 10
    assert "connect_timeout" not in first._parameters


@pytest.mark.parametrize("schema", ["", "public", "pg_catalog", "pg_bad",
                                    "information_schema", "bad-name", "x;SELECT 1"])
def test_schema_names_fail_closed(monkeypatch, schema):
    with pytest.raises(module.AuthorityUnavailable, match="authority_schema_invalid"):
        authority(monkeypatch, schema=schema)


@pytest.mark.parametrize("change", [
    {"sslmode": "require"}, {"sslmode": "disable"}, {"sslrootcert": ""},
    {"host": "127.0.0.1,authority.example"}, {"host": "/tmp/socket"},
    {"service": "another"}, {"options": "-c search_path=public"},
    {"connect_timeout": "0"}, {"connect_timeout": "31"}, {"connect_timeout": "invalid"},
])
def test_operational_connection_rejects_implicit_or_weaker_identity(monkeypatch, change):
    authority(monkeypatch)
    with pytest.raises(module.AuthorityUnavailable):
        module.PostgresAuthority(dict(parameters(), **change), authority_id=AUTHORITY)


def test_sql_adapter_preserves_literals_and_binds_values():
    query = "SELECT 't.rowid ? 50%' AS literal,t.rowid FROM tasks t WHERE task_id=?"
    assert module.compile_pool_sql(query, 1) == (
        "SELECT 't.rowid ? 50%%' AS literal,t.__pool_fifo FROM tasks t WHERE task_id=%s")
    assert module.compile_pool_sql("SELECT '?' AS literal", 0) == "SELECT '?' AS literal"
    assert module.compile_pool_sql(
        "SELECT 1 /* ? t.rowid */ WHERE ?=1 -- ?\n", 1) == (
        "SELECT 1 /* ? t.rowid */ WHERE %s=1 -- ?\n")
    assert module.compile_pool_sql(
        "INSERT OR IGNORE INTO repository_lanes(repository) VALUES(?)", 1) == (
        "INSERT INTO repository_lanes(repository) VALUES(%s) ON CONFLICT DO NOTHING")


@pytest.mark.parametrize("query,count", [
    ("SELECT ?", 0), ("SELECT 1", 1), ("SELECT 1; DELETE FROM tasks", 0),
    ("SELECT 'unterminated", 0), ("SELECT /* unterminated", 0),
])
def test_sql_adapter_rejects_mismatched_or_stacked_statements(query, count):
    with pytest.raises(module.AuthorityUnavailable):
        module.compile_pool_sql(query, count)


def test_internal_fifo_does_not_change_public_task_shape():
    row = module._Cursor._row({"task_id": "synthetic", "__pool_fifo": 12})
    assert row == {"task_id": "synthetic"}
    assert module._Cursor._row(None) is None


def test_private_file_requires_posix_attestation_and_owner_only_mode(tmp_path):
    file = tmp_path / "database.url"
    file.write_text("synthetic-private-connection\n")
    file.chmod(0o600)
    if os.name != "posix":
        with pytest.raises(module.AuthorityUnavailable, match="acl_unverified"):
            module.read_private_connection_file(file)
        return
    assert module.read_private_connection_file(file) == "synthetic-private-connection"
    file.chmod(0o644)
    with pytest.raises(module.AuthorityUnavailable, match="credentials_file_invalid"):
        module.read_private_connection_file(file)
    file.chmod(0o600)
    alias = tmp_path / "alias.url"
    alias.symlink_to(file)
    with pytest.raises(module.AuthorityUnavailable, match="credentials_file_invalid"):
        module.read_private_connection_file(alias)


def test_private_file_rejects_hardlinks_and_oversized_values(tmp_path):
    if os.name != "posix":
        with pytest.raises(module.AuthorityUnavailable, match="acl_unverified"):
            module.read_private_connection_file(tmp_path / "absent")
        return
    file = tmp_path / "database.url"
    file.write_text("synthetic")
    file.chmod(0o600)
    alias = tmp_path / "linked"
    alias.hardlink_to(file)
    with pytest.raises(module.AuthorityUnavailable):
        module.read_private_connection_file(file)
    alias.unlink()
    file.write_bytes(b"x" * 8193)
    with pytest.raises(module.AuthorityUnavailable):
        module.read_private_connection_file(file)


def test_runtime_hooks_verify_without_migrating(monkeypatch):
    calls = []
    class Authority:
        def verify_schema(self):
            calls.append("verify")
        def clock(self):
            raise AssertionError("construction must not read database time")
        def initialize(self):
            raise AssertionError("runtime migration is forbidden")
    configured = Authority()
    pool = module.PostgresWorkerPoolStore(configured)
    work = module.PostgresWorkOrchestrator(configured, pool)
    assert pool.db_path is None and work.db_path is None
    assert calls == ["verify", "verify"]
    with pytest.raises(module.AuthorityUnavailable, match="work_authority_mismatch"):
        module.PostgresWorkOrchestrator(Authority(), pool)

def test_write_transaction_pins_isolation_before_lock_and_server_clock(monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    configured = authority(monkeypatch, schema="ewp_test_isolation")
    events = []
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)

    class Row:
        def fetchone(self):
            return {"now": now}

    class Raw:
        def execute(self, statement, values=None):
            events.append((statement, values))
            return Row()
        def rollback(self):
            events.append(("rollback", None))
        def close(self):
            events.append(("close", None))

    raw = Raw()
    monkeypatch.setattr(configured, "_connect", lambda: raw)
    monkeypatch.setattr(configured, "_verify_identity",
                        lambda connection: events.append(("verify_identity", connection)))
    monkeypatch.setattr(module, "sql", SimpleNamespace(SQL=str, Identifier=str), raising=False)
    db = configured.connection()
    db.execute("BEGIN IMMEDIATE")

    assert events[0] == ("BEGIN ISOLATION LEVEL READ COMMITTED READ WRITE", None)
    lock = events.index(("SELECT pg_catalog.pg_advisory_xact_lock(%s)",
                         (configured.lock_key,)))
    identity = events.index(("verify_identity", raw))
    assert 0 < lock < identity
    assert configured.clock() is now
    assert events[-1] == ("SELECT pg_catalog.clock_timestamp() AS now", None)
    assert configured._local.writer is db

    db.close()
    assert configured._local.writer is None
    assert events[-2:] == [("rollback", None), ("close", None)]
