from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import multiprocessing
import os
import sqlite3
import threading
from typing import Any

import pytest

from app.store import ConflictError, WorkerPoolStore


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class SharedProcessClock:
    """Clock backed by a multiprocessing.Value for deterministic lease expiry."""

    def __init__(self, epoch_seconds: Any) -> None:
        self.epoch_seconds = epoch_seconds

    def __call__(self) -> datetime:
        with self.epoch_seconds.get_lock():
            value = int(self.epoch_seconds.value)
        return datetime.fromtimestamp(value, tz=timezone.utc)

    def advance(self, seconds: int) -> None:
        with self.epoch_seconds.get_lock():
            self.epoch_seconds.value += seconds


class FakeFencedSink:
    """Test destination that applies the generation check at the effect boundary."""

    def __init__(self) -> None:
        self.generation_by_task: dict[str, int] = {}
        self.effects: dict[str, str] = {}
        self.applied_calls: list[tuple[str, int, str, str]] = []

    def advance_fence(self, task_id: str, lease_generation: int) -> None:
        self.generation_by_task[task_id] = max(
            lease_generation, self.generation_by_task.get(task_id, 0)
        )

    def apply(
        self, task_id: str, lease_generation: int, operation_id: str, value: str
    ) -> None:
        if lease_generation != self.generation_by_task.get(task_id):
            raise ConflictError("stale destination fence")
        previous = self.effects.get(operation_id)
        if previous is not None and previous != value:
            raise ConflictError("divergent effect replay")
        if previous is None:
            self.applied_calls.append(
                (task_id, lease_generation, operation_id, value)
            )
        self.effects[operation_id] = value


def process_store(db_path: str, clock_epoch: Any | None = None) -> WorkerPoolStore:
    options = {}
    if clock_epoch is not None:
        options["clock"] = SharedProcessClock(clock_epoch)
    store = WorkerPoolStore(
        Path(db_path),
        heartbeat_ttl_seconds=30,
        default_lease_seconds=1,
        default_max_attempts=6,
        progress_stall_seconds=30,
        **options,
    )
    store.enable_fenced_cutover(
        correlation_id="multiprocess-exclusive-cutover",
        legacy_claimers_drained=True,
        shared_queue_exclusive=True,
    )
    return store


def initialize_external_sqlite_sink(db_path: Path) -> None:
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """CREATE TABLE fences(
                 task_id TEXT PRIMARY KEY,
                 max_generation INTEGER NOT NULL
               );
               CREATE TABLE effects(
                 task_id TEXT NOT NULL,
                 operation_id TEXT NOT NULL,
                 lease_generation INTEGER NOT NULL,
                 value TEXT NOT NULL,
                 PRIMARY KEY(task_id,operation_id)
               );"""
        )


def advance_external_sqlite_fence(
    db_path: str, task_id: str, lease_generation: int,
) -> None:
    with sqlite3.connect(db_path, timeout=10) as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT max_generation FROM fences WHERE task_id=?", (task_id,)
        ).fetchone()
        if current is None:
            if lease_generation != 1:
                raise ConflictError("external sqlite fence must start at generation one")
            db.execute(
                "INSERT INTO fences(task_id,max_generation) VALUES(?,?)",
                (task_id, lease_generation),
            )
        else:
            current_generation = int(current[0])
            if lease_generation < current_generation:
                raise ConflictError("stale external sqlite fence advance")
            if lease_generation > current_generation + 1:
                raise ConflictError("external sqlite fence generation gap")
            if lease_generation == current_generation + 1:
                updated = db.execute(
                    """UPDATE fences SET max_generation=?
                       WHERE task_id=? AND max_generation=?""",
                    (lease_generation, task_id, current_generation),
                )
                if updated.rowcount != 1:
                    raise ConflictError("external sqlite fence CAS failed")


def apply_external_sqlite_effect(
    db_path: str, task_id: str, lease_generation: int,
    operation_id: str, value: str,
) -> bool:
    with sqlite3.connect(db_path, timeout=10) as db:
        db.execute("BEGIN IMMEDIATE")
        fence = db.execute(
            "SELECT max_generation FROM fences WHERE task_id=?", (task_id,)
        ).fetchone()
        if fence is None or int(fence[0]) != lease_generation:
            raise ConflictError("stale external sqlite fence")
        existing = db.execute(
            """SELECT lease_generation,value FROM effects
               WHERE task_id=? AND operation_id=?""",
            (task_id, operation_id),
        ).fetchone()
        if existing is not None:
            if int(existing[0]) != lease_generation or str(existing[1]) != value:
                raise ConflictError("divergent external sqlite effect")
            return False
        db.execute(
            """INSERT INTO effects(
                 task_id,operation_id,lease_generation,value
               ) VALUES(?,?,?,?)""",
            (task_id, operation_id, lease_generation, value),
        )
    return True


def read_external_sqlite_effect(
    db_path: str, task_id: str, operation_id: str,
) -> tuple[int, str] | None:
    with sqlite3.connect(db_path, timeout=10) as db:
        row = db.execute(
            """SELECT lease_generation,value FROM effects
               WHERE task_id=? AND operation_id=?""",
            (task_id, operation_id),
        ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1])


