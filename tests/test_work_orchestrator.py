from __future__ import annotations

import importlib
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def load_app(tmp_path: Path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    token_file = tmp_path / "api-token"
    token_file.write_text("work-orchestrator-token\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_WORKER_POOL_DB", str(tmp_path / "pool.db"))
    monkeypatch.setenv("CODEX_WORKER_POOL_API_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CODEX_WORKER_POOL_EXPECTED_RULES_SHA", "a" * 40)
    monkeypatch.setenv("CODEX_WORKER_POOL_PROGRESS_STALL_SECONDS", "300")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    return module, TestClient(module.app), {"Authorization": "Bearer work-orchestrator-token"}


def register_workers(client: TestClient, headers: dict[str, str]) -> None:
    for worker_id, role in (("builder-work", "builder"), ("validator-work", "validator")):
        response = client.post(
            "/v1/workers",
            headers=headers,
            json={
                "worker_id": worker_id,
                "host": worker_id,
                "role": role,
                "profile": "NORMAL",
                "capacity_score": 90,
                "rules_sha": "a" * 40,
                "gateway_ok": True,
                "state_validated": True,
                "correlation_id": f"register-{worker_id}",
            },
        )
        assert response.status_code == 200


def build_and_validate(client: TestClient, headers: dict[str, str], task_id: str) -> str:
    build_claim = client.post(
        "/v1/claims",
        headers=headers,
        json={"worker_id": "builder-work", "correlation_id": "claim-builder"},
    )
    assert build_claim.status_code == 200
    lease = build_claim.json()["lease"]["lease_token"]

    started = client.post(
        f"/v1/tasks/{task_id}/start",
        headers=headers,
        json={"worker_id": "builder-work", "lease_token": lease, "correlation_id": "start"},
    )
    assert started.status_code == 200

    produced_sha = "b" * 40
    handoff = client.post(
        f"/v1/tasks/{task_id}/validation",
        headers=headers,
        json={
            "worker_id": "builder-work",
            "lease_token": lease,
            "correlation_id": "handoff",
            "produced_sha": produced_sha,
        },
    )
    assert handoff.status_code == 200

    validation_claim = client.post(
        "/v1/claims",
        headers=headers,
        json={"worker_id": "validator-work", "correlation_id": "claim-validator"},
    )
    assert validation_claim.status_code == 200
    validation_lease = validation_claim.json()["lease"]["lease_token"]

    completed = client.post(
        f"/v1/tasks/{task_id}/complete",
        headers=headers,
        json={
            "worker_id": "validator-work",
            "lease_token": validation_lease,
            "correlation_id": "complete-validator",
        },
    )
    assert completed.status_code == 200
    assert completed.json()["state"] == "completed"
    return produced_sha


def test_work_flow_queue_to_governed_merge_with_replay_and_controls(tmp_path: Path, monkeypatch) -> None:
    _module, client, headers = load_app(tmp_path, monkeypatch)
    register_workers(client, headers)

    payload = {
        "repository": "example/consumer",
        "issue_number": 2048,
        "request_id": "work-orchestrator-e2e",
        "correlation_id": "work-create",
        "base_sha": "1" * 40,
    }
    created = client.post("/v1/work", headers=headers, json=payload)
    replay = client.post("/v1/work", headers=headers, json=payload)
    assert created.status_code == 201
    assert replay.status_code == 200
    assert created.json()["created"] is True
    assert replay.json()["created"] is False
    work = created.json()["work"]
    assert work["phase"] == "queue"
    assert replay.json()["work"]["work_id"] == work["work_id"]
    assert "lease_token" not in work["task"]

    produced_sha = build_and_validate(client, headers, work["task_id"])

    awaiting_evidence = client.get(f"/v1/work/{work['work_id']}", headers=headers)
    assert awaiting_evidence.status_code == 200
    assert awaiting_evidence.json()["phase"] == "evidence"

    bad_sha = client.post(
        f"/v1/work/{work['work_id']}/evidence",
        headers=headers,
        json={
            "validation_run_id": "ci-100",
            "validation_sha": "c" * 40,
            "evidence_reference": "github-actions/run/100",
            "independent_readback": True,
            "positive_control": True,
            "negative_control": True,
            "correlation_id": "evidence-bad-sha",
        },
    )
    assert bad_sha.status_code == 409

    missing_control = client.post(
        f"/v1/work/{work['work_id']}/evidence",
        headers=headers,
        json={
            "validation_run_id": "ci-100",
            "validation_sha": produced_sha,
            "evidence_reference": "github-actions/run/100",
            "independent_readback": True,
            "positive_control": True,
            "negative_control": False,
            "correlation_id": "evidence-missing-control",
        },
    )
    assert missing_control.status_code == 409

    evidence_payload = {
        "validation_run_id": "ci-101",
        "validation_sha": produced_sha,
        "evidence_reference": "github-actions/run/101",
        "independent_readback": True,
        "positive_control": True,
        "negative_control": True,
        "correlation_id": "evidence-good",
    }
    evidence = client.post(
        f"/v1/work/{work['work_id']}/evidence",
        headers=headers,
        json=evidence_payload,
    )
    evidence_replay = client.post(
        f"/v1/work/{work['work_id']}/evidence",
        headers=headers,
        json=evidence_payload,
    )
    assert evidence.status_code == 200
    assert evidence_replay.status_code == 200
    assert evidence.json()["phase"] == "merge"
    assert evidence.json()["merge"]["state"] == "ready"

    wrong_head = client.post(
        f"/v1/work/{work['work_id']}/merge-result",
        headers=headers,
        json={
            "expected_head_sha": "d" * 40,
            "merged": False,
            "reason": "required_checks_pending",
            "correlation_id": "merge-wrong-head",
        },
    )
    assert wrong_head.status_code == 409

    blocked = client.post(
        f"/v1/work/{work['work_id']}/merge-result",
        headers=headers,
        json={
            "expected_head_sha": produced_sha,
            "merged": False,
            "reason": "required_checks_pending",
            "correlation_id": "merge-blocked",
        },
    )
    assert blocked.status_code == 200
    assert blocked.json()["phase"] == "merge_blocked"

    merged_payload = {
        "expected_head_sha": produced_sha,
        "merged": True,
        "merge_commit_sha": "e" * 40,
        "correlation_id": "merge-complete",
    }
    merged = client.post(
        f"/v1/work/{work['work_id']}/merge-result",
        headers=headers,
        json=merged_payload,
    )
    merged_replay = client.post(
        f"/v1/work/{work['work_id']}/merge-result",
        headers=headers,
        json=merged_payload,
    )
    assert merged.status_code == 200
    assert merged_replay.status_code == 200
    assert merged.json()["phase"] == "merged"
    assert merged.json()["merge"]["merge_commit_sha"] == "e" * 40

    independent = client.get(f"/v1/work/{work['work_id']}", headers=headers)
    assert independent.status_code == 200
    assert independent.json()["phase"] == "merged"
    assert independent.json()["task"]["produced_sha"] == produced_sha
    assert independent.json()["evidence"]["independent_readback"] is True
