from __future__ import annotations

import importlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def load_app(
    tmp_path: Path,
    monkeypatch,
    *,
    reconciliation_token_value: str = "test-reconciliation-token-value",
):
    service_root = Path(__file__).resolve().parents[1]
    if str(service_root) not in sys.path:
        sys.path.insert(0, str(service_root))
    token_file = tmp_path / "api-token"
    token_file.write_text("test-token-value\n", encoding="utf-8")
    reconciliation_token_file = tmp_path / "reconciliation-token"
    reconciliation_token_file.write_text(
        f"{reconciliation_token_value}\n", encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_WORKER_POOL_DB", str(tmp_path / "pool.db"))
    monkeypatch.setenv("CODEX_WORKER_POOL_API_TOKEN_FILE", str(token_file))
    monkeypatch.setenv(
        "CODEX_WORKER_POOL_RECONCILIATION_TOKEN_FILE",
        str(reconciliation_token_file),
    )
    monkeypatch.setenv("CODEX_WORKER_POOL_HEARTBEAT_TTL_SECONDS", "60")
    monkeypatch.setenv("CODEX_WORKER_POOL_LEASE_SECONDS", "30")
    monkeypatch.setenv("CODEX_WORKER_POOL_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("CODEX_WORKER_POOL_PROGRESS_STALL_SECONDS", "30")
    monkeypatch.setenv("CODEX_WORKER_POOL_EXPECTED_RULES_SHA", "a" * 40)
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    return module, TestClient(module.app), {"Authorization": "Bearer test-token-value"}


def test_health_and_auth_fail_closed(tmp_path: Path, monkeypatch) -> None:
    _module, client, headers = load_app(tmp_path, monkeypatch)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["auth_configured"] is True
    assert health.json()["reconciliation_auth_configured"] is True
    assert health.json()["expected_rules_sha_configured"] is True
    assert health.json()["progress_stall_seconds"] == 30

    denied = client.get("/v1/snapshot")
    assert denied.status_code == 401

    allowed = client.get("/v1/snapshot", headers=headers)
    assert allowed.status_code == 200
    assert allowed.json()["progress_stall_seconds"] == 30

    watchdog = client.post(
        "/v1/watchdog/recover",
        headers={**headers, "X-Correlation-Id": "watchdog-empty"},
    )
    assert watchdog.status_code == 200
    assert watchdog.json() == {
        "rerouted": 0,
        "blocked": 0,
        "failed": 0,
        "correlation_id": "watchdog-empty",
    }


def test_fenced_admission_requires_separate_reconciliation_authority(
    tmp_path: Path, monkeypatch
) -> None:
    _module, client, headers = load_app(
        tmp_path,
        monkeypatch,
        reconciliation_token_value="test-token-value",
    )
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["reconciliation_auth_configured"] is False

    payload = {
        "repository": "example/fenced-readiness",
        "issue_number": 19,
        "request_id": "fenced-readiness",
        "correlation_id": "fenced-readiness",
        "base_sha": "1" * 40,
        "fencing_required": True,
    }
    rejected = client.post("/v1/tasks", headers=headers, json=payload)
    assert rejected.status_code == 503
    assert rejected.json()["detail"] == (
        "reconciliation_auth_not_configured_or_not_separated"
    )

    payload["request_id"] = "legacy-readiness"
    payload["fencing_required"] = False
    legacy = client.post("/v1/tasks", headers=headers, json=payload)
    assert legacy.status_code == 201


def test_api_e2e_builder_validator_replay(tmp_path: Path, monkeypatch) -> None:
    _module, client, headers = load_app(tmp_path, monkeypatch)

    for worker_id, role in (("desktop-builder", "builder"), ("noteri-validator", "validator")):
        response = client.post(
            "/v1/workers",
            headers=headers,
            json={
                "worker_id": worker_id,
                "host": worker_id.split("-")[0],
                "role": role,
                "profile": "NORMAL",
                "capacity_score": 80,
                "controller_version": "0.2.51",
                "rules_sha": "a" * 40,
                "gateway_ok": True,
                "state_validated": True,
                "worktree_root": f"C:/dev/chatgpt-workers/{worker_id}",
                "correlation_id": f"corr-{worker_id}",
            },
        )
        assert response.status_code == 200

    payload = {
        "repository": "ericson-j-santos/reqsys-v2-enterprise-real",
        "issue_number": 1769,
        "request_id": "api-e2e-1769",
        "correlation_id": "api-e2e",
        "priority": 10,
        "base_sha": "1" * 40,
        "max_attempts": 2,
    }
    first = client.post("/v1/tasks", headers=headers, json=payload)
    replay = client.post("/v1/tasks", headers=headers, json=payload)
    assert first.status_code == 201
    assert replay.status_code == 200
    assert first.json()["created"] is True
    assert replay.json()["created"] is False
    task_id = first.json()["task"]["task_id"]
    assert replay.json()["task"]["task_id"] == task_id

    build_claim = client.post(
        "/v1/claims",
        headers=headers,
        json={"worker_id": "desktop-builder", "correlation_id": "build-claim"},
    )
    assert build_claim.status_code == 200
    assert build_claim.json()["claimed"] is True
    build_lease = build_claim.json()["lease"]["lease_token"]

    start = client.post(
        f"/v1/tasks/{task_id}/start",
        headers=headers,
        json={
            "worker_id": "desktop-builder",
            "lease_token": build_lease,
            "correlation_id": "build-start",
        },
    )
    assert start.status_code == 200
    assert start.json()["state"] == "running"

    handoff = client.post(
        f"/v1/tasks/{task_id}/validation",
        headers=headers,
        json={
            "worker_id": "desktop-builder",
            "lease_token": build_lease,
            "correlation_id": "handoff",
            "produced_sha": "b" * 40,
        },
    )
    assert handoff.status_code == 200
    assert handoff.json()["state"] == "validating"

    validator_claim = client.post(
        "/v1/claims",
        headers=headers,
        json={"worker_id": "noteri-validator", "correlation_id": "validator-claim"},
    )
    assert validator_claim.status_code == 200
    validation_lease = validator_claim.json()["lease"]["lease_token"]

    complete = client.post(
        f"/v1/tasks/{task_id}/complete",
        headers=headers,
        json={
            "worker_id": "noteri-validator",
            "lease_token": validation_lease,
            "correlation_id": "complete",
        },
    )
    assert complete.status_code == 200
    assert complete.json()["state"] == "completed"

    independent = client.get(f"/v1/tasks/{task_id}", headers=headers)
    assert independent.status_code == 200
    assert independent.json()["state"] == "completed"
    assert independent.json()["produced_sha"] == "b" * 40
    assert "lease_token" not in independent.json()

    final_replay = client.post("/v1/tasks", headers=headers, json=payload)
    assert final_replay.status_code == 200
    assert final_replay.json()["created"] is False


def test_task_requires_base_sha(tmp_path: Path, monkeypatch) -> None:
    _module, client, headers = load_app(tmp_path, monkeypatch)

    response = client.post(
        "/v1/tasks",
        headers=headers,
        json={
            "repository": "ericson-j-santos/reqsys-v2-enterprise-real",
            "issue_number": 1769,
            "request_id": "missing-base-sha",
            "correlation_id": "missing-base-sha",
        },
    )

    assert response.status_code == 422


def test_fenced_claim_and_checkpoint_api_are_opt_in_and_idempotent(
    tmp_path: Path, monkeypatch
) -> None:
    module, client, headers = load_app(tmp_path, monkeypatch)
    blocked_before_cutover = client.post(
        "/v1/tasks",
        headers=headers,
        json={
            "repository": "example/fenced-api",
            "issue_number": 90,
            "request_id": "blocked-before-exclusive-cutover",
            "correlation_id": "blocked-before-exclusive-cutover",
            "base_sha": "0" * 40,
            "fencing_required": True,
        },
    )
    assert blocked_before_cutover.status_code == 409
    assert blocked_before_cutover.json()["detail"] == "fenced_cutover_not_enabled"
    module.store.enable_fenced_cutover(
        correlation_id="api-exclusive-cutover",
        legacy_claimers_drained=True,
        shared_queue_exclusive=True,
    )
    registered = client.post(
        "/v1/workers",
        headers=headers,
        json={
            "worker_id": "fenced-builder",
            "host": "portable-host",
            "role": "builder",
            "profile": "NORMAL",
            "gateway_ok": True,
            "state_validated": True,
            "rules_sha": "a" * 40,
            "correlation_id": "register-fenced-builder",
        },
    )
    assert registered.status_code == 200
    created = client.post(
        "/v1/tasks",
        headers=headers,
        json={
            "repository": "example/fenced-api",
            "issue_number": 91,
            "request_id": "fenced-api",
            "correlation_id": "enqueue-fenced-api",
            "base_sha": "1" * 40,
            "fencing_required": True,
        },
    )
    assert created.status_code == 201
    task_id = created.json()["task"]["task_id"]
    assert created.json()["task"]["fencing_required"] is True
    divergent_replay = client.post(
        "/v1/tasks",
        headers=headers,
        json={
            "repository": "example/fenced-api",
            "issue_number": 91,
            "request_id": "fenced-api",
            "correlation_id": "enqueue-fenced-api-divergent",
            "base_sha": "1" * 40,
            "fencing_required": False,
        },
    )
    assert divergent_replay.status_code == 409

    claim_payload = {
        "worker_id": "fenced-builder",
        "task_id": task_id,
        "correlation_id": "claim-fenced-api",
    }
    legacy = client.post(
        "/v1/claims/exact", headers=headers, json=claim_payload
    )
    assert legacy.status_code == 200
    assert legacy.json()["claimed"] is False

    claimed = client.post(
        "/v1/claims/fenced/exact", headers=headers, json=claim_payload
    )
    assert claimed.status_code == 200
    lease = claimed.json()["lease"]
    assert lease["lease_generation"] == 1
    assert lease["attempt_id"]

    mutation = {
        "worker_id": "fenced-builder",
        "lease_token": lease["lease_token"],
        "correlation_id": "start-fenced-api",
    }
    missing_fence = client.post(
        f"/v1/tasks/{task_id}/start", headers=headers, json=mutation
    )
    assert missing_fence.status_code == 409

    mutation.update(
        lease_generation=lease["lease_generation"],
        attempt_id=lease["attempt_id"],
    )
    started = client.post(
        f"/v1/tasks/{task_id}/start", headers=headers, json=mutation
    )
    assert started.status_code == 200

    checkpoint = {
        **mutation,
        "correlation_id": "checkpoint-fenced-api",
        "checkpoint_id": "api-checkpoint-1",
        "sequence": 1,
        "stage": "code-ready",
        "next_action": "validate on another executor",
    }
    first = client.post(
        f"/v1/tasks/{task_id}/checkpoints", headers=headers, json=checkpoint
    )
    replay = client.post(
        f"/v1/tasks/{task_id}/checkpoints", headers=headers, json=checkpoint
    )
    assert first.status_code == 201
    assert replay.status_code == 200
    assert first.json()["created"] is True
    assert replay.json()["created"] is False
    assert (
        first.json()["checkpoint"]["content_digest"]
        == replay.json()["checkpoint"]["content_digest"]
    )

    denied = client.get(f"/v1/tasks/{task_id}/checkpoints/latest")
    latest = client.get(
        f"/v1/tasks/{task_id}/checkpoints/latest", headers=headers
    )
    assert denied.status_code == 401
    assert latest.status_code == 200
    assert latest.json()["checkpoint"]["checkpoint_id"] == "api-checkpoint-1"

    unknown_effect = {
        **mutation,
        "correlation_id": "checkpoint-effect-unknown",
        "checkpoint_id": "api-effect-unknown",
        "sequence": 2,
        "stage": "publish",
        "next_action": "reconcile through independent authority",
        "effect_id": "release-publish",
        "effect_state": "unknown",
    }
    checkpoint_unknown = client.post(
        f"/v1/tasks/{task_id}/checkpoints", headers=headers, json=unknown_effect
    )
    assert checkpoint_unknown.status_code == 201
    for effect_state, effect_result_ref in (
        ("pending", None),
        ("confirmed", "readback://worker/self-confirmed"),
        ("not_applied", "readback://worker/self-not-applied"),
    ):
        self_resolve = {
            **mutation,
            "correlation_id": f"worker-self-resolve-{effect_state}",
            "checkpoint_id": f"worker-self-resolve-{effect_state}",
            "sequence": 3,
            "stage": "publish",
            "next_action": "must use reconciliation authority",
            "effect_id": "release-publish",
            "effect_state": effect_state,
        }
        if effect_result_ref is not None:
            self_resolve["effect_result_ref"] = effect_result_ref
        rejected_self_resolve = client.post(
            f"/v1/tasks/{task_id}/checkpoints",
            headers=headers,
            json=self_resolve,
        )
        assert rejected_self_resolve.status_code == 409
    missing_evidence = client.post(
        f"/v1/tasks/{task_id}/checkpoints",
        headers=headers,
        json={
            **mutation,
            "correlation_id": "resolved-without-evidence",
            "checkpoint_id": "resolved-without-evidence",
            "sequence": 3,
            "stage": "publish",
            "next_action": "must include evidence",
            "effect_id": "different-effect",
            "effect_state": "confirmed",
        },
    )
    assert missing_evidence.status_code == 422
    blocked = client.post(
        f"/v1/tasks/{task_id}/block",
        headers=headers,
        json={**mutation, "correlation_id": "block-effect", "reason": "uncertain"},
    )
    assert blocked.status_code == 200
    assert blocked.json()["blocked_reason"] == "effect_reconciliation_required"

    reconcile_payload = {
        "lease_generation": lease["lease_generation"],
        "outcome": "not_applied",
        "readback_ref": "readback://release/not-found",
        "correlation_id": "reconcile-effect",
    }
    reconciliation_path = (
        f"/v1/tasks/{task_id}/effects/release-publish/reconcile"
    )
    worker_denied = client.post(
        reconciliation_path, headers=headers, json=reconcile_payload
    )
    assert worker_denied.status_code == 401
    reconciliation_headers = {
        "Authorization": "Bearer test-reconciliation-token-value"
    }
    reconciled = client.post(
        reconciliation_path, headers=reconciliation_headers, json=reconcile_payload
    )
    replayed = client.post(
        reconciliation_path, headers=reconciliation_headers, json=reconcile_payload
    )
    assert reconciled.status_code == 201
    assert replayed.status_code == 200
    assert reconciled.json()["created"] is True
    assert replayed.json()["created"] is False

    requeued = client.post(
        f"/v1/tasks/{task_id}/requeue",
        headers=headers,
        json={"correlation_id": "requeue-after-reconciliation"},
    )
    assert requeued.status_code == 200
    assert requeued.json()["state"] == "queued"
    resumed = client.post(
        "/v1/claims/fenced/exact",
        headers=headers,
        json={**claim_payload, "correlation_id": "claim-after-reconciliation"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["claimed"] is True
    assert resumed.json()["lease"]["lease_generation"] == 2
    recovery_context = resumed.json()["recovery_context"]
    assert recovery_context["latest_checkpoint"]["effect_id"] == "release-publish"
    assert recovery_context["effects"][0]["effect_id"] == "release-publish"
    assert recovery_context["effects"][0]["reconciliation_outcome"] == (
        "not_applied"
    )
    assert recovery_context["effects"][0]["reconciliation_readback_ref"] == (
        "readback://release/not-found"
    )


def test_repository_lane_and_worker_affinity_api(tmp_path: Path, monkeypatch) -> None:
    _module, client, headers = load_app(tmp_path, monkeypatch)

    worker = client.post(
        "/v1/workers",
        headers=headers,
        json={
            "worker_id": "desktop-builder-repo",
            "host": "desktop",
            "role": "builder",
            "profile": "NORMAL",
            "capacity_score": 80,
            "controller_version": "0.2.51",
            "rules_sha": "a" * 40,
            "gateway_ok": True,
            "state_validated": True,
            "worktree_root": "C:/dev/chatgpt-workers/desktop-builder-repo",
            "correlation_id": "repo-api-worker",
        },
    )
    assert worker.status_code == 200

    configured = client.post(
        "/v1/repositories",
        headers=headers,
        json={
            "repository": "ericson-j-santos/painel-powerbi",
            "enabled": True,
            "max_in_flight": 2,
            "correlation_id": "repo-api-config",
        },
    )
    assert configured.status_code == 200
    assert configured.json()["max_in_flight"] == 2

    affinity = client.put(
        "/v1/workers/desktop-builder-repo/affinities",
        headers=headers,
        json={
            "repositories": ["ericson-j-santos/painel-powerbi"],
            "correlation_id": "repo-api-affinity",
        },
    )
    assert affinity.status_code == 200
    assert affinity.json()["repositories"] == ["ericson-j-santos/painel-powerbi"]

    observed = client.get("/v1/repositories", headers=headers)
    assert observed.status_code == 200
    lane = next(
        item
        for item in observed.json()
        if item["repository"] == "ericson-j-santos/painel-powerbi"
    )
    assert lane["enabled"] is True
    assert lane["max_in_flight"] == 2

    snapshot = client.get("/v1/snapshot", headers=headers)
    assert snapshot.status_code == 200
    worker_view = next(
        item
        for item in snapshot.json()["workers"]
        if item["worker_id"] == "desktop-builder-repo"
    )
    assert worker_view["repository_affinity"] == ["ericson-j-santos/painel-powerbi"]