def process_actor_a(
    pool_db: str,
    sink_db: str,
    task_id: str,
    clock_epoch: Any,
    checkpointed: Any,
    allow_late_attempt: Any,
    stale_attempted: Any,
    allow_late_complete: Any,
    late_complete_attempted: Any,
    results: Any,
) -> None:
    message: dict[str, Any] = {"actor": "A", "pid": os.getpid()}
    try:
        store = process_store(pool_db, clock_epoch)
        task, lease = store.claim_task(
            worker_id="process-builder-a", role="builder",
            correlation_id="process-a-claim", only_task_id=task_id,
            fencing_required=True,
        )
        if task is None or lease is None:
            raise AssertionError("A did not claim the canonical task")
        store.start_task(
            task_id=task_id, worker_id="process-builder-a",
            correlation_id="process-a-start", **lease_args(lease)
        )
        advance_external_sqlite_fence(sink_db, task_id, lease.lease_generation)
        store.record_checkpoint(
            task_id=task_id, worker_id="process-builder-a",
            correlation_id="process-a-checkpoint",
            checkpoint_id="process-a-portable-checkpoint", sequence=1,
            stage="code-ready", next_action="alternative builder resumes",
            artifact_kind="commit", artifact_ref="shared://commit/process-a",
            artifact_sha256="a" * 64, **lease_args(lease)
        )
        checkpointed.set()
        if not allow_late_attempt.wait(timeout=20):
            raise AssertionError("A was not released for its stale attempt")

        try:
            apply_external_sqlite_effect(
                sink_db, task_id, lease.lease_generation,
                "publish", "stale-a",
            )
        except ConflictError as exc:
            message["late_effect_rejected"] = True
            message["late_effect_error"] = str(exc)
        else:
            message["late_effect_rejected"] = False
        try:
            store.fail_task(
                task_id=task_id, worker_id="process-builder-a",
                reason="stale A must not fail B's active task",
                correlation_id="process-a-late-pool-mutation", **lease_args(lease)
            )
        except ConflictError as exc:
            message["late_pool_mutation_rejected"] = True
            message["late_pool_mutation_error"] = str(exc)
        else:
            message["late_pool_mutation_rejected"] = False
        with sqlite3.connect(sink_db) as db:
            message["observed_sink_generation"] = int(
                db.execute(
                    "SELECT max_generation FROM fences WHERE task_id=?", (task_id,)
                ).fetchone()[0]
            )
        stale_attempted.set()
        if not allow_late_complete.wait(timeout=10):
            raise AssertionError("A was not released against validator C")
        try:
            store.complete_task(
                task_id=task_id, worker_id="process-builder-a",
                correlation_id="process-a-late-complete", **lease_args(lease)
            )
        except ConflictError as exc:
            message["late_complete_rejected"] = True
            message["late_complete_error"] = str(exc)
        else:
            message["late_complete_rejected"] = False
        late_complete_attempted.set()
        message.update(task_id=task["task_id"], generation=lease.lease_generation)
    except BaseException as exc:  # pragma: no cover - reported to parent process
        message["error"] = repr(exc)
    finally:
        checkpointed.set()
        stale_attempted.set()
        late_complete_attempted.set()
        results.put(message)


def process_reconciler(
    pool_db: str, clock_epoch: Any, checkpointed: Any,
    recovered: Any, results: Any,
) -> None:
    message: dict[str, Any] = {"actor": "reconciler", "pid": os.getpid()}
    try:
        if not checkpointed.wait(timeout=10):
            raise AssertionError("A did not checkpoint")
        SharedProcessClock(clock_epoch).advance(2)
        store = process_store(pool_db, clock_epoch)
        outcome = store.reconcile()
        if outcome["expired_leases_recovered"] != 1:
            raise AssertionError("reconciler did not recover A's expired lease")
        message["outcome"] = outcome
        recovered.set()
    except BaseException as exc:  # pragma: no cover - reported to parent process
        message["error"] = repr(exc)
    finally:
        recovered.set()
        results.put(message)


def process_actor_b(
    pool_db: str,
    sink_db: str,
    task_id: str,
    clock_epoch: Any,
    recovered: Any,
    fence_advanced: Any,
    stale_attempted: Any,
    handed_off: Any,
    results: Any,
) -> None:
    message: dict[str, Any] = {"actor": "B", "pid": os.getpid()}
    try:
        if not recovered.wait(timeout=15):
            raise AssertionError("reconciler did not release the task")
        store = process_store(pool_db, clock_epoch)
        task, lease = store.claim_task(
            worker_id="process-builder-b", role="builder",
            correlation_id="process-b-claim", only_task_id=task_id,
            fencing_required=True,
        )
        if task is None or lease is None:
            raise AssertionError("B did not claim the recovered canonical task")
        context = store.get_recovery_context(task_id)
        if context["latest_checkpoint"]["artifact_ref"] != "shared://commit/process-a":
            raise AssertionError("B did not receive A's portable checkpoint")
        store.start_task(
            task_id=task_id, worker_id="process-builder-b",
            correlation_id="process-b-start", **lease_args(lease)
        )
        store.record_checkpoint(
            task_id=task_id, worker_id="process-builder-b",
            correlation_id="process-b-effect-pending",
            checkpoint_id="process-b-effect-pending", sequence=1,
            stage="publish", next_action="apply fenced external effect",
            effect_id="publish", effect_state="pending", **lease_args(lease)
        )
        advance_external_sqlite_fence(sink_db, task_id, lease.lease_generation)
        fence_advanced.set()
        if not stale_attempted.wait(timeout=10):
            raise AssertionError("A did not exercise its stale generation")
        created = apply_external_sqlite_effect(
            sink_db, task_id, lease.lease_generation, "publish", "from-b"
        )
        store.record_checkpoint(
            task_id=task_id, worker_id="process-builder-b",
            correlation_id="process-b-effect-confirmed",
            checkpoint_id="process-b-effect-confirmed", sequence=2,
            stage="publish", next_action="independent validation",
            effect_id="publish", effect_state="confirmed",
            effect_result_ref="sqlite-sink://publish/from-b", **lease_args(lease)
        )
        store.submit_for_validation(
            task_id=task_id, worker_id="process-builder-b",
            produced_sha="b" * 40, correlation_id="process-b-handoff",
            **lease_args(lease)
        )
        message.update(
            task_id=task["task_id"], generation=lease.lease_generation,
            effect_created=created,
            checkpoint_ref=context["latest_checkpoint"]["artifact_ref"],
        )
        handed_off.set()
    except BaseException as exc:  # pragma: no cover - reported to parent process
        message["error"] = repr(exc)
    finally:
        fence_advanced.set()
        stale_attempted.set()
        handed_off.set()
        results.put(message)


