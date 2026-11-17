"""Contract tests only. No physical Qwen, Gateway, CAS or model call occurs."""
from __future__ import annotations

import json
from types import ModuleType, SimpleNamespace

import pytest

from app.native_gateway_commands import NativeGatewayCommandRegistry
from app.native_one_shot import ExecutionBoundary, LocalWorkspace, NativeExecutorBlocked, ReceiptStore
from app.native_pool_http import LocalWorkerApi
from app.issue62_native_adapter import Issue62NativeAdapter
from app.store import identity, task_id
from app.issue62_typed_builder import (
    ISSUE62_DESCRIPTOR, TypedBuilderAction, execute_installed_builder_once,
)
from app.issue_native_adapter import DESCRIPTOR as REQSYS_ONLY_DESCRIPTOR

REPO = "ericson-j-santos/engineering-worker-pool"
BASE = "a" * 40
RULES = "b" * 40
DIGEST = "c" * 64
REQUEST = "issue62-native-builder-v1"
KEY = identity(REPO, 62, REQUEST)
TASK = task_id(KEY)
CORRELATION = "ewp62-native-stage-v1"
WORKER = "ewp-builder-62"


@pytest.fixture
def installed(monkeypatch, tmp_path):
    from app import issue62_typed_builder as mod

    action = TypedBuilderAction(
        task_id=TASK, repository=REPO, issue_number=62,
        request_id=REQUEST, idempotency_key=KEY,
        correlation_id=CORRELATION, todo_id="TODO-69",
        base_sha=BASE, rules_sha=RULES,
    )
    api = object.__new__(LocalWorkerApi)
    adapter = Issue62NativeAdapter(
        task_id=TASK, original=b"def decide_next_action(task, checkpoint):\\n    return None\\n",
        model_receipt_reader=lambda: {}, validation_receipt_reader=lambda: {},
        trusted_model_digest=DIGEST,
    )
    registry = object.__new__(NativeGatewayCommandRegistry)
    registry.gateway = ModuleType("mock_gateway")
    registry.policy_path = tmp_path / "policy.json"
    registry.workspace = tmp_path
    registry.session_id = "ewp62-session-v1"
    registry.correlation_id = CORRELATION
    registry.trusted_gateway_sha256 = "f" * 64
    registry.trusted_policy_sha256 = "e" * 64
    registry.boundary = ExecutionBoundary(
        session_id=registry.session_id, correlation_id=CORRELATION,
        expected_head=BASE, state_validated=True, gateway_verified=True,
    )
    workspace = LocalWorkspace(tmp_path)
    receipts = ReceiptStore(tmp_path / "receipts")
    canonical = {
        "task_id": TASK, "repository": REPO, "issue_number": 62,
        "request_id": REQUEST, "idempotency_key": KEY,
        "correlation_id": CORRELATION, "base_sha": BASE,
        "fencing_required": True, "state": "queued",
    }
    state = {"task": canonical, "checkpoint": None, "calls": 0,
             "cost_calls": 0, "gateway_calls": 0, "read_calls": 0}
    api.get_task = lambda task: _read_task(state, task)
    api.get_latest_checkpoint = lambda task: _read_cp(state, task)

    def attest(**kwargs):
        state["gateway_calls"] += 1
        return registry.boundary
    monkeypatch.setattr(mod, "attest_execution_boundary", attest)

    def cost(a):
        assert a is action
        state["cost_calls"] += 1

    def synthetic_bridge(_api, **kw):
        state["calls"] += 1
        assert _api is api and kw["role"] == "builder"
        assert kw["worker_id"] == WORKER and kw["task_id"] == TASK
        assert kw["registry"] is registry and kw["adapter"] is adapter
        assert kw["artifact_transport"] is artifact_transport
        adapter._verified_model_receipt = {
            "task_id": TASK, "base_sha": BASE, "model": "qwen2.5-coder:7b",
            "model_digest": DIGEST, "model_reply_sha256": "d" * 64,
            "model_calls": 1, "process_verified": True,
            "run_id": "attempt-0001",
        }
        state["task"] = {**canonical, "state": "validating",
                         "produced_sha": "9" * 40, "builder_worker_id": WORKER}
        state["checkpoint"] = {
            "task_id": TASK, "stage": "builder_result",
            "next_action": "submit_validation", "effect_state": "confirmed",
            "lease_generation": 1, "attempt_id": "attempt-0001",
            "artifact_ref": "cas/bundle/fixture", "artifact_sha256": "7" * 64,
        }
        receipt_path = receipts.paths(TASK, "builder")[1]
        receipt_path.write_text(json.dumps({
            "schema_version": 2, "result": "NATIVE_ONE_SHOT_BUILT",
            "task_id": TASK, "worker_id": WORKER,
            "session_id": registry.session_id, "correlation_id": CORRELATION,
            "gateway_verified": True, "replayed": False,
            "fencing_required": True, "attempt_id": "attempt-0001",
            "lease_generation": 1, "artifact_ref": "cas/bundle/fixture",
            "artifact_sha256": "7" * 64, "produced_sha": "9" * 40,
        }), encoding="utf-8")
        return {"task_id": TASK, "state": "validator",
                "executed": True, "executed_role": "builder"}

    artifact_transport = object()
    monkeypatch.setattr(mod, "continue_with_attested_native", synthetic_bridge)
    kwargs = dict(
        action=action, api=api, registry=registry, workspace=workspace,
        receipts=receipts, adapter=adapter, artifact_transport=artifact_transport,
        manifest={
            "schema_version": 1, "task_id": TASK, "repository": REPO,
            "base_sha": BASE, "workspace_sha": BASE,
            "adapter_id": ISSUE62_DESCRIPTOR.adapter_id,
            "adapter_contract_sha256": ISSUE62_DESCRIPTOR.contract_sha256,
            "before_sha256": {"app/next_action_bridge.py": "e" * 64},
        },
        worker_id=WORKER, installed_rules_sha=RULES,
        installed_model_digest=DIGEST, cost_preflight=cost,
    )
    return SimpleNamespace(action=action, adapter=adapter, state=state,
                           kwargs=kwargs, receipts=receipts)


