"""Live isolated API contract plus negative controls for native exact-claim client."""
from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
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
    reconciliation_token = tmp_path / "reconciliation-token"
    reconciliation_token.write_text(
        "isolated-reconciliation-token-value\n", encoding="ascii"
    )
    reconciliation_token.chmod(0o600)
    monkeypatch.setenv("CODEX_WORKER_POOL_DB", str(tmp_path / "pool.db"))
    monkeypatch.setenv("CODEX_WORKER_POOL_API_TOKEN_FILE", str(token))
    monkeypatch.setenv(
        "CODEX_WORKER_POOL_RECONCILIATION_TOKEN_FILE",
        str(reconciliation_token),
    )
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
        # A distinct wrong client token must not rotate the server's secret.
        bad_token = tmp_path / "incorrect-client-token"
        bad_token.write_text("incorrect-but-valid-token\n", encoding="ascii")
        bad_token.chmod(0o600)
        assert token.read_text(encoding="ascii").strip() == "isolated-worker-token-value"
        with LocalWorkerApi(port=18097, token_file=bad_token, client=app) as unauth:
            with pytest.raises(NativeExecutorBlocked, match="worker_auth_rejected"):
                unauth.get_task(task_id)
        assert app.get(f"/v1/tasks/{task_id}", headers=headers).json()["state"] == "completed"