def process_validator_c(
    pool_db: str, sink_db: str, task_id: str,
    clock_epoch: Any, handed_off: Any, validator_claimed: Any,
    late_complete_attempted: Any, completed: Any, results: Any,
) -> None:
    message: dict[str, Any] = {"actor": "C", "pid": os.getpid()}
    try:
        if not handed_off.wait(timeout=15):
            raise AssertionError("B did not hand off for validation")
        store = process_store(pool_db, clock_epoch)
        task, lease = store.claim_task(
            worker_id="process-validator-c", role="validator",
            correlation_id="process-c-claim", only_task_id=task_id,
            fencing_required=True,
        )
        if task is None or lease is None:
            raise AssertionError("C did not claim validation")
        observed_effect = read_external_sqlite_effect(sink_db, task_id, "publish")
        expected_effect = (lease.lease_generation - 1, "from-b")
        if observed_effect != expected_effect:
            raise AssertionError(
                f"independent sink readback mismatch: {observed_effect!r}"
            )
        validator_claimed.set()
        if not late_complete_attempted.wait(timeout=10):
            raise AssertionError("A did not exercise stale complete against C")
        advance_external_sqlite_fence(sink_db, task_id, lease.lease_generation)
        final = store.complete_task(
            task_id=task_id, worker_id="process-validator-c",
            correlation_id="process-c-complete", **lease_args(lease)
        )
        message.update(
            task_id=task["task_id"], generation=lease.lease_generation,
            state=final["state"], independent_effect_readback=observed_effect,
        )
        completed.set()
    except BaseException as exc:  # pragma: no cover - reported to parent process
        message["error"] = repr(exc)
    finally:
        validator_claimed.set()
        completed.set()
        results.put(message)


def register(store: WorkerPoolStore, worker_id: str, role: str) -> None:
    store.register_worker(
        worker_id=worker_id,
        host=worker_id,
        role=role,
        correlation_id=f"register-{worker_id}",
        profile="NORMAL",
        gateway_ok=True,
        state_validated=True,
    )


def make_store(tmp_path: Path, clock: MutableClock) -> WorkerPoolStore:
    store = WorkerPoolStore(
        tmp_path / "fenced-continuity.db",
        clock=clock,
        heartbeat_ttl_seconds=120,
        default_lease_seconds=10,
        default_max_attempts=6,
        progress_stall_seconds=300,
    )
    store.enable_fenced_cutover(
        correlation_id="test-exclusive-cutover",
        legacy_claimers_drained=True,
        shared_queue_exclusive=True,
    )
    return store


def enqueue_fenced(store: WorkerPoolStore, request_id: str = "fenced-pilot") -> dict:
    task, created = store.enqueue_task(
        repository="example/fenced-pilot",
        issue_number=1,
        request_id=request_id,
        correlation_id=f"enqueue-{request_id}",
        base_sha="1" * 40,
        fencing_required=True,
    )
    assert created is True
    return task


def test_fenced_admission_requires_persisted_exclusive_cutover(tmp_path: Path) -> None:
    db_path = tmp_path / "cutover-gate.db"
    store = WorkerPoolStore(db_path)
    assert store.fenced_cutover_status()["enabled"] is False

    with pytest.raises(ConflictError, match="fenced_cutover_not_enabled"):
        store.enqueue_task(
            repository="example/cutover", issue_number=1,
            request_id="blocked-before-cutover", correlation_id="blocked",
            base_sha="1" * 40, fencing_required=True,
        )
    with sqlite3.connect(db_path) as db:
        with pytest.raises(
            sqlite3.IntegrityError, match="fenced_cutover_not_enabled"
        ):
            db.execute(
                """INSERT INTO tasks(
                     task_id,repository,issue_number,request_id,idempotency_key,
                     state,priority,branch,workspace_key,base_sha,max_attempts,
                     correlation_id,created_at,updated_at,last_material_progress_at,
                     fencing_required)
                   VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,1)""",
                (
                    "cwp-" + "c" * 24, "example/cutover", 2, "direct-blocked",
                    "d" * 64, 100, "codex/direct-blocked", "cutover-direct",
                    "2" * 40, 3, "direct-blocked", "2026-10-19T12:00:00Z",
                    "2026-10-19T12:00:00Z", "2026-10-19T12:00:00Z",
                ),
            )

    legacy, created = store.enqueue_task(
        repository="example/cutover", issue_number=3,
        request_id="legacy-before-cutover", correlation_id="legacy",
        base_sha="3" * 40,
    )
    assert created is True
    assert legacy["fencing_required"] == 0
    with pytest.raises(ValueError, match="claimers antigos drenados"):
        store.enable_fenced_cutover(
            correlation_id="unsafe-cutover",
            legacy_claimers_drained=False,
            shared_queue_exclusive=True,
        )

    status = store.enable_fenced_cutover(
        correlation_id="exclusive-cutover",
        legacy_claimers_drained=True,
        shared_queue_exclusive=True,
    )
    assert status["enabled"] is True
    assert status["enabled_by_correlation_id"] == "exclusive-cutover"
    restarted = WorkerPoolStore(db_path)
    assert restarted.fenced_cutover_status()["enabled"] is True
    fenced, created = restarted.enqueue_task(
        repository="example/cutover", issue_number=4,
        request_id="allowed-after-cutover", correlation_id="allowed",
        base_sha="4" * 40, fencing_required=True,
    )
    assert created is True
    assert fenced["fencing_required"] == 1


