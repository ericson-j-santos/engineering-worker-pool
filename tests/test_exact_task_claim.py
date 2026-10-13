"""Exact-task claims cannot consume unrelated queued product work."""
from __future__ import annotations

import importlib
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = "ericson-j-santos/reqsys-v2-enterprise-real"


def api_fixture(tmp_path: Path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    token = tmp_path / "api-token"
    token.write_text("fixture-only-value\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_WORKER_POOL_DB", str(tmp_path / "pool.db"))
    monkeypatch.setenv("CODEX_WORKER_POOL_API_TOKEN_FILE", str(token))
    monkeypatch.setenv("CODEX_WORKER_POOL_EXPECTED_RULES_SHA", "a" * 40)
    monkeypatch.setenv("CODEX_WORKER_POOL_HEARTBEAT_TTL_SECONDS", "60")
    sys.modules.pop("app.main", None)
    api = importlib.import_module("app.main")
    return api, TestClient(api.app), {"Authorization": "Bearer fixture-only-value"}


def worker(client, headers, name, role, profile="NORMAL"):
    response = client.post("/v1/workers", headers=headers, json={
        "worker_id": name, "host": "synthetic", "role": role,
        "profile": profile, "rules_sha": "a" * 40,
        "gateway_ok": True, "state_validated": True,
        "correlation_id": "worker-registration",
    })
    assert response.status_code == 200


def enqueue(client, headers, issue, priority):
    result = client.post("/v1/tasks", headers=headers, json={
        "repository": REPO, "issue_number": issue, "base_sha": "1" * 40,
        "request_id": "exact-test-" + str(issue), "correlation_id": "enqueue",
        "priority": priority, "max_attempts": 3,
    })
    assert result.status_code == 201
    return result.json()["task"]["task_id"]


def exact(client, headers, who, task_id):
    return client.post("/v1/claims/exact", headers=headers, json={
        "worker_id": who, "task_id": task_id, "correlation_id": "exact",
    })


def test_exact_claim_skips_higher_priority_unsupported_task(tmp_path, monkeypatch):
    _api, client, headers = api_fixture(tmp_path, monkeypatch)
    worker(client, headers, "capable", "builder")
    other = enqueue(client, headers, 2547, 0)
    target = enqueue(client, headers, 2546, 100)
    selected = exact(client, headers, "capable", target)
    assert selected.status_code == 200
    assert selected.json()["claimed"] is True
    assert selected.json()["task"]["task_id"] == target
    current = client.get(f"/v1/tasks/{other}", headers=headers).json()
    assert (current["state"], current["attempt_count"], current["leased_by"]) == ("queued", 0, None)
    assert "lease_token" not in current
    assert client.get(f"/v1/tasks/{target}", headers=headers).json()["attempt_count"] == 1


def test_auth_malformed_absent_and_estudo_fail_closed(tmp_path, monkeypatch):
    _api, client, headers = api_fixture(tmp_path, monkeypatch)
    worker(client, headers, "capable", "builder")
    worker(client, headers, "study", "builder", profile="ESTUDO")
    target = enqueue(client, headers, 2546, 0)
    assert exact(client, {}, "capable", target).status_code == 401
    assert exact(client, headers, "capable", "invalid").status_code == 422
    unknown = exact(client, headers, "capable", "cwp-" + "f" * 24)
    assert unknown.status_code == 200
    assert unknown.json() == {"claimed": False, "task": None, "lease": None}
    assert exact(client, headers, "study", target).status_code == 409
    state = client.get(f"/v1/tasks/{target}", headers=headers).json()
    assert (state["state"], state["attempt_count"]) == ("queued", 0)


def test_builder_validator_exact_completion_and_independent_readback(tmp_path, monkeypatch):
    _api, client, headers = api_fixture(tmp_path, monkeypatch)
    worker(client, headers, "builder", "builder")
    worker(client, headers, "validator", "validator")
    unrelated = enqueue(client, headers, 2547, 0)
    target = enqueue(client, headers, 2546, 30)
    c = exact(client, headers, "builder", target).json()
    assert c["claimed"] is True
    lease = {"worker_id": "builder", "lease_token": c["lease"]["lease_token"],
             "correlation_id": "progress"}
    assert client.post(f"/v1/tasks/{target}/start", headers=headers, json=lease).status_code == 200
    transfer = client.post(f"/v1/tasks/{target}/validation", headers=headers, json={
        **lease, "produced_sha": "b" * 40,
    })
    assert transfer.status_code == 200
    validator = exact(client, headers, "validator", target).json()
    assert validator["claimed"] is True
    finish = client.post(f"/v1/tasks/{target}/complete", headers=headers, json={
        "worker_id": "validator", "lease_token": validator["lease"]["lease_token"],
        "correlation_id": "validated",
    })
    assert finish.status_code == 200
    final = client.get(f"/v1/tasks/{target}", headers=headers).json()
    assert final["state"] == "completed" and final["produced_sha"] == "b" * 40
    assert "lease_token" not in final
    other = client.get(f"/v1/tasks/{unrelated}", headers=headers).json()
    assert (other["state"], other["attempt_count"]) == ("queued", 0)
    assert exact(client, headers, "validator", target).json()["claimed"] is False


def test_concurrent_exact_claim_has_one_winner(tmp_path):
    from app.store import WorkerPoolStore
    store = WorkerPoolStore(tmp_path / "pool.db", expected_rules_sha="a" * 40)
    for name in ("one", "two"):
        store.register_worker(worker_id=name, host="synthetic", role="builder",
                              correlation_id="registration", rules_sha="a" * 40,
                              gateway_ok=True, state_validated=True)
    task, _ = store.enqueue_task(repository=REPO, issue_number=2546, request_id="race",
                                 correlation_id="enqueue", base_sha="1" * 40)
    barrier, results, errors = threading.Barrier(2), [], []
    def run(name):
        try:
            barrier.wait(timeout=3)
            found, _lease = store.claim_task(worker_id=name, role="builder",
                                             correlation_id="race", only_task_id=task["task_id"])
            results.append(found["task_id"] if found else None)
        except Exception as exc:
            errors.append(type(exc).__name__)
    threads = [threading.Thread(target=run, args=(name,)) for name in ("one", "two")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert errors == [] and len(results) == 2
    assert results.count(task["task_id"]) == results.count(None) == 1
    assert store.get_task(task["task_id"])["attempt_count"] == 1


def test_exact_claim_does_not_reconcile_unrelated_expired_lease(tmp_path):
    from app.store import WorkerPoolStore
    class Clock:
        value = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        def __call__(self):
            return self.value
    clock = Clock()
    store = WorkerPoolStore(tmp_path / "pool.db", clock=clock, heartbeat_ttl_seconds=60,
                            default_lease_seconds=10, default_max_attempts=3,
                            progress_stall_seconds=30, expected_rules_sha="a" * 40)
    store.configure_repository(repository=REPO, max_in_flight=2)
    for name in ("legacy", "exact"):
        store.register_worker(worker_id=name, role="builder", host="synthetic",
                              correlation_id="registration", rules_sha="a" * 40,
                              gateway_ok=True, state_validated=True)
    older, _ = store.enqueue_task(repository=REPO, issue_number=2547, request_id="old",
                                  base_sha="1" * 40, correlation_id="enqueue", priority=0)
    newer, _ = store.enqueue_task(repository=REPO, issue_number=2546, request_id="new",
                                  base_sha="1" * 40, correlation_id="enqueue", priority=100)
    first, lease = store.claim_task(worker_id="legacy", role="builder", correlation_id="general")
    assert first["task_id"] == older["task_id"]
    store.start_task(task_id=older["task_id"], worker_id="legacy",
                     lease_token=lease.lease_token, correlation_id="started")
    clock.value += timedelta(seconds=31)
    store.heartbeat_worker("exact", correlation_id="fresh")
    picked, _ = store.claim_task(worker_id="exact", role="builder", correlation_id="exact",
                                 only_task_id=newer["task_id"])
    assert picked["task_id"] == newer["task_id"]
    previous = store.get_task(older["task_id"])
    assert previous["state"] == "running" and previous["leased_by"] == "legacy"
    assert previous["attempt_count"] == 1
    with pytest.raises(ValueError, match="exact_task_id"):
        store.claim_task(worker_id="exact", role="builder", correlation_id="invalid",
                         only_task_id="not-a-task")
