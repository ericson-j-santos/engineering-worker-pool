"""Owner-authorized issue admission into existing fenced Worker Pool.

Synthetic GitHub observations; one E2E uses the real FastAPI, SQLite, auth,
fenced-cutover and canonical HTTP readback. No Ollama or GitHub writes here.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import issue_pool_admission as admission
from app import issue_draft_pr as gate
from app.native_pool_http import LocalWorkerApi, NativeExecutorBlocked
from test_issue_draft_pr import FakeOfficialGitHub, BASE, NOW, admitted


@pytest.fixture
def inputs():
    binding = admitted()
    source = (Path(__file__).resolve().parents[1] / gate.PATH).read_bytes()
    return binding, source, FakeOfficialGitHub(binding, source)


class FakePool:
    def __init__(self):
        self.lanes = [{"repository": gate.REPO, "enabled": True, "max_in_flight": 1}]
        self.effects = []
        self.task = None

    def list_repositories(self):
        return self.lanes

    def enqueue_fenced_issue(self, payload):
        self.effects.append(dict(payload))
        created = self.task is None
        if created:
            self.task = {**payload, "task_id": "cwp-" + "2" * 24,
                         "branch": payload["target_branch"], "state": "queued"}
        return {"created": created, "task_id": self.task["task_id"],
                "task": self.task}


def execute(github, pool, *, env="dev"):
    return admission.admit_to_existing_pool(
        github=github, pool=pool, issue_number=8, comment_id=8721,
        environment=env, now=NOW,
    )


def test_real_baseline_and_idempotent_issue_dispatch(inputs):
    binding, source, github = inputs
    pool = FakePool()
    first = execute(github, pool)
    second = execute(github, pool)
    assert first["result"] == second["result"] == "ISSUE_FENCED_ENQUEUED"
    assert first["baseline_total"] == 42 and first["baseline_passed"] < 42
    assert first["task_id"] == second["task_id"]
    assert first["created"] is True and second["created"] is False
    assert all(item["request_id"] == binding.request_id
               and item["target_branch"] == binding.branch
               and item["fencing_required"] is True
               and item["max_attempts"] == 1 for item in pool.effects)
    assert first["builder_started"] is False
    assert first["draft_pr_created"] is False


@pytest.mark.parametrize("kind,code", [
    ("production", "issue_dispatch_dev_only"),
    ("closed", "issue_identity_invalid"),
    ("comment_revoked", "owner_authorization_missing"),
    ("branch_drift", "main_head_drift"),
    ("disabled_lane", "issue_fenced_lane_not_ready"),
    ("duplicate_lane", "issue_fenced_lane_not_ready"),
    ("wide_lane", "issue_fenced_lane_not_ready"),
    ("already_fixed", "issue_no_failing_baseline"),
])
def test_negative_admission_has_no_worker_write(inputs, monkeypatch, kind, code):
    _, _, github = inputs
    pool = FakePool()
    monkeypatch.setattr(admission, "validate", lambda *_: {"total": 42, "passed": 26})
    if kind == "closed":
        github.issue["state"] = "closed"
    elif kind == "comment_revoked":
        github.comment["body"] = "/local-agent revoke"
    elif kind == "branch_drift":
        github.main["commit"]["sha"] = "f" * 40
    elif kind == "disabled_lane":
        pool.lanes[0]["enabled"] = False
    elif kind == "duplicate_lane":
        pool.lanes.append(dict(pool.lanes[0]))
    elif kind == "wide_lane":
        pool.lanes[0]["max_in_flight"] = 2
    elif kind == "already_fixed":
        monkeypatch.setattr(admission, "validate",
                            lambda *_: {"total": 42, "passed": 42})
    with pytest.raises(gate.HandoffBlocked, match="^" + code + "$"):
        execute(github, pool, env="prod" if kind == "production" else "dev")
    assert pool.effects == []


def test_issue_change_after_preflight_does_not_enqueue(inputs, monkeypatch):
    _, _, github = inputs
    pool = FakePool()
    monkeypatch.setattr(admission, "validate", lambda *_: {"total": 42, "passed": 26})
    orig = github.get_issue
    hits = 0
    def issue(number):
        nonlocal hits
        hits += 1
        result = orig(number)
        return result if hits == 1 else dict(result, state="closed")
    github.get_issue = issue
    with pytest.raises(gate.HandoffBlocked, match="issue_identity_invalid"):
        execute(github, pool)
    assert hits == 2 and pool.effects == []


def test_enqueue_replay_requires_matching_readback(inputs, monkeypatch):
    _, _, github = inputs
    pool = FakePool()
    monkeypatch.setattr(admission, "validate", lambda *_: {"total": 42, "passed": 26})
    orig = pool.enqueue_fenced_issue
    def forged(payload):
        receipt = orig(payload)
        receipt["task"] = dict(receipt["task"], base_sha="e" * 40)
        return receipt
    pool.enqueue_fenced_issue = forged
    with pytest.raises(gate.HandoffBlocked, match="issue_pool_readback_invalid"):
        execute(github, pool)


def test_live_fastapi_sqlite_fenced_enqueue_and_readback(inputs, monkeypatch, tmp_path):
    """The pool is a real FastAPI+SQLite instance; GitHub remains synthetic."""
    binding, _, github = inputs
    token = tmp_path / "worker-token"
    reconcile = tmp_path / "reconciliation-token"
    token.write_text("worker-test", encoding="ascii")
    reconcile.write_text("independent-reconciliation-test-token", encoding="ascii")
    if os.name == "posix":
        token.chmod(0o600)
        reconcile.chmod(0o600)
    monkeypatch.setenv("CODEX_WORKER_POOL_DB", str(tmp_path / "pool.db"))
    monkeypatch.setenv("CODEX_WORKER_POOL_API_TOKEN_FILE", str(token))
    monkeypatch.setenv("CODEX_WORKER_POOL_RECONCILIATION_TOKEN_FILE", str(reconcile))
    monkeypatch.setenv("CODEX_WORKER_POOL_EXPECTED_RULES_SHA", "c" * 40)
    monkeypatch.setenv("CODEX_WORKER_POOL_FENCED_CUTOVER_ATTESTED", "0")
    sys.modules.pop("app.main", None)
    service = importlib.import_module("app.main")
    service.store.enable_fenced_cutover(
        correlation_id="issue-test-fenced-cutover",
        legacy_claimers_drained=True, shared_queue_exclusive=True,
    )
    with TestClient(service.app) as client:
        auth = {"Authorization": "Bearer worker-test"}
        lane = client.post("/v1/repositories", headers=auth,
                           json={"repository": gate.REPO, "enabled": True,
                                 "max_in_flight": 1,
                                 "correlation_id": "issue-test-lane"})
        assert lane.status_code == 200
        observed_calls = []
        def backend(request):
            observed_calls.append((request.method, request.url.path))
            response = client.request(
                request.method, request.url.path, headers=dict(request.headers),
                content=request.content if request.method != "GET" else None,
            )
            return httpx.Response(response.status_code,
                                  content=response.content,
                                  headers={"content-type": "application/json"})
        with httpx.Client(transport=httpx.MockTransport(backend),
                          trust_env=False, follow_redirects=False) as http:
            pool = LocalWorkerApi(port=8097, token_file=token, client=http)
            # The local test-client adapter overrides only credential retrieval;
            # authentication is still enforced by the *real* Worker Pool API.
            monkeypatch.setattr(pool, "_token", lambda: "worker-test")
            first = execute(github, pool)
            second = execute(github, pool)
        assert first["created"] is True and second["created"] is False
        assert first["task_id"] == second["task_id"]
        readback = client.get(f"/v1/tasks/{first['task_id']}", headers=auth)
        assert readback.status_code == 200
        data = readback.json()
        assert data["task_id"] == first["task_id"]
        assert data["base_sha"] == binding.base_sha
        assert data["fencing_required"] is True
        assert data["branch"] == binding.branch
        assert data["attempt_count"] == 0
        assert data["max_attempts"] == 1
        assert sum(method == "POST" and route == "/v1/tasks"
                   for method, route in observed_calls) == 2
        snapshot = client.get("/v1/snapshot", headers=auth).json()
        assert len(snapshot["tasks"]) == 1
        assert snapshot["tasks"][0]["task_id"] == first["task_id"]


def test_bounded_http_port_never_enqueues_without_fenced_payload(monkeypatch):
    http_calls = []
    def handler(request):
        http_calls.append(request.method)
        raise AssertionError("unauthorized transport")
    with httpx.Client(transport=httpx.MockTransport(handler),
                      trust_env=False, follow_redirects=False) as http:
        pool = LocalWorkerApi(port=8097, token_file=Path("/not-real"), client=http)
        for entry in [
            {"repository": gate.REPO},
            {"repository": gate.REPO, "fencing_required": False},
        ]:
            with pytest.raises(NativeExecutorBlocked, match="worker_issue_payload_invalid"):
                pool.enqueue_fenced_issue(entry)
    assert http_calls == []