def test_legacy_database_migrates_without_enabling_fencing(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as db:
        db.executescript(
            """
            CREATE TABLE tasks(
              task_id TEXT PRIMARY KEY, repository TEXT NOT NULL,
              issue_number INTEGER NOT NULL, request_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL UNIQUE,
              state TEXT NOT NULL, priority INTEGER NOT NULL, branch TEXT NOT NULL,
              workspace_key TEXT NOT NULL, base_sha TEXT, produced_sha TEXT,
              builder_worker_id TEXT, validator_worker_id TEXT, leased_by TEXT,
              lease_token TEXT, lease_expires_at TEXT,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              max_attempts INTEGER NOT NULL, blocked_reason TEXT, last_error TEXT,
              correlation_id TEXT NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, last_material_progress_at TEXT NOT NULL,
              completed_at TEXT
            );
            """
        )
        db.execute(
            """INSERT INTO tasks(
                 task_id,repository,issue_number,request_id,idempotency_key,state,
                 priority,branch,workspace_key,base_sha,attempt_count,max_attempts,
                 correlation_id,created_at,updated_at,last_material_progress_at)
               VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?)""",
            (
                "cwp-" + "a" * 24,
                "example/legacy",
                1,
                "legacy-request",
                "b" * 64,
                10,
                "codex/legacy",
                "worker-legacy",
                "1" * 40,
                0,
                3,
                "legacy-correlation",
                "2026-10-19T12:00:00Z",
                "2026-10-19T12:00:00Z",
                "2026-10-19T12:00:00Z",
            ),
        )

    store = WorkerPoolStore(db_path)
    migrated = store.get_task("cwp-" + "a" * 24)
    assert migrated["lease_generation"] == 0
    assert migrated["attempt_id"] is None
    assert migrated["fencing_required"] == 0
    with sqlite3.connect(db_path) as db:
        tables = {
            row[0]
            for row in db.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name IN (
                     'task_attempts','task_checkpoints','effect_reconciliations'
                   )"""
            )
        }
    assert tables == {
        "task_attempts", "task_checkpoints", "effect_reconciliations"
    }


def lease_args(lease) -> dict:
    return {
        "lease_token": lease.lease_token,
        "lease_generation": lease.lease_generation,
        "attempt_id": lease.attempt_id,
    }


def test_sqlite_guard_blocks_literal_old_builder_and_validator_claims(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = make_store(tmp_path, clock)
    register(store, "builder-new", "builder")
    register(store, "validator-new", "validator")
    task = enqueue_fenced(store, "mixed-version-guard")
    db_path = tmp_path / "fenced-continuity.db"
    old_claim_sql = """UPDATE tasks SET state=?,leased_by=?,lease_token=?,
      lease_expires_at=?,attempt_count=attempt_count+1,validator_worker_id=?,
      correlation_id=?,updated_at=?,last_material_progress_at=?
      WHERE task_id=? AND state=? AND leased_by IS NULL"""
    stamp = "2026-10-19T12:00:00Z"

    with sqlite3.connect(db_path) as db:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="fenced_claim_requires_generation_and_attempt",
        ):
            db.execute(
                old_claim_sql,
                (
                    "leased", "old-builder", "old-builder-token", stamp, None,
                    "old-builder-claim", stamp, stamp, task["task_id"], "queued",
                ),
            )
        db.rollback()
    assert store.get_task(task["task_id"])["state"] == "queued"

    claimed, builder_lease = store.claim_task(
        worker_id="builder-new", role="builder", correlation_id="new-builder-claim",
        only_task_id=task["task_id"], fencing_required=True,
    )
    assert claimed and builder_lease
    store.start_task(
        task_id=task["task_id"], worker_id="builder-new",
        correlation_id="new-builder-start", **lease_args(builder_lease)
    )
    store.submit_for_validation(
        task_id=task["task_id"], worker_id="builder-new",
        produced_sha="7" * 40, correlation_id="new-builder-handoff",
        **lease_args(builder_lease)
    )

    store = make_store(tmp_path, clock)
    with sqlite3.connect(db_path) as db:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="fenced_claim_requires_generation_and_attempt",
        ):
            db.execute(
                old_claim_sql,
                (
                    "validating", "old-validator", "old-validator-token", stamp,
                    "old-validator", "old-validator-claim", stamp, stamp,
                    task["task_id"], "validating",
                ),
            )
        db.rollback()
    assert store.get_task(task["task_id"])["leased_by"] is None

    validating, validator_lease = store.claim_task(
        worker_id="validator-new", role="validator",
        correlation_id="new-validator-claim", only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert validating and validator_lease
    assert validator_lease.lease_generation == 2

    legacy, created = store.enqueue_task(
        repository="example/legacy-mixed-version",
        issue_number=2,
        request_id="legacy-old-claim-positive",
        correlation_id="legacy-old-claim-positive",
        base_sha="8" * 40,
    )
    assert created is True
    with sqlite3.connect(db_path) as db:
        result = db.execute(
            old_claim_sql,
            (
                "leased", "old-legacy-builder", "old-legacy-token", stamp, None,
                "old-legacy-claim", stamp, stamp, legacy["task_id"], "queued",
            ),
        )
        assert result.rowcount == 1
    assert store.get_task(legacy["task_id"])["leased_by"] == "old-legacy-builder"


def test_same_canonical_task_moves_from_unavailable_a_to_b_and_completes_once(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = make_store(tmp_path, clock)
    register(store, "builder-a", "builder")
    register(store, "builder-b", "builder")
    register(store, "validator-c", "validator")
    task = enqueue_fenced(store)

    legacy_task, legacy_lease = store.claim_task(
        worker_id="builder-a", role="builder", correlation_id="legacy-claim"
    )
    assert legacy_task is None
    assert legacy_lease is None

    claimed_a, lease_a = store.claim_task(
        worker_id="builder-a",
        role="builder",
        correlation_id="claim-a",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert claimed_a and lease_a
    assert claimed_a["task_id"] == task["task_id"]
    assert lease_a.lease_generation == 1
    assert lease_a.attempt_id

    with pytest.raises(ConflictError, match="fence obrigatório"):
        store.start_task(
            task_id=task["task_id"],
            worker_id="builder-a",
            lease_token=lease_a.lease_token,
            correlation_id="start-a-without-fence",
        )

    store.start_task(
        task_id=task["task_id"],
        worker_id="builder-a",
        correlation_id="start-a",
        **lease_args(lease_a),
    )
    checkpoint, created = store.record_checkpoint(
        task_id=task["task_id"],
        worker_id="builder-a",
        correlation_id="checkpoint-a",
        checkpoint_id="builder-a-code-ready",
        sequence=1,
        stage="code-ready",
        next_action="resume on an eligible builder",
        artifact_kind="commit",
        artifact_ref="refs/heads/fenced-pilot",
        artifact_sha256="2" * 64,
        **lease_args(lease_a),
    )
    replay, replay_created = store.record_checkpoint(
        task_id=task["task_id"],
        worker_id="builder-a",
        correlation_id="checkpoint-a-replay",
        checkpoint_id="builder-a-code-ready",
        sequence=1,
        stage="code-ready",
        next_action="resume on an eligible builder",
        artifact_kind="commit",
        artifact_ref="refs/heads/fenced-pilot",
        artifact_sha256="2" * 64,
        **lease_args(lease_a),
    )
    assert created is True
    assert replay_created is False
    assert replay["content_digest"] == checkpoint["content_digest"]

    clock.advance(11)
    store.heartbeat_worker("builder-b", correlation_id="heartbeat-b")
    assert store.recover_expired_leases() == 1
    released = store.get_task(task["task_id"])
    assert released["state"] == "queued"
    assert released["leased_by"] is None
    store = make_store(tmp_path, clock)

    claimed_b, lease_b = store.claim_task(
        worker_id="builder-b",
        role="builder",
        correlation_id="claim-b",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert claimed_b and lease_b
    assert claimed_b["task_id"] == task["task_id"]
    assert lease_b.lease_generation == 2
    assert lease_b.attempt_id != lease_a.attempt_id
    portable_context = store.get_recovery_context(task["task_id"])
    assert portable_context["latest_checkpoint"]["checkpoint_id"] == (
        "builder-a-code-ready"
    )
    assert portable_context["latest_checkpoint"]["artifact_ref"] == (
        "refs/heads/fenced-pilot"
    )

    with pytest.raises(ConflictError):
        store.renew_lease(
            task_id=task["task_id"],
            worker_id="builder-a",
            correlation_id="late-renew-a",
            **lease_args(lease_a),
        )
    with pytest.raises(ConflictError):
        store.record_checkpoint(
            task_id=task["task_id"],
            worker_id="builder-a",
            correlation_id="late-checkpoint-a",
            checkpoint_id="late-a",
            sequence=2,
            stage="late-write",
            next_action="must be rejected",
            **lease_args(lease_a),
        )

    sink = FakeFencedSink()
    sink.advance_fence(task["task_id"], lease_b.lease_generation)
    with pytest.raises(ConflictError, match="stale destination fence"):
        sink.apply(task["task_id"], lease_a.lease_generation, "publish", "old")
    sink.apply(task["task_id"], lease_b.lease_generation, "publish", "new")

    store.start_task(
        task_id=task["task_id"],
        worker_id="builder-b",
        correlation_id="start-b",
        **lease_args(lease_b),
    )
    store.submit_for_validation(
        task_id=task["task_id"],
        worker_id="builder-b",
        correlation_id="handoff-b",
        produced_sha="3" * 40,
        **lease_args(lease_b),
    )
    validated, validation_lease = store.claim_task(
        worker_id="validator-c",
        role="validator",
        correlation_id="claim-validator",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert validated and validation_lease
    assert validated["task_id"] == task["task_id"]
    assert validation_lease.lease_generation == 3
    with pytest.raises(ConflictError):
        store.complete_task(
            task_id=task["task_id"],
            worker_id="builder-a",
            correlation_id="late-complete-a-generation-1",
            **lease_args(lease_a),
        )
    completed = store.complete_task(
        task_id=task["task_id"],
        worker_id="validator-c",
        correlation_id="complete-validator",
        **lease_args(validation_lease),
    )
    assert completed["state"] == "completed"
    assert completed["task_id"] == task["task_id"]
    assert sink.effects == {"publish": "new"}
    assert sink.applied_calls == [
        (task["task_id"], lease_b.lease_generation, "publish", "new")
    ]
    with pytest.raises(ConflictError):
        store.complete_task(
            task_id=task["task_id"],
            worker_id="validator-c",
            correlation_id="duplicate-complete-validator",
            **lease_args(validation_lease),
        )


def test_multiprocess_automatic_recovery_uses_external_sqlite_fence(
    tmp_path: Path,
) -> None:
    pool_db = tmp_path / "multiprocess-pool.db"
    sink_db = tmp_path / "external-fenced-sink.db"
    context = multiprocessing.get_context("spawn")
    clock_epoch = context.Value(
        "q", int(datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc).timestamp())
    )
    store = process_store(str(pool_db), clock_epoch)
    register(store, "process-builder-a", "builder")
    register(store, "process-builder-b", "builder")
    register(store, "process-validator-c", "validator")
    task = enqueue_fenced(store, "multiprocess-alternative-route")
    initialize_external_sqlite_sink(sink_db)

    checkpointed = context.Event()
    recovered = context.Event()
    fence_advanced = context.Event()
    stale_attempted = context.Event()
    handed_off = context.Event()
    validator_claimed = context.Event()
    completed = context.Event()
    allow_late_attempt = context.Event()
    allow_late_complete = context.Event()
    late_complete_attempted = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=process_actor_a,
            args=(
                str(pool_db), str(sink_db), task["task_id"], clock_epoch,
                checkpointed, allow_late_attempt, stale_attempted,
                allow_late_complete, late_complete_attempted, results,
            ),
            name="fenced-builder-a",
        ),
        context.Process(
            target=process_reconciler,
            args=(str(pool_db), clock_epoch, checkpointed, recovered, results),
            name="fenced-reconciler",
        ),
        context.Process(
            target=process_actor_b,
            args=(
                str(pool_db), str(sink_db), task["task_id"], clock_epoch,
                recovered, fence_advanced, stale_attempted, handed_off, results,
            ),
            name="fenced-builder-b",
        ),
        context.Process(
            target=process_validator_c,
            args=(
                str(pool_db), str(sink_db), task["task_id"], clock_epoch,
                handed_off, validator_claimed, late_complete_attempted,
                completed, results,
            ),
            name="fenced-validator-c",
        ),
    ]
    started_processes = []
    try:
        for process in processes:
            process.start()
            started_processes.append(process)
        assert checkpointed.wait(timeout=10), "A did not create its checkpoint"
        assert fence_advanced.wait(timeout=10), "B did not advance the sink fence to g2"
        with sqlite3.connect(sink_db) as db:
            assert db.execute(
                "SELECT max_generation FROM fences WHERE task_id=?", (task["task_id"],)
            ).fetchone()[0] == 2
            assert db.execute(
                "SELECT COUNT(*) FROM effects WHERE task_id=?", (task["task_id"],)
            ).fetchone()[0] == 0
        allow_late_attempt.set()
        assert stale_attempted.wait(timeout=10), "A did not attempt its stale g1"
        assert validator_claimed.wait(timeout=10), "C did not claim validation g3"
        allow_late_complete.set()
        assert late_complete_attempted.wait(timeout=10), "A did not attempt stale complete"
        assert completed.wait(timeout=20), "A→reconciler→B→C did not complete"
        for process in processes:
            process.join(timeout=10)
        assert all(not process.is_alive() for process in processes)
        assert all(process.exitcode == 0 for process in processes)
        messages = [results.get(timeout=5) for _ in processes]
    finally:
        checkpointed.set()
        recovered.set()
        fence_advanced.set()
        stale_attempted.set()
        handed_off.set()
        validator_claimed.set()
        completed.set()
        allow_late_attempt.set()
        allow_late_complete.set()
        late_complete_attempted.set()
        for process in started_processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()

    by_actor = {message["actor"]: message for message in messages}
    assert set(by_actor) == {"A", "reconciler", "B", "C"}
    assert all("error" not in message for message in messages), messages
    assert len({message["pid"] for message in messages}) == 4
    assert by_actor["reconciler"]["outcome"]["expired_leases_recovered"] == 1
    assert by_actor["A"]["task_id"] == task["task_id"]
    assert by_actor["B"]["task_id"] == task["task_id"]
    assert by_actor["C"]["task_id"] == task["task_id"]
    assert by_actor["A"]["generation"] == 1
    assert by_actor["B"]["generation"] == 2
    assert by_actor["C"]["generation"] == 3
    assert by_actor["B"]["checkpoint_ref"] == "shared://commit/process-a"
    assert by_actor["B"]["effect_created"] is True
    assert by_actor["C"]["state"] == "completed"
    assert by_actor["C"]["independent_effect_readback"] == (2, "from-b")
    assert by_actor["A"]["late_effect_rejected"] is True
    assert by_actor["A"]["late_effect_error"] == "stale external sqlite fence"
    assert by_actor["A"]["late_pool_mutation_rejected"] is True
    assert by_actor["A"]["late_pool_mutation_error"] == "lease inválido"
    assert by_actor["A"]["late_complete_rejected"] is True
    assert by_actor["A"]["late_complete_error"] == "lease inválido"
    assert by_actor["A"]["observed_sink_generation"] == 2

    final = process_store(str(pool_db), clock_epoch).get_task(task["task_id"])
    assert final["state"] == "completed"
    with sqlite3.connect(pool_db) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM tasks WHERE task_id=?", (task["task_id"],)
        ).fetchone()[0] == 1
    with sqlite3.connect(sink_db) as db:
        assert db.execute(
            "SELECT max_generation FROM fences WHERE task_id=?", (task["task_id"],)
        ).fetchone()[0] == 3
        assert db.execute(
            """SELECT operation_id,lease_generation,value FROM effects
               WHERE task_id=?""",
            (task["task_id"],),
        ).fetchall() == [("publish", 2, "from-b")]


def test_concurrent_fenced_claim_has_one_generation_one_winner(tmp_path: Path) -> None:
    clock = MutableClock()
    store = make_store(tmp_path, clock)
    register(store, "builder-a", "builder")
    register(store, "builder-b", "builder")
    task = enqueue_fenced(store, "concurrent-fenced-claim")
    barrier = threading.Barrier(2)
    results: list[tuple[str, int | None]] = []

    def claim(worker_id: str) -> None:
        barrier.wait(timeout=3)
        _task, lease = store.claim_task(
            worker_id=worker_id,
            role="builder",
            correlation_id=f"claim-{worker_id}",
            only_task_id=task["task_id"],
            fencing_required=True,
        )
        results.append(
            (worker_id, lease.lease_generation if lease is not None else None)
        )

    threads = [
        threading.Thread(target=claim, args=("builder-a",)),
        threading.Thread(target=claim, args=("builder-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(generation for _worker, generation in results if generation) == [1]
    assert sum(generation is None for _worker, generation in results) == 1
    assert store.get_task(task["task_id"])["lease_generation"] == 1


def test_checkpoint_replay_is_idempotent_and_divergence_fails_closed(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = make_store(tmp_path, clock)
    register(store, "builder-a", "builder")
    task = enqueue_fenced(store, "checkpoint-replay")
    _claimed, lease = store.claim_task(
        worker_id="builder-a",
        role="builder",
        correlation_id="claim",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert lease
    store.start_task(
        task_id=task["task_id"], worker_id="builder-a",
        correlation_id="start", **lease_args(lease)
    )
    before = store.get_task(task["task_id"])["last_material_progress_at"]
    _checkpoint, created = store.record_checkpoint(
        task_id=task["task_id"], worker_id="builder-a",
        correlation_id="checkpoint", checkpoint_id="cp-1", sequence=1,
        stage="analysis", next_action="continue", **lease_args(lease)
    )
    assert created is True
    after_create = store.get_task(task["task_id"])["last_material_progress_at"]
    assert after_create >= before

    clock.advance(5)
    _checkpoint, created = store.record_checkpoint(
        task_id=task["task_id"], worker_id="builder-a",
        correlation_id="checkpoint-replay", checkpoint_id="cp-1", sequence=1,
        stage="analysis", next_action="continue", **lease_args(lease)
    )
    assert created is False
    assert store.get_task(task["task_id"])["last_material_progress_at"] == after_create

    with pytest.raises(ConflictError, match="checkpoint divergente"):
        store.record_checkpoint(
            task_id=task["task_id"], worker_id="builder-a",
            correlation_id="checkpoint-divergent", checkpoint_id="cp-1", sequence=1,
            stage="analysis", next_action="different", **lease_args(lease)
        )


def test_unknown_effect_blocks_automatic_reroute(tmp_path: Path) -> None:
    clock = MutableClock()
    store = make_store(tmp_path, clock)
    register(store, "builder-a", "builder")
    register(store, "builder-b", "builder")
    task = enqueue_fenced(store, "unknown-effect")
    _claimed, lease = store.claim_task(
        worker_id="builder-a",
        role="builder",
        correlation_id="claim-a",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert lease
    store.start_task(
        task_id=task["task_id"], worker_id="builder-a",
        correlation_id="start-a", **lease_args(lease)
    )
    unknown_checkpoint, unknown_created = store.record_checkpoint(
        task_id=task["task_id"], worker_id="builder-a",
        correlation_id="effect-unknown", checkpoint_id="publish-unknown", sequence=1,
        stage="publish", next_action="reconcile destination before retry",
        effect_id="publish-release", effect_state="unknown", **lease_args(lease)
    )
    unknown_replay, unknown_replay_created = store.record_checkpoint(
        task_id=task["task_id"], worker_id="builder-a",
        correlation_id="effect-unknown-replay", checkpoint_id="publish-unknown", sequence=1,
        stage="publish", next_action="reconcile destination before retry",
        effect_id="publish-release", effect_state="unknown", **lease_args(lease)
    )
    assert unknown_created is True
    assert unknown_replay_created is False
    assert unknown_replay["content_digest"] == unknown_checkpoint["content_digest"]
    for later_state, result_ref in (
        ("pending", None),
        ("confirmed", "readback://worker/self-confirmed"),
        ("not_applied", "readback://worker/self-not-applied"),
    ):
        with pytest.raises(
            ConflictError,
            match="efeito unknown exige reconciliação independente",
        ):
            store.record_checkpoint(
                task_id=task["task_id"], worker_id="builder-a",
                correlation_id=f"unsafe-worker-{later_state}",
                checkpoint_id=f"unsafe-worker-{later_state}", sequence=2,
                stage="publish", next_action="must use separate reconciliation",
                effect_id="publish-release", effect_state=later_state,
                effect_result_ref=result_ref, **lease_args(lease)
            )
    with pytest.raises(ValueError, match="effect_id incompatível"):
        store.record_checkpoint(
            task_id=task["task_id"], worker_id="builder-a",
            correlation_id="unsafe-worker-none",
            checkpoint_id="unsafe-worker-none", sequence=2,
            stage="publish", next_action="cannot erase effect identity",
            effect_id="publish-release", effect_state="none", **lease_args(lease)
        )
    with pytest.raises(ValueError, match="effect_result_ref obrigatório"):
        store.record_checkpoint(
            task_id=task["task_id"], worker_id="builder-a",
            correlation_id="resolved-without-evidence",
            checkpoint_id="resolved-without-evidence", sequence=2,
            stage="publish", next_action="must include evidence",
            effect_id="different-effect", effect_state="confirmed",
            **lease_args(lease)
        )

    clock.advance(11)
    store.heartbeat_worker("builder-b", correlation_id="heartbeat-b")
    assert store.recover_expired_leases() == 1
    blocked = store.get_task(task["task_id"])
    assert blocked["state"] == "blocked"
    assert blocked["blocked_reason"] == "effect_reconciliation_required"

    with pytest.raises(ConflictError, match="efeito requer reconciliação"):
        store.requeue_blocked(task["task_id"], correlation_id="unsafe-requeue")
    next_task, next_lease = store.claim_task(
        worker_id="builder-b",
        role="builder",
        correlation_id="claim-b",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert next_task is None
    assert next_lease is None

    reconciliation, created = store.reconcile_effect(
        task_id=task["task_id"],
        effect_id="publish-release",
        lease_generation=lease.lease_generation,
        outcome="not_applied",
        readback_ref="readback://release/missing",
        correlation_id="reconcile-release",
    )
    replay, replay_created = store.reconcile_effect(
        task_id=task["task_id"],
        effect_id="publish-release",
        lease_generation=lease.lease_generation,
        outcome="not_applied",
        readback_ref="readback://release/missing",
        correlation_id="reconcile-release-replay",
    )
    assert created is True
    assert replay_created is False
    assert replay["content_digest"] == reconciliation["content_digest"]
    recovery_checkpoint = store.get_latest_checkpoint(task["task_id"])
    assert recovery_checkpoint
    assert recovery_checkpoint["reconciliation_outcome"] == "not_applied"
    assert recovery_checkpoint["reconciliation_readback_ref"] == (
        "readback://release/missing"
    )
    with pytest.raises(ConflictError, match="reconciliação divergente"):
        store.reconcile_effect(
            task_id=task["task_id"],
            effect_id="publish-release",
            lease_generation=lease.lease_generation,
            outcome="confirmed",
            readback_ref="readback://release/present",
            correlation_id="reconcile-release-divergent",
        )

    requeued = store.requeue_blocked(
        task["task_id"], correlation_id="safe-requeue"
    )
    assert requeued["state"] == "queued"
    resumed, resumed_lease = store.claim_task(
        worker_id="builder-b",
        role="builder",
        correlation_id="claim-b-after-reconcile",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert resumed and resumed_lease
    assert resumed_lease.lease_generation == 2
    assert resumed_lease.attempt_id != lease.attempt_id

    store.start_task(
        task_id=task["task_id"], worker_id="builder-b",
        correlation_id="start-b-after-reconcile", **lease_args(resumed_lease)
    )
    store.record_checkpoint(
        task_id=task["task_id"], worker_id="builder-b",
        correlation_id="same-effect-new-generation",
        checkpoint_id="publish-unknown-generation-2", sequence=1,
        stage="publish", next_action="reconcile generation two",
        effect_id="publish-release", effect_state="unknown",
        **lease_args(resumed_lease)
    )
    blocked_again = store.block_task(
        task_id=task["task_id"], worker_id="builder-b",
        reason="uncertain response", correlation_id="block-generation-two",
        **lease_args(resumed_lease)
    )
    assert blocked_again["blocked_reason"] == "effect_reconciliation_required"
    generation_two, generation_two_created = store.reconcile_effect(
        task_id=task["task_id"],
        effect_id="publish-release",
        lease_generation=resumed_lease.lease_generation,
        outcome="confirmed",
        readback_ref="readback://release/present",
        correlation_id="reconcile-generation-two",
    )
    assert generation_two_created is True
    assert generation_two["lease_generation"] == 2
    recovery_checkpoint = store.get_latest_checkpoint(task["task_id"])
    assert recovery_checkpoint
    assert recovery_checkpoint["reconciliation_outcome"] == "confirmed"
    assert recovery_checkpoint["reconciliation_readback_ref"] == (
        "readback://release/present"
    )
    assert store.requeue_blocked(
        task["task_id"], correlation_id="requeue-generation-two"
    )["state"] == "queued"


def test_unresolved_effect_cannot_escape_through_normal_transitions(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = make_store(tmp_path, clock)
    register(store, "builder-a", "builder")
    register(store, "validator-b", "validator")

    task = enqueue_fenced(store, "transition-gate-builder")
    _claimed, lease = store.claim_task(
        worker_id="builder-a",
        role="builder",
        correlation_id="claim-builder",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert lease
    store.start_task(
        task_id=task["task_id"], worker_id="builder-a",
        correlation_id="start-builder", **lease_args(lease)
    )
    store.record_checkpoint(
        task_id=task["task_id"], worker_id="builder-a",
        correlation_id="unknown-builder-effect", checkpoint_id="unknown-builder-effect",
        sequence=1, stage="publish", next_action="reconcile before release",
        effect_id="external-write", effect_state="pending", **lease_args(lease)
    )
    with pytest.raises(ConflictError, match="efeito requer reconciliação"):
        store.submit_for_validation(
            task_id=task["task_id"], worker_id="builder-a",
            produced_sha="3" * 40, correlation_id="unsafe-submit", **lease_args(lease)
        )
    with pytest.raises(ConflictError, match="efeito requer reconciliação"):
        store.fail_task(
            task_id=task["task_id"], worker_id="builder-a",
            reason="unsafe retry", correlation_id="unsafe-fail", **lease_args(lease)
        )
    still_owned = store.get_task(task["task_id"])
    assert still_owned["state"] == "running"
    assert still_owned["leased_by"] == "builder-a"
    blocked = store.block_task(
        task_id=task["task_id"], worker_id="builder-a",
        reason="ordinary block", correlation_id="safe-block", **lease_args(lease)
    )
    assert blocked["blocked_reason"] == "effect_reconciliation_required"

    validation_task = enqueue_fenced(store, "transition-gate-validator")
    _claimed, builder_lease = store.claim_task(
        worker_id="builder-a",
        role="builder",
        correlation_id="claim-validation-builder",
        only_task_id=validation_task["task_id"],
        fencing_required=True,
    )
    assert builder_lease
    store.start_task(
        task_id=validation_task["task_id"], worker_id="builder-a",
        correlation_id="start-validation-builder", **lease_args(builder_lease)
    )
    store.submit_for_validation(
        task_id=validation_task["task_id"], worker_id="builder-a",
        produced_sha="4" * 40, correlation_id="safe-submit",
        **lease_args(builder_lease)
    )
    _claimed, validator_lease = store.claim_task(
        worker_id="validator-b",
        role="validator",
        correlation_id="claim-validator",
        only_task_id=validation_task["task_id"],
        fencing_required=True,
    )
    assert validator_lease
    store.record_checkpoint(
        task_id=validation_task["task_id"], worker_id="validator-b",
        correlation_id="unknown-validator-effect",
        checkpoint_id="unknown-validator-effect", sequence=1,
        stage="validation", next_action="reconcile validation effect",
        effect_id="validation-write", effect_state="unknown",
        **lease_args(validator_lease)
    )
    with pytest.raises(ConflictError, match="efeito requer reconciliação"):
        store.complete_task(
            task_id=validation_task["task_id"], worker_id="validator-b",
            correlation_id="unsafe-complete", **lease_args(validator_lease)
        )
    assert store.get_task(validation_task["task_id"])["state"] == "validating"


def test_recovery_context_preserves_distinct_multi_effect_outcomes(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = make_store(tmp_path, clock)
    register(store, "builder-a", "builder")
    register(store, "builder-b", "builder")
    task = enqueue_fenced(store, "multi-effect-recovery-context")
    _claimed, lease_a = store.claim_task(
        worker_id="builder-a",
        role="builder",
        correlation_id="claim-a",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert lease_a
    store.start_task(
        task_id=task["task_id"], worker_id="builder-a",
        correlation_id="start-a", **lease_args(lease_a)
    )
    for sequence, effect_id in enumerate(("effect-a", "effect-b"), start=1):
        store.record_checkpoint(
            task_id=task["task_id"], worker_id="builder-a",
            correlation_id=f"checkpoint-{effect_id}",
            checkpoint_id=f"checkpoint-{effect_id}", sequence=sequence,
            stage="publish", next_action="reconcile before resume",
            effect_id=effect_id, effect_state="unknown", **lease_args(lease_a)
        )
    store.block_task(
        task_id=task["task_id"], worker_id="builder-a",
        reason="uncertain effects", correlation_id="block-a", **lease_args(lease_a)
    )
    for effect_id, outcome in (
        ("effect-a", "confirmed"),
        ("effect-b", "not_applied"),
    ):
        store.reconcile_effect(
            task_id=task["task_id"], effect_id=effect_id,
            lease_generation=lease_a.lease_generation, outcome=outcome,
            readback_ref=f"readback://{effect_id}/{outcome}",
            correlation_id=f"reconcile-{effect_id}",
        )
    store.requeue_blocked(task["task_id"], correlation_id="safe-requeue")
    _claimed, lease_b = store.claim_task(
        worker_id="builder-b",
        role="builder",
        correlation_id="claim-b",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert lease_b and lease_b.lease_generation == 2

    context = store.get_recovery_context(task["task_id"])
    effect_ledger = {
        row["effect_id"]: row for row in context["effects"]
    }
    assert set(effect_ledger) == {"effect-a", "effect-b"}
    assert effect_ledger["effect-a"]["reconciliation_outcome"] == "confirmed"
    assert effect_ledger["effect-b"]["reconciliation_outcome"] == "not_applied"
    skip_effects = {
        effect_id for effect_id, row in effect_ledger.items()
        if row["reconciliation_outcome"] == "confirmed"
    }
    retryable_effects = {
        effect_id for effect_id, row in effect_ledger.items()
        if row["reconciliation_outcome"] == "not_applied"
    }
    assert skip_effects == {"effect-a"}
    assert retryable_effects == {"effect-b"}
