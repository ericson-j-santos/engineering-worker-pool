"""Contrato DEV: task fixada por host ou distribuída em pool elegível.

Fixtures sintéticas: não atestam runner, Gateway nem execução física.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.store import ConflictError, WorkerPoolStore


class Clock:
    def __init__(self):
        self.value = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds: int):
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def pool(tmp_path):
    clock = Clock()
    store = WorkerPoolStore(
        tmp_path / "route.db", clock=clock, expected_rules_sha="a" * 40,
        heartbeat_ttl_seconds=90, default_lease_seconds=120,
        progress_stall_seconds=30, default_max_attempts=3,
    )
    return store, clock


def worker(store, name: str, host: str, role: str = "builder"):
    return store.register_worker(
        worker_id=name, host=host, role=role, profile="NORMAL",
        correlation_id="route-" + name, rules_sha="a" * 40,
        gateway_ok=True, state_validated=True,
        worktree_root=f"C:/dev/chatgpt-workers/{name}",
    )


def task(store, request_id="route-1", **extra):
    return store.enqueue_task(
        repository="ericson-j-santos/engineering-worker-pool",
        issue_number=60, request_id=request_id, correlation_id="host-routing-20261105",
        base_sha="1" * 40, **extra,
    )


def claim(store, name: str, role="builder", only_task_id=None):
    return store.claim_task(
        worker_id=name, role=role, correlation_id="route-claim-" + name,
        only_task_id=only_task_id,
    )


def test_pinned_host_builder_validator_and_exact_claim_fail_closed(pool):
    store, _ = pool
    worker(store, "builder-desktop", "DESKTOP-PDQK954")
    worker(store, "builder-noteri", "NOTERI")
    worker(store, "validator-desktop", "DESKTOP-PDQK954", role="validator")
    worker(store, "validator-noteri", "NOTERI", role="validator")
    created, fresh = task(
        store, execution_mode="pinned_host", target_host="DESKTOP-PDQK954"
    )
    assert fresh is True
    assert created["target_host"] == "DESKTOP-PDQK954"
    assert created["execution_mode"] == "pinned_host"

    assert claim(store, "builder-noteri")[0] is None
    assert claim(store, "builder-noteri", only_task_id=created["task_id"])[0] is None
    selected, lease = claim(store, "builder-desktop", only_task_id=created["task_id"])
    assert selected["task_id"] == created["task_id"] and lease is not None
    store.start_task(task_id=created["task_id"], worker_id="builder-desktop",
                     lease_token=lease.lease_token, correlation_id="route-start")
    store.submit_for_validation(
        task_id=created["task_id"], worker_id="builder-desktop",
        lease_token=lease.lease_token, produced_sha="b" * 40,
        correlation_id="route-handoff",
    )
    assert claim(store, "validator-noteri", role="validator")[0] is None
    validated, val_lease = claim(
        store, "validator-desktop", role="validator",
        only_task_id=created["task_id"],
    )
    assert validated["produced_sha"] == "b" * 40 and val_lease is not None
    done = store.complete_task(
        task_id=created["task_id"], worker_id="validator-desktop",
        lease_token=val_lease.lease_token, correlation_id="route-complete",
    )
    assert done["state"] == "completed"
    assert store.get_task(created["task_id"])["target_host"] == "DESKTOP-PDQK954"


@pytest.mark.parametrize("mode,host", [
    ("unknown", None), ("pinned_host", None),
    ("pinned_host", "x/other"), ("pinned_host", "x\\other"),
    ("eligible_pool", "NOTERI"), ("eligible_pool", ""),
])
def test_invalid_route_is_rejected_without_enqueue(pool, mode, host):
    store, _ = pool
    with pytest.raises(ValueError):
        task(store, execution_mode=mode, target_host=host)
    assert store.snapshot()["tasks"] == []


def test_replay_preserves_route_and_rejects_target_change(pool):
    store, _ = pool
    original, created = task(store, execution_mode="pinned_host", target_host="NOTERI")
    replay, created_again = task(store, execution_mode="pinned_host", target_host="NOTERI")
    assert created and not created_again and replay["task_id"] == original["task_id"]
    with pytest.raises(ConflictError, match="execution_route divergente"):
        task(store, execution_mode="pinned_host", target_host="DESKTOP-PDQK954")
    with pytest.raises(ConflictError, match="execution_route divergente"):
        task(store, execution_mode="eligible_pool")
    reread = store.get_task(original["task_id"])
    assert reread["target_host"] == "NOTERI" and reread["state"] == "queued"
    assert len(store.snapshot()["tasks"]) == 1


def test_legacy_pool_default_allows_other_host(pool):
    store, _ = pool
    worker(store, "builder-any", "NOTERI")
    queued, created = task(store)
    assert created and queued["execution_mode"] == "eligible_pool"
    assert queued["target_host"] is None
    claimed, lease = claim(store, "builder-any")
    assert lease is not None and claimed["task_id"] == queued["task_id"]


def test_watchdog_does_not_reroute_pinned_task_outside_host(pool):
    store, clock = pool
    worker(store, "desktop-a", "DESKTOP-PDQK954")
    worker(store, "noteri-b", "NOTERI")
    queued, _ = task(store, execution_mode="pinned_host", target_host="DESKTOP-PDQK954")
    selected, lease = claim(store, "desktop-a")
    assert selected and lease
    store.start_task(task_id=queued["task_id"], worker_id="desktop-a",
                     lease_token=lease.lease_token, correlation_id="route-start")
    clock.advance(31)
    result = store.recover_stalled_tasks()
    assert result["blocked"] == 1 and result["rerouted"] == 0
    blocked = store.get_task(queued["task_id"])
    assert blocked["state"] == "blocked"
    assert blocked["leased_by"] is None
    assert claim(store, "noteri-b")[0] is None


def test_watchdog_allows_alternative_on_same_host(pool):
    store, clock = pool
    worker(store, "desktop-a", "DESKTOP-PDQK954")
    worker(store, "desktop-b", "DESKTOP-PDQK954")
    queued, _ = task(store, execution_mode="pinned_host", target_host="DESKTOP-PDQK954")
    selected, lease = claim(store, "desktop-a")
    assert selected and lease
    store.start_task(task_id=queued["task_id"], worker_id="desktop-a",
                     lease_token=lease.lease_token, correlation_id="route-start")
    clock.advance(31)
    result = store.recover_stalled_tasks()
    assert result["rerouted"] == 1 and result["blocked"] == 0
    recovered, recovered_lease = claim(store, "desktop-b")
    assert recovered_lease is not None and recovered["task_id"] == queued["task_id"]


def test_existing_sqlite_schema_adds_optional_route_fields(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, repository TEXT NOT NULL,
            issue_number INTEGER NOT NULL, request_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL, priority INTEGER NOT NULL,
            branch TEXT NOT NULL, workspace_key TEXT NOT NULL,
            base_sha TEXT, produced_sha TEXT,
            builder_worker_id TEXT, validator_worker_id TEXT,
            leased_by TEXT, lease_token TEXT, lease_expires_at TEXT,
            lease_generation INTEGER NOT NULL DEFAULT 0,
            attempt_id TEXT, fencing_required INTEGER NOT NULL DEFAULT 0,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL, blocked_reason TEXT,
            last_error TEXT, correlation_id TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            last_material_progress_at TEXT NOT NULL, completed_at TEXT
        )""")
    store = WorkerPoolStore(path)
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
    assert {"execution_mode", "target_host"}.issubset(columns)
    queued, created = task(store, request_id="migrated")
    assert created and queued["execution_mode"] == "eligible_pool"