def test_fenced_exact_http_checkpoint_and_stale_generation(tmp_path, monkeypatch):
    token = token_file(tmp_path)
    module, app = isolated_server(tmp_path, monkeypatch, token)
    headers = {"Authorization": "Bearer " + token.read_text().strip()}
    with app:
        module.store.enable_fenced_cutover(
            correlation_id="native-http-cutover",
            legacy_claimers_drained=True,
            shared_queue_exclusive=True,
        )
        for worker_id, role in (("native-builder", "builder"),
                                ("native-validator", "validator")):
            response = app.post("/v1/workers", headers=headers, json={
                "worker_id": worker_id, "host": "isolated",
                "role": role, "profile": "NORMAL",
                "gateway_ok": True, "state_validated": True,
                "rules_sha": RULES, "worktree_root": str(tmp_path / worker_id),
                "correlation_id": "native-fenced-register-" + role,
            })
            assert response.status_code == 200
        created = app.post("/v1/tasks", headers=headers, json={
            "repository": REPO, "issue_number": 18,
            "request_id": "native-http-fenced", "base_sha": BASE,
            "correlation_id": "native-fenced-create",
            "fencing_required": True,
        })
        assert created.status_code == 201
        task_id = created.json()["task"]["task_id"]

        with LocalWorkerApi(port=18097, token_file=token, client=app) as api:
            assert api.claim_exact(
                "native-builder", task_id, "native-fenced-legacy-negative",
            )["claimed"] is False
            builder = api.claim_fenced_exact(
                "native-builder", task_id, "native-fenced-claim-builder",
            )
            lease_a = builder["lease"]
            assert lease_a["lease_generation"] == 1
            assert builder["recovery_context"] == {
                "latest_checkpoint": None, "effects": [],
            }
            fence_a = {
                "lease_generation": lease_a["lease_generation"],
                "attempt_id": lease_a["attempt_id"],
            }
            api.start(
                task_id, "native-builder", lease_a["lease_token"],
                "native-fenced-start", **fence_a,
            )
            effect_id = "native-artifact-http"
            pending = api.record_checkpoint(
                task_id, "native-builder", lease_a["lease_token"],
                lease_a["lease_generation"], lease_a["attempt_id"],
                "native-builder-artifact-pending", 1, "artifact_publish",
                "publish_artifact", "native-fenced-checkpoint-pending",
                effect_id=effect_id, effect_state="pending",
            )
            assert pending["effect_state"] == "pending"
            confirmed = api.record_checkpoint(
                task_id, "native-builder", lease_a["lease_token"],
                lease_a["lease_generation"], lease_a["attempt_id"],
                "native-builder-result", 2, "builder_result",
                "submit_validation", "native-fenced-checkpoint-confirmed",
                artifact_kind="git_bundle.v1",
                artifact_ref="artifact://fixture/bundle",
                artifact_sha256="d" * 64,
                effect_id=effect_id,
                effect_state="confirmed",
                effect_result_ref="artifact://fixture/bundle",
            )
            assert confirmed == api.get_latest_checkpoint(task_id)
            api.submit_validation(
                task_id, "native-builder", lease_a["lease_token"], "c" * 40,
                "native-fenced-validation", **fence_a,
            )

            validator = api.claim_fenced_exact(
                "native-validator", task_id, "native-fenced-claim-validator",
            )
            lease_b = validator["lease"]
            assert lease_b["lease_generation"] == 2
            assert validator["recovery_context"]["latest_checkpoint"][
                "checkpoint_id"] == "native-builder-result"
            with pytest.raises(NativeExecutorBlocked, match="worker_http_unverified"):
                api.fail(
                    task_id, "native-builder", lease_a["lease_token"],
                    "native-fenced-stale-fail", "adapter_failed", **fence_a,
                )
            active = api.get_task(task_id)
            assert active["state"] == "validating"
            assert active["leased_by"] == "native-validator"

            api.record_checkpoint(
                task_id, "native-validator", lease_b["lease_token"],
                lease_b["lease_generation"], lease_b["attempt_id"],
                "native-validator-result", 1, "validator_result", "complete",
                "native-fenced-validator-checkpoint",
                artifact_kind="git_bundle.v1",
                artifact_ref="artifact://fixture/bundle",
                artifact_sha256="d" * 64,
            )
            api.complete(
                task_id, "native-validator", lease_b["lease_token"],
                "native-fenced-complete",
                lease_generation=lease_b["lease_generation"],
                attempt_id=lease_b["attempt_id"],
            )
            final = api.get_task(task_id)
            assert final["state"] == "completed"
            assert final["lease_generation"] == 2

            ambiguous_created = app.post("/v1/tasks", headers=headers, json={
                "repository": REPO, "issue_number": 19,
                "request_id": "native-http-ambiguous", "base_sha": BASE,
                "correlation_id": "native-ambiguous-create",
                "fencing_required": True,
            })
            assert ambiguous_created.status_code == 201
            ambiguous_id = ambiguous_created.json()["task"]["task_id"]
            ambiguous_claim = api.claim_fenced_exact(
                "native-builder", ambiguous_id, "native-ambiguous-claim",
            )
            ambiguous_lease = ambiguous_claim["lease"]
            ambiguous_fence = {
                "lease_generation": ambiguous_lease["lease_generation"],
                "attempt_id": ambiguous_lease["attempt_id"],
            }
            api.start(
                ambiguous_id, "native-builder", ambiguous_lease["lease_token"],
                "native-ambiguous-start", **ambiguous_fence,
            )
            for sequence, state, action in (
                (1, "pending", "publish_artifact"),
                (2, "unknown", "reconcile_artifact"),
            ):
                api.record_checkpoint(
                    ambiguous_id, "native-builder",
                    ambiguous_lease["lease_token"],
                    ambiguous_lease["lease_generation"],
                    ambiguous_lease["attempt_id"],
                    f"native-ambiguous-{state}", sequence,
                    "artifact_publish", action,
                    f"native-ambiguous-checkpoint-{state}",
                    effect_id="native-ambiguous-artifact",
                    effect_state=state,
                )
            api.block(
                ambiguous_id, "native-builder", ambiguous_lease["lease_token"],
                "native-ambiguous-block", "artifact_publish_ambiguous",
                **ambiguous_fence,
            )
            blocked = api.get_task(ambiguous_id)
            assert blocked["state"] == "blocked"
            assert blocked["blocked_reason"] == "effect_reconciliation_required"


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


@pytest.mark.parametrize("readback_stage,accepted", [
    ("artifact_publish", True),
    ("different_stage", False),
])
def test_checkpoint_transport_ambiguity_uses_readback_without_post_replay(
    tmp_path, readback_stage, accepted,
):
    token = token_file(tmp_path)
    task_id = "cwp-" + "1" * 24
    calls = []
    stored = None

    def handler(request):
        nonlocal stored
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            body = json.loads(request.content)
            stored = {
                "task_id": task_id,
                **{
                    key: value for key, value in body.items()
                    if key not in {"worker_id", "lease_token"}
                },
                "stage": readback_stage,
            }
            raise httpx.ConnectTimeout("private-network-detail")
        return httpx.Response(
            200, json={"checkpoint": stored}, request=request,
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler), trust_env=False,
        follow_redirects=False,
    )
    try:
        with LocalWorkerApi(port=8097, token_file=token, client=client) as api:
            arguments = (
                task_id, "native-builder", "isolated-lease-token-123", 1,
                "attempt-1", "checkpoint-1", 1, "artifact_publish",
                "publish_artifact", "native-checkpoint-ambiguity",
            )
            if accepted:
                checkpoint = api.record_checkpoint(*arguments)
                assert checkpoint["checkpoint_id"] == "checkpoint-1"
            else:
                with pytest.raises(
                    NativeExecutorBlocked, match="worker_checkpoint_ambiguous"
                ):
                    api.record_checkpoint(*arguments)
        assert calls == [
            ("POST", f"/v1/tasks/{task_id}/checkpoints"),
            ("GET", f"/v1/tasks/{task_id}/checkpoints/latest"),
        ]
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


