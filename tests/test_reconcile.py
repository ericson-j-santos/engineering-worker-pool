from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from app.reconcile import ReconciliationController  # noqa: E402
from app.store import WorkerPoolStore  # noqa: E402


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def make_store(
    tmp_path: Path,
    clock: MutableClock,
    *,
    lease_seconds: int = 120,
    stall_seconds: int = 30,
) -> WorkerPoolStore:
    return WorkerPoolStore(
        tmp_path / "reconcile.db",
        clock=clock,
        heartbeat_ttl_seconds=60,
        default_lease_seconds=lease_seconds,
        default_max_attempts=3,
        progress_stall_seconds=stall_seconds,
        expected_rules_sha="a" * 40,
    )


def register_builder(store: WorkerPoolStore, worker_id: str) -> None:
    store.register_worker(
        worker_id=worker_id,
        host=f"host-{worker_id}",
        role="builder",
        profile="NORMAL",
        correlation_id=f"register-{worker_id}",
        controller_version="0.2.51",
        rules_sha="a" * 40,
        gateway_ok=True,
        state_validated=True,
        worktree_root=f"C:/dev/chatgpt-workers/{worker_id}",
    )


def enqueue_and_start(
    store: WorkerPoolStore,
    *,
    worker_id: str,
    request_id: str,
) -> tuple[dict, str]:
    task, created = store.enqueue_task(
        repository="ericson-j-santos/reconcile-test",
        issue_number=11,
        request_id=request_id,
        correlation_id=f"enqueue-{request_id}",
        priority=1,
        base_sha="1" * 40,
        max_attempts=3,
    )
    assert created is True
    claimed, lease = store.claim_task(
        worker_id=worker_id,
        role="builder",
        correlation_id=f"claim-{request_id}",
    )
    assert claimed and lease
    running = store.start_task(
        task_id=task["task_id"],
        worker_id=worker_id,
        lease_token=lease.lease_token,
        correlation_id=f"start-{request_id}",
    )
    assert running["state"] == "running"
    return running, lease.lease_token


async def run_background_cycle(controller: ReconciliationController) -> dict:
    await controller.start()
    for _ in range(100):
        status = controller.status()
        if status["last_completed_at"] is not None:
            break
        await asyncio.sleep(0.01)
    status = controller.status()
    await controller.stop()
    assert status["last_completed_at"] is not None
    return status


def test_background_controller_reroutes_stalled_task_without_external_traffic(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = make_store(tmp_path, clock)
    register_builder(store, "builder-a")
    register_builder(store, "builder-b")
    running, _lease = enqueue_and_start(
        store,
        worker_id="builder-a",
        request_id="autonomous-reroute",
    )
    material_progress_at = running["last_material_progress_at"]

    clock.advance(31)
    store.heartbeat_worker("builder-b", correlation_id="heartbeat-builder-b")

    controller = ReconciliationController(store, interval_seconds=0.1)
    status = asyncio.run(run_background_cycle(controller))

    observed = store.get_task(running["task_id"])
    assert observed["state"] == "queued"
    assert observed["leased_by"] is None
    assert observed["last_error"] == "material_progress_timeout_rerouted"
    assert observed["last_material_progress_at"] == material_progress_at
    assert status["last_result"]["rerouted"] == 1


def test_background_controller_blocks_stalled_task_without_alternative(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = make_store(tmp_path, clock)
    register_builder(store, "builder-a")
    running, _lease = enqueue_and_start(
        store,
        worker_id="builder-a",
        request_id="autonomous-block",
    )

    clock.advance(31)

    controller = ReconciliationController(store, interval_seconds=0.1)
    status = asyncio.run(run_background_cycle(controller))

    observed = store.get_task(running["task_id"])
    assert observed["state"] == "blocked"
    assert observed["leased_by"] is None
    assert observed["blocked_reason"] == "material_progress_timeout_no_alternative"
    assert status["last_result"]["blocked"] == 1


def test_background_controller_recovers_expired_lease_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = make_store(tmp_path, clock, lease_seconds=10, stall_seconds=300)
    register_builder(store, "builder-a")
    running, _lease = enqueue_and_start(
        store,
        worker_id="builder-a",
        request_id="autonomous-expired-lease",
    )

    clock.advance(11)

    controller = ReconciliationController(store, interval_seconds=0.1)
    first = asyncio.run(controller.run_once())
    second = asyncio.run(controller.run_once())

    observed = store.get_task(running["task_id"])
    assert observed["state"] == "queued"
    assert observed["leased_by"] is None
    assert first == {
        "expired_leases_recovered": 1,
        "rerouted": 0,
        "blocked": 0,
        "failed": 0,
    }
    assert second == {
        "expired_leases_recovered": 0,
        "rerouted": 0,
        "blocked": 0,
        "failed": 0,
    }


def test_controller_survives_cycle_failure_and_recovers_next_cycle() -> None:
    class FlakyStore:
        def __init__(self) -> None:
            self.calls = 0

        def reconcile(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("synthetic failure")
            return {
                "expired_leases_recovered": 0,
                "rerouted": 0,
                "blocked": 0,
                "failed": 0,
            }

    controller = ReconciliationController(FlakyStore(), interval_seconds=0.1)  # type: ignore[arg-type]

    assert asyncio.run(controller.run_once()) is None
    failed_status = controller.status()
    assert failed_status["consecutive_failures"] == 1
    assert failed_status["last_error_code"] == "reconciliation_cycle_failed"

    recovered = asyncio.run(controller.run_once())
    recovered_status = controller.status()
    assert recovered is not None
    assert recovered_status["consecutive_failures"] == 0
    assert recovered_status["last_error_code"] is None


def test_fastapi_lifespan_starts_and_stops_reconciliation_controller(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token_file = tmp_path / "api-token"
    token_file.write_text("test-token\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_WORKER_POOL_DB", str(tmp_path / "lifespan.db"))
    monkeypatch.setenv("CODEX_WORKER_POOL_API_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CODEX_WORKER_POOL_EXPECTED_RULES_SHA", "a" * 40)
    monkeypatch.setenv("CODEX_WORKER_POOL_RECONCILE_INTERVAL_SECONDS", "0.1")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")

    with TestClient(module.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["reconciliation"]["running"] is True
        assert health.json()["reconciliation"]["interval_seconds"] == 0.1

    assert module.reconciliation_controller.status()["running"] is False