class LegacyPostgresV1Replica(WorkerPoolStore):
    """Exercise the shared v1 SQL surface without touching PostgreSQL."""

    supports_task_routing = False


def test_legacy_authority_schema_keeps_pooled_builder_validator_and_snapshot(tmp_path):
    path = tmp_path / "pg-v1-replica.db"
    clock = Clock()
    pool = LegacyPostgresV1Replica(
        path, clock=clock, expected_rules_sha="a" * 40,
        heartbeat_ttl_seconds=90, progress_stall_seconds=30,
    )
    # Reproduce PostgreSQL v1: the canonical v1 task table has NO routing columns.
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE tasks DROP COLUMN target_host")
        db.execute("ALTER TABLE tasks DROP COLUMN execution_mode")
        assert not {"execution_mode", "target_host"} & {
            row[1] for row in db.execute("PRAGMA table_info(tasks)")
        }
    worker(pool, "old-builder", "HOST-A")
    worker(pool, "old-validator", "HOST-B", role="validator")
    queued, created = task(pool, request_id="pg-legacy")
    assert created and queued["execution_mode"] == "eligible_pool"
    assert queued["target_host"] is None
    replay, created_again = task(pool, request_id="pg-legacy")
    assert not created_again and replay["task_id"] == queued["task_id"]
    with pytest.raises(ConflictError, match="versioned_migration"):
        task(pool, request_id="pg-pinned-denied",
             execution_mode="pinned_host", target_host="HOST-A")
    assert pool.get_task(queued["task_id"])["execution_mode"] == "eligible_pool"
    started, lease = claim(pool, "old-builder")
    assert started["task_id"] == queued["task_id"] and lease is not None
    pool.start_task(task_id=queued["task_id"], worker_id="old-builder",
                    lease_token=lease.lease_token, correlation_id="v1-start")
    pool.submit_for_validation(
        task_id=queued["task_id"], worker_id="old-builder",
        lease_token=lease.lease_token, produced_sha="b" * 40,
        correlation_id="v1-handoff",
    )
    validated, validator_lease = claim(pool, "old-validator", role="validator")
    assert validated["task_id"] == queued["task_id"] and validator_lease is not None
    completed = pool.complete_task(
        task_id=queued["task_id"], worker_id="old-validator",
        lease_token=validator_lease.lease_token, correlation_id="v1-complete",
    )
    assert completed["state"] == "completed"
    assert completed["execution_mode"] == "eligible_pool"
    snap = pool.snapshot()
    assert snap["queue"]["completed"] == 1
    assert snap["queue"]["queued"] == 0