@pytest.mark.parametrize("drift", ["generation", "attempt", "recovery"])
def test_fenced_claim_rejects_missing_or_inconsistent_fence_context(tmp_path, drift):
    task_id = "cwp-" + "1" * 24
    worker_id = "native-builder"
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    task = {
        "task_id": task_id,
        "state": "leased",
        "leased_by": worker_id,
        "fencing_required": True,
        "lease_generation": 1,
        "attempt_id": "attempt-1",
    }
    lease = {
        "task_id": task_id,
        "worker_id": worker_id,
        "role": "builder",
        "lease_token": "isolated-lease-token-123",
        "lease_expires_at": future,
        "lease_generation": 1,
        "attempt_id": "attempt-1",
    }
    recovery = {"latest_checkpoint": None, "effects": []}
    if drift == "generation":
        lease["lease_generation"] = None
    elif drift == "attempt":
        task["attempt_id"] = "different-attempt"
    else:
        recovery["effects"] = "not-a-list"
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "claimed": True,
            "task": task,
            "lease": lease,
            "recovery_context": recovery,
        }, request=request)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), trust_env=False,
        follow_redirects=False,
    )
    try:
        with LocalWorkerApi(
            port=8097, token_file=token_file(tmp_path), client=client,
        ) as api:
            with pytest.raises(
                NativeExecutorBlocked, match="worker_fenced_claim_invalid"
            ):
                api.claim_fenced_exact(
                    worker_id, task_id, "native-fenced-claim-negative",
                )
        assert len(calls) == 1
        assert calls[0].url.path == "/v1/claims/fenced/exact"
    finally:
        client.close()


@pytest.mark.parametrize("lease_patch,task_patch", [
    ({"role": "operator"}, {}),
    ({"role": None}, {}),
    ({"lease_expires_at": None}, {}),
    ({"lease_expires_at": "not-an-iso-date"}, {}),
    ({"lease_expires_at": "2000-01-01T00:00:00+00:00"}, {}),
    ({"lease_expires_at": "2999-01-01T00:00:00"}, {}),
    ({}, {"state": "completed"}),
])
def test_claim_with_unverified_role_or_expiry_never_becomes_eligible(
    tmp_path, lease_patch, task_patch,
):
    task_id = "cwp-" + "1" * 24
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    lease = {
        "task_id": task_id, "worker_id": "native-builder",
        "role": "builder", "lease_token": "isolated-lease-token-123",
        "lease_expires_at": future,
    }
    task = {
        "task_id": task_id, "state": "leased",
        "leased_by": "native-builder",
    }
    lease.update(lease_patch)
    task.update(task_patch)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "claimed": True, "task": task, "lease": lease,
        }, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler),
                          trust_env=False, follow_redirects=False)
    try:
        with LocalWorkerApi(port=8097, token_file=token_file(tmp_path),
                            client=client) as api:
            with pytest.raises(NativeExecutorBlocked, match="worker_claim_invalid"):
                api.claim_exact("native-builder", task_id, "native-lease-negative")
        assert len(calls) == 1
        assert calls[0].url.path == "/v1/claims/exact"
    finally:
        client.close()


@pytest.mark.parametrize("configured", [None, 90])
def test_trusted_request_timeout_reaches_the_http_transport(tmp_path, configured):
    observed = []
    def handle(request):
        observed.append(request.extensions["timeout"])
        return httpx.Response(200, json={"contract": "fixture"})
    token = token_file(tmp_path)
    options = {} if configured is None else {"timeout_seconds": configured}
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with LocalWorkerApi(port=18097, token_file=token, client=client, **options) as api:
            assert api._request("GET", "/v1/contract") == {"contract": "fixture"}
    expected = 8 if configured is None else configured
    assert observed == [{key: expected for key in ("connect", "read", "write", "pool")}]


@pytest.mark.parametrize("invalid", [True, 0, 121, float("nan"), float("inf"), "90", None])
def test_unbounded_or_untyped_request_timeout_is_rejected(tmp_path, invalid):
    with pytest.raises(NativeExecutorBlocked, match="worker_timeout_invalid"):
        LocalWorkerApi(port=18097, token_file=tmp_path / "unused", timeout_seconds=invalid)