def _read_task(state, task):
    assert task == TASK
    state["read_calls"] += 1
    return dict(state["task"])


def _read_cp(state, task):
    assert task == TASK
    return None if state["checkpoint"] is None else dict(state["checkpoint"])


def test_typed_builder_contract_positive_returns_only_native_evidence(installed):
    reply = execute_installed_builder_once(**installed.kwargs)
    assert reply == {
        "result": "TYPED_NATIVE_BUILDER_CONFIRMED", "task_id": TASK,
        "native_run_id": "attempt-0001",
        "model": "qwen2.5-coder:7b", "model_digest": DIGEST,
        "model_reply_sha256": "d" * 64, "produced_sha": "9" * 40,
        "artifact_ref": "cas/bundle/fixture", "artifact_sha256": "7" * 64,
        "session_id": "ewp62-session-v1", "validator_pending": True,
    }
    assert installed.state["cost_calls"] == 1
    assert installed.state["gateway_calls"] == 1
    assert installed.state["calls"] == 1


@pytest.mark.parametrize("changed,reason", [
    ({"todo_id": "TODO-OTHER"}, "typed_builder_identity_invalid"),
    ({"issue_number": 63}, "typed_builder_identity_invalid"),
    ({"idempotency_key": "1" * 64}, "typed_builder_idempotency_invalid"),
    ({"task_id": "cwp-" + "0" * 24}, "typed_builder_idempotency_invalid"),
    ({"request_id": "bad/command"}, "typed_builder_identity_invalid"),
    ({"rules_sha": "garbage"}, "typed_builder_identity_invalid"),
    ({"correlation_id": "x"}, "typed_builder_identity_invalid"),
])
def test_wrong_action_blocks_before_cost_session_and_api(installed, changed, reason):
    from dataclasses import replace
    action = replace(installed.action, **changed)
    with pytest.raises(NativeExecutorBlocked, match=reason):
        execute_installed_builder_once(**{**installed.kwargs, "action": action})
    assert installed.state["cost_calls"] == 0
    assert installed.state["gateway_calls"] == 0
    assert installed.state["read_calls"] == 0
    assert installed.state["calls"] == 0


@pytest.mark.parametrize("key,value", [
    ("installed_rules_sha", "1" * 40),
    ("installed_model_digest", "invalid-digest"),
    ("worker_id", "bad/worker"),
    ("cost_preflight", None),
    ("api", object()),
    ("registry", object()),
])
def test_untrusted_installed_components_block_before_cost_and_api(installed, key, value):
    kwargs = {**installed.kwargs, key: value}
    with pytest.raises(NativeExecutorBlocked):
        execute_installed_builder_once(**kwargs)
    assert installed.state["read_calls"] == 0
    assert installed.state["calls"] == 0


def test_stale_model_cannot_be_reused(installed):
    installed.adapter._verified_model_receipt = {"run_id": "stale"}
    with pytest.raises(NativeExecutorBlocked, match="typed_builder_model_not_fresh"):
        execute_installed_builder_once(**installed.kwargs)
    assert installed.state["calls"] == 0


def test_cost_failure_prevents_gateway_and_api(installed):
    def refused(_action):
        raise NativeExecutorBlocked("cost_unknown")
    with pytest.raises(NativeExecutorBlocked, match="cost_unknown"):
        execute_installed_builder_once(**{**installed.kwargs, "cost_preflight": refused})
    assert installed.state["gateway_calls"] == 0
    assert installed.state["read_calls"] == 0
    assert installed.state["calls"] == 0


