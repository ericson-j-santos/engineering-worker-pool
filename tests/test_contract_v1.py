from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "contracts" / "v1" / "contract.json"


def load_runtime(tmp_path: Path, monkeypatch):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    token_file = tmp_path / "api-token"
    token_file.write_text("contract-test-token\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_WORKER_POOL_DB", str(tmp_path / "pool.db"))
    monkeypatch.setenv("CODEX_WORKER_POOL_API_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CODEX_WORKER_POOL_EXPECTED_RULES_SHA", "a" * 40)
    monkeypatch.setenv("CODEX_WORKER_POOL_PROGRESS_STALL_SECONDS", "300")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    return module, TestClient(module.app), {"Authorization": "Bearer contract-test-token"}


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_contract_manifest_is_independent_and_contains_no_secret_material() -> None:
    raw = MANIFEST.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["contract_name"] == "engineering-worker-pool"
    assert data["contract_version"] == "v1"
    assert "ReqSys" not in raw
    assert "reqsys-v2-enterprise-real" not in raw
    assert "ghp_" not in raw
    assert "github_pat_" not in raw
    assert "BEGIN PRIVATE KEY" not in raw
    assert data["security"]["secret_values_in_contract"] is False


def test_contract_operations_exist_in_runtime(tmp_path: Path, monkeypatch) -> None:
    module, _client, _headers = load_runtime(tmp_path, monkeypatch)
    runtime = {
        (method, route.path)
        for route in module.app.routes
        for method in getattr(route, "methods", set())
    }
    for operation in manifest()["operations"]:
        assert (operation["method"], operation["path"]) in runtime, operation["id"]


def test_contract_required_request_fields_match_runtime_models(tmp_path: Path, monkeypatch) -> None:
    module, _client, _headers = load_runtime(tmp_path, monkeypatch)
    for model_name, declared in manifest()["models"].items():
        model = getattr(module, model_name)
        runtime_required = {
            name for name, field in model.model_fields.items() if field.is_required()
        }
        assert runtime_required == set(declared["required"]), model_name


def test_contract_descriptor_and_health_publish_v1(tmp_path: Path, monkeypatch) -> None:
    _module, client, headers = load_runtime(tmp_path, monkeypatch)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["contract_version"] == "v1"

    denied = client.get("/v1/contract")
    assert denied.status_code == 401

    discovered = client.get("/v1/contract", headers=headers)
    assert discovered.status_code == 200
    assert discovered.json() == {
        "contract_name": "engineering-worker-pool",
        "contract_version": "v1",
        "service_name": "codex-worker-pool",
        "compatibility_policy": "backward-compatible-additive",
    }


def test_contract_core_semantics_e2e(tmp_path: Path, monkeypatch) -> None:
    _module, client, headers = load_runtime(tmp_path, monkeypatch)

    for worker_id, role in (("builder-v1", "builder"), ("validator-v1", "validator")):
        registered = client.post(
            "/v1/workers",
            headers=headers,
            json={
                "worker_id": worker_id,
                "host": worker_id,
                "role": role,
                "profile": "NORMAL",
                "capacity_score": 80,
                "rules_sha": "a" * 40,
                "gateway_ok": True,
                "state_validated": True,
                "correlation_id": f"register-{worker_id}",
            },
        )
        assert registered.status_code == 200

    payload = {
        "repository": "example/consumer",
        "issue_number": 2020,
        "request_id": "contract-v1-replay",
        "correlation_id": "contract-v1-enqueue",
        "base_sha": "1" * 40,
    }
    created = client.post("/v1/tasks", headers=headers, json=payload)
    replay = client.post("/v1/tasks", headers=headers, json=payload)
    assert created.status_code == 201
    assert replay.status_code == 200
    assert created.json()["created"] is True
    assert replay.json()["created"] is False
    task_id = created.json()["task"]["task_id"]
    assert replay.json()["task"]["task_id"] == task_id

    claim = client.post(
        "/v1/claims",
        headers=headers,
        json={"worker_id": "builder-v1", "correlation_id": "claim-builder"},
    )
    lease = claim.json()["lease"]["lease_token"]
    assert claim.json()["claimed"] is True

    started = client.post(
        f"/v1/tasks/{task_id}/start",
        headers=headers,
        json={"worker_id": "builder-v1", "lease_token": lease, "correlation_id": "start"},
    )
    assert started.status_code == 200

    handoff = client.post(
        f"/v1/tasks/{task_id}/validation",
        headers=headers,
        json={
            "worker_id": "builder-v1",
            "lease_token": lease,
            "correlation_id": "handoff",
            "produced_sha": "b" * 40,
        },
    )
    assert handoff.status_code == 200
    assert handoff.json()["produced_sha"] == "b" * 40

    validation_claim = client.post(
        "/v1/claims",
        headers=headers,
        json={"worker_id": "validator-v1", "correlation_id": "claim-validator"},
    )
    validation_lease = validation_claim.json()["lease"]["lease_token"]
    completed = client.post(
        f"/v1/tasks/{task_id}/complete",
        headers=headers,
        json={
            "worker_id": "validator-v1",
            "lease_token": validation_lease,
            "correlation_id": "complete",
        },
    )
    assert completed.status_code == 200
    assert completed.json()["state"] == "completed"

    readback = client.get(f"/v1/tasks/{task_id}", headers=headers)
    assert readback.status_code == 200
    assert readback.json()["state"] == "completed"
    assert readback.json()["produced_sha"] == "b" * 40
    assert "lease_token" not in readback.json()

    final_replay = client.post("/v1/tasks", headers=headers, json=payload)
    assert final_replay.status_code == 200
    assert final_replay.json()["created"] is False
    assert final_replay.json()["task"]["task_id"] == task_id
