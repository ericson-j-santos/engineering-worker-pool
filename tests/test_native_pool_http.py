"""Live isolated API contract plus negative controls for native exact-claim client."""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.native_one_shot import NativeExecutorBlocked
from app.native_pool_http import LocalWorkerApi

RULES = "a" * 40
BASE = "b" * 40
REPO = "ericson-j-santos/reqsys-v2-enterprise-real"


def token_file(tmp_path: Path, value: str = "isolated-worker-token-value") -> Path:
    path = tmp_path / "pool-token"
    path.write_text(value + "\n", encoding="ascii")
    path.chmod(0o600)
    return path


def isolated_server(tmp_path: Path, monkeypatch, token: Path):
    monkeypatch.setenv("CODEX_WORKER_POOL_DB", str(tmp_path / "pool.db"))
    monkeypatch.setenv("CODEX_WORKER_POOL_API_TOKEN_FILE", str(token))
    monkeypatch.setenv("CODEX_WORKER_POOL_EXPECTED_RULES_SHA", RULES)
    monkeypatch.setenv("CODEX_WORKER_POOL_HEARTBEAT_TTL_SECONDS", "600")
    monkeypatch.setenv("CODEX_WORKER_POOL_LEASE_SECONDS", "300")
    monkeypatch.setenv("CODEX_WORKER_POOL_RECONCILE_INTERVAL_SECONDS", "10")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    return module, TestClient(module.app, base_url="http://127.0.0.1:18097")


def test_exact_claim_real_isolated_api_and_independent_readback(tmp_path, monkeypatch):
    token = token_file(tmp_path)
    _module, app = isolated_server(tmp_path, monkeypatch, token)
    headers = {"Authorization": "Bearer " + token.read_text().strip()}
    with app:
        assert app.get("/health").status_code == 200
        for worker_id, role in (("native-builder", "builder"),
                                ("native-validator", "validator")):
            registered = app.post("/v1/workers", headers=headers, json={
                "worker_id": worker_id, "host": "isolated",
                "role": role, "profile": "NORMAL",
                "gateway_ok": True, "state_validated": True,
                "rules_sha": RULES, "worktree_root": str(tmp_path / worker_id),
                "correlation_id": "native-http-register-" + role,
            })
            assert registered.status_code == 200
        created = app.post("/v1/tasks", headers=headers, json={
            "repository": REPO, "issue_number": 8,
            "request_id": "native-http-contract", "base_sha": BASE,
            "correlation_id": "native-http-create",
        })
        assert created.status_code == 201
        task_id = created.json()["task"]["task_id"]
        with LocalWorkerApi(port=18097, token_file=token, client=app) as api:
            assert api.get_task(task_id)["state"] == "queued"
            claim = api.claim_exact("native-builder", task_id, "native-http-claim-b")
            assert claim["claimed"] is True and claim["task"]["task_id"] == task_id
            builder_token = claim["lease"]["lease_token"]
            api.start(task_id, "native-builder", builder_token, "native-http-start")
            assert api.get_task(task_id)["state"] == "running"
            api.submit_validation(
                task_id, "native-builder", builder_token, "c" * 40,
                "native-http-validation",
            )
            validator = api.claim_exact("native-validator", task_id, "native-http-claim-v")
            assert validator["claimed"] is True
            api.complete(task_id, "native-validator",
                         validator["lease"]["lease_token"], "native-http-complete")
            readback = api.get_task(task_id)
            assert readback["state"] == "completed"
            assert readback["produced_sha"] == "c" * 40
            assert "lease_token" not in readback
            assert api.claim_exact("native-builder", task_id, "native-http-replay")[
                "claimed"] is False
        # The default client must not bypass real HTTP auth.
        token.write_text("incorrect-but-valid-token\n", encoding="ascii")
        with LocalWorkerApi(port=18097, token_file=token, client=app) as unauth:
            with pytest.raises(NativeExecutorBlocked, match="worker_auth_rejected"):
                unauth.get_task(task_id)
        assert app.get(f"/v1/tasks/{task_id}", headers=headers).json()["state"] == "completed"


@pytest.mark.parametrize("port", [0, 65536, True, "8097"])
def test_invalid_port_blocks_without_network(tmp_path, port):
    with pytest.raises(NativeExecutorBlocked, match="worker_port_invalid"):
        LocalWorkerApi(port=port, token_file=token_file(tmp_path))


def test_missing_permissive_and_symlink_token_block(tmp_path):
    missing = tmp_path / "missing"
    with LocalWorkerApi(port=8097, token_file=missing) as api:
        with pytest.raises(NativeExecutorBlocked, match="worker_token_file_unavailable"):
            api.get_task("cwp-" + "1" * 24)
    unsafe = token_file(tmp_path)
    unsafe.chmod(0o644)
    with LocalWorkerApi(port=8097, token_file=unsafe) as api:
        with pytest.raises(NativeExecutorBlocked, match="worker_token_permissions"):
            api.get_task("cwp-" + "1" * 24)
    unsafe.chmod(0o600)
    alias = tmp_path / "token-link"
    alias.symlink_to(unsafe)
    with LocalWorkerApi(port=8097, token_file=alias) as api:
        with pytest.raises(NativeExecutorBlocked, match="worker_token_file_unavailable"):
            api.get_task("cwp-" + "1" * 24)


@pytest.mark.parametrize("status,data,code", [
    (401, {"detail": "secret-should-not-appear"}, "worker_auth_rejected"),
    (307, {"detail": "redirect"}, "worker_http_unverified"),
    (503, {"detail": "service"}, "worker_http_unverified"),
    (200, ["wrong"], "worker_response_invalid"),
    (200, {"state": "queued", "task_id": "cwp-" + "f" * 24},
     "worker_task_readback_invalid"),
])
def test_status_contract_and_identity_fail_closed(tmp_path, status, data, code):
    token = token_file(tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=data, request=request)
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          trust_env=False, follow_redirects=False)
    try:
        with LocalWorkerApi(port=8097, token_file=token, client=client) as api:
            with pytest.raises(NativeExecutorBlocked, match=code) as error:
                api.get_task("cwp-" + "1" * 24)
        assert len(calls) == 1
        assert "secret-should-not-appear" not in str(error.value)
        assert calls[0].url.host == "127.0.0.1"
    finally:
        client.close()


def test_transport_failure_never_retries_mutation(tmp_path):
    token = token_file(tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectTimeout("private-network-detail")
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          trust_env=False, follow_redirects=False)
    try:
        with LocalWorkerApi(port=8097, token_file=token, client=client) as api:
            with pytest.raises(NativeExecutorBlocked, match="worker_transport_unverified") as error:
                api.claim_exact("native-builder", "cwp-" + "1" * 24,
                                "native-http-timeout")
        assert len(calls) == 1
        assert "private-network-detail" not in str(error.value)
    finally:
        client.close()


def test_reject_unbounded_or_wrong_claim_without_claim_replay(tmp_path):
    token = token_file(tmp_path)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "claimed": "true", "task": {"task_id": "cwp-" + "1" * 24},
            "lease": None,
        }, request=request)
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          trust_env=False, follow_redirects=False)
    try:
        with LocalWorkerApi(port=8097, token_file=token, client=client) as api:
            with pytest.raises(NativeExecutorBlocked, match="worker_claim_invalid"):
                api.claim_exact("native-builder", "cwp-" + "1" * 24,
                                "native-http-reject")
        assert len(calls) == 1
    finally:
        client.close()