def test_legacy_authority_watchdog_reassigns_any_eligible_host(tmp_path):
    path = tmp_path / "pg-v1-watchdog.db"
    clock = Clock()
    pool = LegacyPostgresV1Replica(
        path, clock=clock, expected_rules_sha="a" * 40,
        heartbeat_ttl_seconds=90, progress_stall_seconds=30,
        default_lease_seconds=120,
    )
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE tasks DROP COLUMN target_host")
        db.execute("ALTER TABLE tasks DROP COLUMN execution_mode")
    worker(pool, "old-a", "HOST-A")
    worker(pool, "old-b", "HOST-B")
    queued, _ = task(pool, request_id="pg-v1-watchdog")
    selected, lease = claim(pool, "old-a")
    assert selected and lease
    pool.start_task(task_id=queued["task_id"], worker_id="old-a",
                    lease_token=lease.lease_token, correlation_id="v1-start")
    clock.advance(31)
    result = pool.recover_stalled_tasks()
    assert result["rerouted"] == 1 and result["blocked"] == 0
    after, new_lease = claim(pool, "old-b")
    assert after["task_id"] == queued["task_id"] and new_lease is not None


def test_pinned_route_rejects_unready_and_study_hosts(pool):
    store, _ = pool
    worker(store, "desktop-study", "DESKTOP-PDQK954")
    store.heartbeat_worker("desktop-study", correlation_id="study", profile="ESTUDO")
    worker(store, "noteri-normal", "NOTERI")
    queued, _ = task(store, request_id="study-rejection",
                     execution_mode="pinned_host", target_host="DESKTOP-PDQK954")
    assert claim(store, "noteri-normal")[0] is None
    with pytest.raises(ConflictError, match="profile ESTUDO"):
        claim(store, "desktop-study", only_task_id=queued["task_id"])
    assert store.get_task(queued["task_id"])["state"] == "queued"


def test_postgres_v1_manifest_remains_signed_and_routing_opted_out():
    from app.postgres_authority import PostgresWorkerPoolStore, SCHEMA_VERSION, _MIGRATION

    assert SCHEMA_VERSION == 1
    assert _MIGRATION.name == "postgres_authority_v1.sql"
    assert PostgresWorkerPoolStore.supports_task_routing is False
    source = _MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE tasks(" in source
    assert "execution_mode" not in source
    assert "target_host" not in source