def test_cost_boolean_success_is_not_accepted(installed):
    with pytest.raises(NativeExecutorBlocked, match="typed_builder_cost_preflight_invalid"):
        execute_installed_builder_once(**{
            **installed.kwargs, "cost_preflight": lambda _: True})
    assert installed.state["gateway_calls"] == 0


def test_gateway_drift_blocks_before_api_and_model(monkeypatch, installed):
    from app import issue62_typed_builder as mod
    monkeypatch.setattr(mod, "attest_execution_boundary", lambda **kw:
                        ExecutionBoundary("other-session", CORRELATION, BASE, True, True))
    with pytest.raises(NativeExecutorBlocked, match="typed_builder_session_changed"):
        execute_installed_builder_once(**installed.kwargs)
    assert installed.state["read_calls"] == 0
    assert installed.state["calls"] == 0


def test_canonical_idempotency_drift_blocks_claim(installed):
    installed.state["task"] = {**installed.state["task"],
                               "idempotency_key": "2" * 64}
    with pytest.raises(NativeExecutorBlocked, match="typed_builder_canonical_identity_drift"):
        execute_installed_builder_once(**installed.kwargs)
    assert installed.state["calls"] == 0


def test_success_without_local_model_proof_fails_closed(monkeypatch, installed):
    from app import issue62_typed_builder as mod
    def fake_bridge(*args, **kw):
        installed.state["calls"] += 1
        return {"task_id": TASK, "state": "validator",
                "executed": True, "executed_role": "builder"}
    monkeypatch.setattr(mod, "continue_with_attested_native", fake_bridge)
    with pytest.raises(NativeExecutorBlocked, match="issue62_model_receipt_not_available"):
        execute_installed_builder_once(**installed.kwargs)
    assert installed.state["calls"] == 1


@pytest.mark.parametrize("field,bad", [
    ("artifact_sha256", "0" * 64),
    ("attempt_id", "attempt-foreign"),
    ("produced_sha", "0" * 40),
    ("session_id", "session-foreign"),
    ("gateway_verified", False),
])
def test_tampered_receipt_fails_cas_readback(installed, field, bad):
    from app import issue62_typed_builder as mod
    original = mod.continue_with_attested_native
    def altered(*args, **kw):
        result = original(*args, **kw)
        path = installed.receipts.paths(TASK, "builder")[1]
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt[field] = bad
        path.write_text(json.dumps(receipt), encoding="utf-8")
        return result
    from _pytest.monkeypatch import MonkeyPatch
    with MonkeyPatch.context() as mp:
        mp.setattr(mod, "continue_with_attested_native", altered)
        with pytest.raises(NativeExecutorBlocked, match="typed_builder_cas_readback_unverified"):
            execute_installed_builder_once(**installed.kwargs)


@pytest.mark.parametrize("profile", [
    REQSYS_ONLY_DESCRIPTOR,
    SimpleNamespace(adapter_id="issue62.next-action.v1"),
])
def test_reqsys_or_fake_adapter_rejected_before_cost_or_gateway(installed, profile):
    installed.kwargs["adapter"].descriptor = profile
    with pytest.raises(
        NativeExecutorBlocked, match="typed_builder_adapter_profile_incompatible",
    ):
        execute_installed_builder_once(**installed.kwargs)
    assert installed.state["cost_calls"] == 0
    assert installed.state["gateway_calls"] == 0
    assert installed.state["read_calls"] == 0
    assert installed.state["calls"] == 0


@pytest.mark.parametrize("field,wrong", [
    ("repository", "ericson-j-santos/reqsys-v2-enterprise-real"),
    ("adapter_id", REQSYS_ONLY_DESCRIPTOR.adapter_id),
    ("adapter_contract_sha256", "f" * 64),
    ("base_sha", "8" * 40),
    ("workspace_sha", "9" * 40),
    ("before_sha256", {"src/example.py": "e" * 64}),
])
def test_foreign_task_manifest_fails_closed_before_any_native_effect(
    installed, field, wrong,
):
    installed.kwargs["manifest"] = {**installed.kwargs["manifest"], field: wrong}
    with pytest.raises(NativeExecutorBlocked, match="typed_builder_adapter_profile_incompatible"):
        execute_installed_builder_once(**installed.kwargs)
    assert installed.state["cost_calls"] == 0
    assert installed.state["gateway_calls"] == 0
    assert installed.state["read_calls"] == 0
    assert installed.state["calls"] == 0


def test_manifest_is_not_optional_and_has_exact_version(installed):
    for manifest in (
        {k: v for k, v in installed.kwargs["manifest"].items() if k != "schema_version"},
        {**installed.kwargs["manifest"], "schema_version": 2},
    ):
        with pytest.raises(NativeExecutorBlocked):
            execute_installed_builder_once(**{**installed.kwargs, "manifest": manifest})
    assert installed.state["cost_calls"] == 0
    assert installed.state["gateway_calls"] == 0
    assert installed.state["calls"] == 0
