"""Real PostgreSQL contract gate: invoke this file explicitly.

No skip/fake/PostgreSQL container fallback. Requires an already initialized,
fenced, isolated pilot authority and an owner-only runtime URL file. This suite
creates unique synthetic task/worker/repository IDs, never schemas or a second
queue. Run serially before the native continuity harness: reconciliation is
global to the existing authority by design. No production authority is eligible.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.postgres_authority import (
    AuthorityUnavailable, PostgresAuthority,
    PostgresWorkerPoolStore, PostgresWorkOrchestrator,
)
from app.reconcile import ReconciliationController
from app.store import ConflictError, parse_iso
from app.task_diagnostics import read_task_diagnostics

ENV = ("EWP_TEST_POSTGRES_URL_FILE", "EWP_TEST_POSTGRES_SCHEMA",
       "EWP_TEST_POSTGRES_AUTHORITY_ID")


def connect():
    missing = [name for name in ENV if not os.environ.get(name)]
    if missing:
        pytest.fail("real_postgres_gate_configuration_missing: " + ",".join(missing))
    return PostgresAuthority.from_file(
        Path(os.environ[ENV[0]]), schema=os.environ[ENV[1]],
        authority_id=os.environ[ENV[2]])


@pytest.fixture
def scope():
    authority = connect()
    tag = uuid.uuid4().hex[:16]
    value = {
        "authority": authority,
        "tag": tag,
        "repository": "example/pg-contract-" + tag,
        "workers": [],
    }
    pool = PostgresWorkerPoolStore(
        authority, default_max_attempts=8, heartbeat_ttl_seconds=120,
        default_lease_seconds=30, progress_stall_seconds=300)
    if not pool.fenced_cutover_status()["enabled"]:
        pytest.fail("real_postgres_gate_requires_operator_fenced_attestation")
    value["pool"] = pool
    try:
        yield value
    finally:
        # Only this scenario's lane changes; retain task/attempt evidence.
        pool.configure_repository(repository=value["repository"], enabled=False)


def independent(scope):
    return PostgresWorkerPoolStore(
        connect(), default_max_attempts=8, heartbeat_ttl_seconds=120,
        default_lease_seconds=30, progress_stall_seconds=300)


def register(scope, suffix, role="builder", pool=None):
    pool = pool or scope["pool"]
    worker = "pg-" + scope["tag"] + "-" + suffix
    pool.register_worker(
        worker_id=worker, host="native-postgres-contract-" + suffix,
        role=role, profile="NORMAL", correlation_id="contract-register",
        gateway_ok=True, state_validated=True)
    scope["workers"].append(worker)
    pool.replace_worker_affinities(worker, [scope["repository"]])
    return worker


def enqueue(scope, number=1):
    task, created = scope["pool"].enqueue_task(
        repository=scope["repository"], issue_number=number,
        request_id=scope["tag"] + "-" + str(number),
        correlation_id="contract-enqueue", base_sha="1" * 40,
        max_attempts=8, fencing_required=True)
    assert created is True
    return task["task_id"]


def claim(pool, task, worker, role="builder", seconds=30):
    return pool.claim_task(
        worker_id=worker, role=role, correlation_id="contract-claim",
        only_task_id=task, fencing_required=True, lease_seconds=seconds)


def fence(lease):
    return {"lease_token": lease.lease_token,
            "lease_generation": lease.lease_generation,
            "attempt_id": lease.attempt_id}


def checkpoint(pool, task, worker, lease, identifier="cp-1", **extra):
    return pool.record_checkpoint(
        task_id=task, worker_id=worker, correlation_id="contract-checkpoint",
        checkpoint_id=identifier, sequence=1, stage="analysis",
        next_action="continue", **fence(lease), **extra)


def wait_expired(authority, expires_at, maximum=12):
    deadline = time.monotonic() + maximum
    expires = parse_iso(expires_at)
    while authority.clock() <= expires:
        assert time.monotonic() < deadline, "lease_expiry_not_observed"
        time.sleep(0.1)


def test_real_runtime_requires_same_persisted_identity(scope):
    first = scope["authority"].verify_schema()
    second = connect().verify_schema()
    assert first["authority_id"] == second["authority_id"]
    assert first["schema_sha256"] == second["schema_sha256"]
    with scope["authority"].transaction() as db:
        assert db.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"] == "read committed"
        assert db.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "off"
    with scope["authority"].read_connection() as db:
        assert db.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"] == "repeatable read"
        assert db.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
        assert db.execute(
            "SELECT has_schema_privilege(current_user, ?, 'CREATE') AS can_create",
            (scope["authority"].schema,)).fetchone()["can_create"] is False
    wrong = PostgresAuthority.from_file(
        Path(os.environ[ENV[0]]), schema=os.environ[ENV[1]],
        authority_id=str(uuid.uuid4()))
    assert wrong.lock_key == scope["authority"].lock_key
    with pytest.raises(AuthorityUnavailable, match="authority_identity_mismatch"):
        wrong.verify_schema()


def test_real_work_facade_replay_reads_same_canonical_task(scope):
    authority = scope["authority"]
    pool = scope["pool"]
    work_a = PostgresWorkOrchestrator(authority, pool)
    # Work v1 retains its existing legacy-task admission semantics.
    kwargs = dict(
        repository=scope["repository"], issue_number=101,
        request_id=scope["tag"], correlation_id="contract-work",
        base_sha="1" * 40)
    first, created = work_a.create_work(**kwargs)
    assert created is True
    remote = independent(scope)
    work_b = PostgresWorkOrchestrator(remote.authority, remote)
    replay, created = work_b.create_work(**kwargs)
    assert created is False
    assert replay["work_id"] == first["work_id"]
    assert replay["task_id"] == first["task_id"]
    with remote.authority.read_connection() as db:
        count = db.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE task_id=?",
            (first["task_id"],)).fetchone()["n"]
    assert count == 1
    assert work_b.get_work(first["work_id"])["phase"] == "queue"


def test_real_concurrent_exact_claim_has_one_winner_and_generation(scope):
    task = enqueue(scope)
    contenders = [(independent(scope), register(scope, "race-" + str(index)))
                  for index in range(6)]
    barrier = threading.Barrier(len(contenders))
    def contend(pair):
        pool, worker = pair
        barrier.wait(timeout=20)
        return claim(pool, task, worker)
    with ThreadPoolExecutor(max_workers=len(contenders)) as executor:
        results = list(executor.map(contend, contenders))
    winners = [lease for _row, lease in results if lease is not None]
    assert len(winners) == 1
    winner = winners[0]
    assert winner.lease_generation == 1
    with scope["authority"].read_connection() as db:
        attempts = db.execute(
            "SELECT COUNT(*) AS n FROM task_attempts WHERE task_id=?", (task,)).fetchone()["n"]
    assert attempts == 1
    assert independent(scope).get_task(task)["lease_generation"] == 1
    scope["pool"].fail_task(
        task_id=task, worker_id=winner.worker_id, correlation_id="contract-release",
        reason="synthetic_contract_complete", **fence(winner))


def test_real_lane_capacity_and_fifo_survive_independent_connections(scope):
    first, second = enqueue(scope, 1), enqueue(scope, 2)
    a = register(scope, "lane-a")
    b = register(scope, "lane-b")
    pool_a, pool_b = independent(scope), independent(scope)
    pool_a.configure_repository(repository=scope["repository"], max_in_flight=1)
    # Make time/priority ties exact, so this exercises the explicit FIFO column.
    with scope["authority"].transaction() as db:
        db.execute("UPDATE tasks SET created_at=? WHERE task_id IN (?,?)",
                   ("2026-01-01T00:00:00Z", first, second))
    got, lease = pool_a.claim_task(
        worker_id=a, role="builder", correlation_id="contract-fifo", fencing_required=True)
    assert got["task_id"] == first
    assert "__pool_fifo" not in got
    assert claim(pool_b, second, b) == (None, None)
    pool_a.fail_task(
        task_id=first, worker_id=a, correlation_id="contract-release",
        reason="synthetic_contract_complete", **fence(lease))


def test_real_lease_clock_is_resolved_after_lock_wait(scope):
    task = enqueue(scope)
    worker = register(scope, "clock")
    remote = independent(scope)
    entered = threading.Event()
    def delayed_claim():
        entered.set()
        return claim(remote, task, worker, seconds=5)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with scope["authority"].transaction():
            future = executor.submit(delayed_claim)
            assert entered.wait(timeout=5)
            # Longer than the requested lease; a pre-lock timestamp is invalid.
            time.sleep(5.3)
            assert not future.done()
        _row, lease = future.result(timeout=20)
    assert lease is not None
    remote.start_task(
        task_id=task, worker_id=worker, correlation_id="contract-start-after-wait",
        **fence(lease))
    remote.fail_task(
        task_id=task, worker_id=worker, correlation_id="contract-release",
        reason="synthetic_contract_complete", **fence(lease))


def test_real_expiry_during_lock_wait_rejects_renewal(scope):
    task = enqueue(scope)
    worker = register(scope, "renew")
    pool = independent(scope)
    _row, lease = claim(pool, task, worker, seconds=5)
    entered = threading.Event()
    def delayed_renew():
        entered.set()
        return pool.renew_lease(
            task_id=task, worker_id=worker, correlation_id="contract-renew",
            lease_seconds=30, **fence(lease))
    with ThreadPoolExecutor(max_workers=1) as executor:
        with scope["authority"].transaction():
            future = executor.submit(delayed_renew)
            assert entered.wait(timeout=5)
            time.sleep(5.3)
            assert not future.done()
        with pytest.raises(ConflictError, match="lease expirado"):
            future.result(timeout=20)
    assert independent(scope).get_task(task)["lease_expires_at"] == lease.lease_expires_at
    asyncio.run(ReconciliationController(scope["pool"]).run_once())


def test_real_checkpoint_survives_facade_loss_and_rejects_stale_fence(scope):
    task = enqueue(scope)
    a, b, v = register(scope, "a"), register(scope, "b"), register(scope, "v", "validator")
    pool_a = independent(scope)
    _row, lease_a = claim(pool_a, task, a)
    pool_a.start_task(task_id=task, worker_id=a, correlation_id="contract-start-a",
                      **fence(lease_a))
    saved, created = checkpoint(pool_a, task, a, lease_a)
    assert created is True
    replay, created = checkpoint(pool_a, task, a, lease_a)
    assert created is False
    assert replay["content_digest"] == saved["content_digest"]
    with pytest.raises(ConflictError, match="checkpoint divergente"):
        pool_a.record_checkpoint(
            task_id=task, worker_id=a, correlation_id="contract-divergent",
            checkpoint_id="cp-1", sequence=1, stage="analysis",
            next_action="different", **fence(lease_a))
    # Finish remote setup under the normal lease before requesting a short,
    # real expiry. Regional network latency must not expire the setup itself.
    renewed = pool_a.renew_lease(
        task_id=task, worker_id=a, correlation_id="contract-expiry-a",
        lease_seconds=5, **fence(lease_a))
    assert renewed["lease_generation"] == lease_a.lease_generation
    assert renewed["attempt_id"] == lease_a.attempt_id
    del pool_a  # No database file or state is transported to the next facade.
    wait_expired(scope["authority"], renewed["lease_expires_at"])
    controller = ReconciliationController(independent(scope))
    result = asyncio.run(controller.run_once())
    assert result is not None
    pool_b = independent(scope)
    _row, lease_b = claim(pool_b, task, b)
    assert lease_b.lease_generation == 2
    assert lease_b.attempt_id != lease_a.attempt_id
    assert pool_b.get_latest_checkpoint(task)["content_digest"] == saved["content_digest"]
    pool_b.start_task(task_id=task, worker_id=b, correlation_id="contract-start-b",
                      **fence(lease_b))
    for old in (
        {"lease_generation": lease_a.lease_generation},
        {"attempt_id": lease_a.attempt_id},
    ):
        metadata = dict(fence(lease_b), **old)
        with pytest.raises(ConflictError):
            pool_b.record_checkpoint(
                task_id=task, worker_id=b, correlation_id="contract-stale",
                checkpoint_id="stale", sequence=1, stage="analysis",
                next_action="continue", **metadata)
    assert pool_b.get_latest_checkpoint(task)["content_digest"] == saved["content_digest"]
    _current, created = checkpoint(pool_b, task, b, lease_b, "cp-2")
    assert created is True
    pool_b.submit_for_validation(
        task_id=task, worker_id=b, produced_sha="2" * 40,
        correlation_id="contract-handoff", **fence(lease_b))
    validator = independent(scope)
    _row, lease_v = claim(validator, task, v, role="validator")
    assert lease_v.lease_generation == 3
    completed = validator.complete_task(
        task_id=task, worker_id=v, correlation_id="contract-complete", **fence(lease_v))
    assert completed["state"] == "completed"
    readback = independent(scope).get_task(task)
    assert readback["state"] == "completed"
    assert readback["produced_sha"] == "2" * 40
    with scope["authority"].read_connection() as db:
        count = db.execute("SELECT COUNT(*) AS n FROM tasks WHERE task_id=?",
                           (task,)).fetchone()["n"]
    assert count == 1


def test_real_unknown_effect_does_not_become_automatic_retry(scope):
    task = enqueue(scope)
    a, b = register(scope, "unknown-a"), register(scope, "unknown-b")
    pool = scope["pool"]
    _row, lease = claim(pool, task, a)
    pool.start_task(task_id=task, worker_id=a, correlation_id="contract-start", **fence(lease))
    pool.record_checkpoint(
        task_id=task, worker_id=a, correlation_id="contract-unknown",
        checkpoint_id="unknown", sequence=1, stage="publish",
        next_action="reconcile destination", effect_id="synthetic-effect",
        effect_state="unknown", **fence(lease))
    renewed = pool.renew_lease(
        task_id=task, worker_id=a, correlation_id="contract-expiry-unknown",
        lease_seconds=5, **fence(lease))
    assert renewed["lease_generation"] == lease.lease_generation
    assert renewed["attempt_id"] == lease.attempt_id
    wait_expired(scope["authority"], renewed["lease_expires_at"])
    asyncio.run(ReconciliationController(independent(scope)).run_once())
    observed = independent(scope).get_task(task)
    assert observed["state"] == "blocked"
    assert observed["blocked_reason"] == "effect_reconciliation_required"
    assert claim(independent(scope), task, b) == (None, None)


def test_real_database_guard_rejects_literal_legacy_claim(scope):
    task = enqueue(scope)
    worker = register(scope, "literal")
    with pytest.raises(ConflictError, match="authority_integrity_conflict"):
        with scope["authority"].transaction() as db:
            db.execute(
                "UPDATE tasks SET state='leased',leased_by=?,lease_token=?,"
                "lease_expires_at=? WHERE task_id=? AND leased_by IS NULL",
                (worker, "synthetic-invalid-token", "2099-01-01T00:00:00Z", task))
    observed = independent(scope).get_task(task)
    assert observed["state"] == "queued"
    assert observed["lease_generation"] == 0
    assert observed["attempt_id"] is None


def test_real_read_only_diagnostics_cannot_mutate_authority(scope):
    task = enqueue(scope)
    register(scope, "diagnostic")
    pool = scope["pool"]
    report = read_task_diagnostics(
        Path("unused-postgres-path"), task,
        expected_rules_sha=None, heartbeat_ttl_seconds=120,
        progress_stall_seconds=300, clock=scope["authority"].clock,
        _connection_factory=scope["authority"].read_connection,
        _read_mode="postgres_read_only")
    assert report["read_mode"] == "postgres_read_only"
    assert report["claim_performed"] is False
    assert report["reconciliation_requested"] is False
    assert report["task"]["task_id"] == task
    with pytest.raises(AuthorityUnavailable):
        with scope["authority"].read_connection() as db:
            db.execute("UPDATE tasks SET state='failed' WHERE task_id=?", (task,))
    assert independent(scope).get_task(task)["state"] == "queued"
