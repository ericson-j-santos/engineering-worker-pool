from __future__ import annotations

import hashlib
import io
import json
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.native_one_shot import (
    AdapterDescriptor,
    AdapterResult,
    DurableArtifact,
    ExecutionBoundary,
    LocalWorkspace,
    NativeExecutorBlocked,
    NativeOneShotExecutor,
    ReceiptStore,
)
from app.s3_git_bundle_artifact import (
    S3ArtifactConfig,
    S3GitBundleArtifactTransport,
)
from app.store import WorkerPoolStore

TASK = "cwp-" + "1" * 24
REPO = "ericson-j-santos/reqsys-v2-enterprise-real"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True,
                          check=True).stdout.strip()


def repository(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    git(root, "init", "-b", "worker/native-test")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "core.autocrlf", "false")
    (root / "src").mkdir()
    target = root / "src" / "task.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    (root / "outside.txt").write_text("outside\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    return root, target, git(root, "rev-parse", "HEAD")


class Api:
    def __init__(self, task):
        self.task, self.calls = dict(task), []

    def get_task(self, task_id):
        self.calls.append(("get", task_id))
        return dict(self.task)

    def claim_exact(self, worker_id, task_id, correlation_id):
        self.calls.append(("claim", worker_id, task_id))
        claimed = dict(self.task)
        claimed["state"] = "validating" if "validator" in worker_id else "leased"
        claimed["leased_by"] = worker_id
        return {"claimed": True, "task": claimed, "lease": {"task_id": task_id,
                "worker_id": worker_id,
                "role": "validator" if "validator" in worker_id else "builder",
                "lease_token": "fixture-lease-token",
                "lease_expires_at": (
                    datetime.now(UTC) + timedelta(minutes=5)).isoformat()}}

    def start(self, task_id, worker_id, lease_token, correlation_id):
        self.calls.append(("start", worker_id))

    def submit_validation(self, task_id, worker_id, lease_token, produced_sha,
                          correlation_id):
        self.calls.append(("validation", produced_sha))
        self.task.update(state="validating", produced_sha=produced_sha)

    def complete(self, task_id, worker_id, lease_token, correlation_id):
        self.calls.append(("complete", worker_id))
        self.task["state"] = "completed"

    def fail(self, task_id, worker_id, lease_token, correlation_id, reason):
        self.calls.append(("fail", reason))


class Adapter:
    descriptor = AdapterDescriptor("fixture.adapter.v1", ("src/task.py",),
                                   ("fixture.build",), ("fixture.validate",))

    def __init__(self, *, bad_path=False, bad_sha=False, interrupt=False, barrier=None):
        self.bad_path, self.bad_sha, self.interrupt, self.barrier = (
            bad_path, bad_sha, interrupt, barrier)
        self.calls = []

    def build(self, context):
        self.calls.append("build")
        if self.barrier:
            self.barrier.wait(timeout=3)
        context.invoke("fixture.build")
        if self.interrupt:
            raise NativeExecutorBlocked("fixture_interrupted")
        path = context.root / "src" / "task.py"
        path.write_text("VALUE = 2\n", encoding="utf-8")
        git(context.root, "add", ".")
        git(context.root, "commit", "-m", "fixture result")
        produced = "f" * 40 if self.bad_sha else git(context.root, "rev-parse", "HEAD")
        return AdapterResult(produced, ("other.py",) if self.bad_path else ("src/task.py",),
                             tuple(context.invoked), "a" * 64)

    def validate(self, context, produced_sha):
        self.calls.append("validate")
        context.invoke("fixture.validate")
        return AdapterResult("f" * 40 if self.bad_sha else produced_sha,
                             ("src/task.py",), tuple(context.invoked), "b" * 64)


def setup(tmp_path: Path, *, adapter=None, api=None, head=None, before=None,
          boundary=True):
    root, target, initial = repository(tmp_path)
    adapter = adapter or Adapter()
    manifest = {
        "schema_version": 1, "task_id": TASK, "repository": REPO,
        "base_sha": head or initial, "workspace_sha": head or initial,
        "adapter_id": adapter.descriptor.adapter_id,
        "adapter_contract_sha256": adapter.descriptor.contract_sha256,
        "before_sha256": {"src/task.py": before or hashlib.sha256(target.read_bytes()).hexdigest()},
    }
    task = {"task_id": TASK, "repository": REPO, "base_sha": manifest["base_sha"],
            "state": "queued", "produced_sha": None}
    api = api or Api(task)
    edge = ExecutionBoundary("native-test", "native-one-shot-test", manifest["base_sha"],
                             boundary, boundary)
    commands = {"fixture.build": lambda: "built", "fixture.validate": lambda: "valid"}
    executor = NativeOneShotExecutor(
        api=api, workspace=LocalWorkspace(root), receipts=ReceiptStore(tmp_path / "state"),
        adapters={adapter.descriptor.adapter_id: adapter},
        command_registries={adapter.descriptor.adapter_id: commands}, boundary=edge)
    return executor, manifest, api, adapter, root


def test_builder_then_validator_and_replay(tmp_path):
    executor, manifest, api, builder, root = setup(tmp_path)
    built = executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert built["result"] == "NATIVE_ONE_SHOT_BUILT"
    assert [call[0] for call in api.calls] == ["get", "claim", "start", "validation"]
    first_count = len(api.calls)
    assert executor.run(manifest, role="builder", worker_id="fixture-builder")["replayed"] is True
    assert len(api.calls) == first_count + 1
    assert api.calls[-1] == ("get", TASK) and builder.calls == ["build"]

    # Validator receives an independently materialized checkout at produced_sha.
    produced = built["produced_sha"]
    validator_root = tmp_path / "validator-repo"
    git(root, "worktree", "add", "--detach", str(validator_root), produced)
    manifest = {**manifest, "workspace_sha": produced,
                "before_sha256": {"src/task.py": hashlib.sha256(
                    (validator_root / "src" / "task.py").read_bytes()).hexdigest()}}
    api.task.update(produced_sha=produced, state="validating")
    validator = Adapter()
    manifest["adapter_contract_sha256"] = validator.descriptor.contract_sha256
    edge = ExecutionBoundary("native-test", "native-one-shot-test", produced, True, True)
    validate_executor = NativeOneShotExecutor(
        api=api, workspace=LocalWorkspace(validator_root), receipts=executor.receipts,
        adapters={validator.descriptor.adapter_id: validator},
        command_registries={validator.descriptor.adapter_id: {
            "fixture.build": lambda: None, "fixture.validate": lambda: "valid"}},
        boundary=edge)
    checked = validate_executor.run(manifest, role="validator", worker_id="fixture-validator")
    assert checked["result"] == "NATIVE_ONE_SHOT_VALIDATED"
    assert api.calls[-3:][0][0] == "get" and api.calls[-1] == ("complete", "fixture-validator")
    assert validator.calls == ["validate"]


@pytest.mark.parametrize("change,code", [
    ({"base_sha": "f" * 40}, "builder_workspace_not_base"),
    ({"workspace_sha": "f" * 40}, "boundary_manifest_drift"),
    ({"before_sha256": {"../task.py": "a" * 64}}, "manifest_path_traversal"),
    ({"adapter_contract_sha256": "f" * 64}, "adapter_contract_mismatch"),
])
def test_manifest_and_drift_block_before_api(tmp_path, change, code):
    executor, manifest, api, _adapter, _root = setup(tmp_path)
    manifest.update(change)
    with pytest.raises(NativeExecutorBlocked, match=code):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls == []


def test_head_file_remote_and_boundary_block_before_api(tmp_path):
    executor, manifest, api, _adapter, root = setup(tmp_path)
    (root / "src" / "task.py").write_text("DRIFT = 1\n", encoding="utf-8")
    with pytest.raises(NativeExecutorBlocked, match="workspace_dirty"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls == []

    git(root, "checkout", "--", "src/task.py")
    git(root, "remote", "add", "origin", "https://example.invalid/repo.git")
    with pytest.raises(NativeExecutorBlocked, match="workspace_remote_forbidden"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls == []

    bad, manifest2, api2, _adapter2, _root2 = setup(tmp_path / "other", boundary=False)
    with pytest.raises(NativeExecutorBlocked, match="governed_boundary_unproved"):
        bad.run(manifest2, role="builder", worker_id="fixture-builder")
    assert api2.calls == []


def test_result_path_sha_and_command_registry_fail_closed(tmp_path):
    for adapter, expected in ((Adapter(bad_path=True), "adapter_result_paths"),
                              (Adapter(bad_sha=True), "builder_produced_sha_drift")):
        area = tmp_path / expected
        executor, manifest, api, _adapter, _root = setup(area, adapter=adapter)
        with pytest.raises(NativeExecutorBlocked, match=expected):
            executor.run(manifest, role="builder", worker_id="fixture-builder")
        assert api.calls[-1][0] == "fail"

    executor, manifest, api, adapter, _root = setup(tmp_path / "registry")
    executor.command_registries[adapter.descriptor.adapter_id] = {
        "fixture.build": lambda: None, "remote.supplied": lambda: None}
    with pytest.raises(NativeExecutorBlocked, match="local_command_registry"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls == []


def test_role_cannot_invoke_other_roles_command(tmp_path):
    class WrongRoleCommand(Adapter):
        def build(self, context):
            context.invoke("fixture.validate")
            raise AssertionError("unreachable")

    adapter = WrongRoleCommand()
    executor, manifest, api, _adapter, _root = setup(tmp_path, adapter=adapter)
    with pytest.raises(NativeExecutorBlocked, match="command_not_allowlisted"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls[-1] == ("fail", "command_not_allowlisted")


def test_omitted_change_untracked_and_missing_required_command_fail(tmp_path):
    class Omitted(Adapter):
        def build(self, context):
            result = super().build(context)
            return AdapterResult(result.produced_sha, (), result.invoked_commands,
                                 result.evidence_sha256)

    class Untracked(Adapter):
        def build(self, context):
            result = super().build(context)
            (context.root / "rogue.txt").write_text("untracked\n", encoding="utf-8")
            return result

    class MissingCommand(Adapter):
        def build(self, context):
            path = context.root / "src" / "task.py"
            path.write_text("VALUE = 2\n", encoding="utf-8")
            git(context.root, "add", ".")
            git(context.root, "commit", "-m", "without required command")
            return AdapterResult(git(context.root, "rev-parse", "HEAD"),
                                 ("src/task.py",), (), "a" * 64)

    for name, adapter, expected in (
        ("omitted", Omitted(), "adapter_result_paths"),
        ("untracked", Untracked(), "builder_workspace_dirty"),
        ("missing-command", MissingCommand(), "required_command_missing"),
    ):
        executor, manifest, api, _adapter, _root = setup(tmp_path / name, adapter=adapter)
        with pytest.raises(NativeExecutorBlocked, match=expected):
            executor.run(manifest, role="builder", worker_id="fixture-builder")
        assert api.calls[-1] == ("fail", expected)


@pytest.mark.parametrize("operation", ["delete", "rename"])
def test_independent_diff_detects_omitted_delete_and_rename(tmp_path, operation):
    class HiddenChange(Adapter):
        def build(self, context):
            outside = context.root / "outside.txt"
            if operation == "delete":
                outside.unlink()
            else:
                outside.rename(context.root / "renamed.txt")
            return super().build(context)

    executor, manifest, api, _adapter, _root = setup(
        tmp_path, adapter=HiddenChange())
    with pytest.raises(NativeExecutorBlocked, match="builder_diff_mismatch"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls[-1] == ("fail", "builder_diff_mismatch")


@pytest.mark.parametrize("field,value", [
    ("session_id", "different-session"),
    ("correlation_id", "different-correlation"),
    ("gateway_verified", False),
    ("gateway_verified", 1),
    ("replayed", True),
    ("evidence_sha256", "f" * 63),
    ("changed_paths", []),
    ("changed_paths", ["outside.txt"]),
    ("changed_paths", ["src/task.py", "src/unknown.py"]),
    ("changed_paths", ["src/task.py", "src/task.py"]),
    ("invoked_commands", []),
    ("invoked_commands", ["fixture.validate"]),
])
def test_replay_rejects_tampered_receipt_without_claim(tmp_path, field, value):
    executor, manifest, api, adapter, _root = setup(tmp_path)
    built = executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert built["result"] == "NATIVE_ONE_SHOT_BUILT"

    receipt_path = executor.receipts.paths(TASK, "builder")[1]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt[field] = value
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    calls_before = list(api.calls)
    with pytest.raises(NativeExecutorBlocked, match="receipt_mismatch"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls == calls_before + [("get", TASK)]
    assert adapter.calls == ["build"]


def test_replay_receipt_cannot_move_to_distinct_checkout(tmp_path):
    executor, manifest, _api, adapter, root = setup(tmp_path)
    built = executor.run(manifest, role="builder", worker_id="fixture-builder")
    other = tmp_path / "copied-checkout"
    git(root, "worktree", "add", "--detach", str(other), built["produced_sha"])
    copied = NativeOneShotExecutor(
        api=executor.api, workspace=LocalWorkspace(other), receipts=executor.receipts,
        adapters={adapter.descriptor.adapter_id: adapter},
        command_registries={adapter.descriptor.adapter_id: {
            "fixture.build": lambda: None, "fixture.validate": lambda: None}},
        boundary=executor.boundary)
    with pytest.raises(NativeExecutorBlocked, match="receipt_mismatch"):
        copied.run(manifest, role="builder", worker_id="fixture-builder")


def test_claim_snapshot_race_blocks_before_adapter(tmp_path):
    executor, manifest, api, adapter, _root = setup(tmp_path)
    original = api.claim_exact

    def raced_claim(worker_id, task_id, correlation_id):
        claimed = original(worker_id, task_id, correlation_id)
        claimed["task"]["base_sha"] = "f" * 40
        return claimed

    api.claim_exact = raced_claim
    with pytest.raises(NativeExecutorBlocked, match="claimed_task_drift"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert adapter.calls == []


def test_interruption_fails_lease_without_receipt(tmp_path):
    adapter = Adapter(interrupt=True)
    executor, manifest, api, _adapter, _root = setup(tmp_path, adapter=adapter)
    with pytest.raises(NativeExecutorBlocked, match="adapter_failed"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls[-1] == ("fail", "adapter_failed")
    assert not executor.receipts.paths(TASK, "builder")[1].exists()


def test_concurrent_role_has_single_winner(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Slow(Adapter):
        def build(self, context):
            entered.set()
            assert release.wait(timeout=3)
            return super().build(context)

    adapter = Slow()
    executor, manifest, _api, _adapter, _root = setup(tmp_path, adapter=adapter)
    outcomes = []

    def first():
        outcomes.append(executor.run(manifest, role="builder", worker_id="fixture-builder"))

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(timeout=3)
    with pytest.raises(NativeExecutorBlocked, match="executor_busy"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    release.set()
    thread.join(timeout=5)
    assert len(outcomes) == 1


def test_task_drift_and_expired_or_invalid_lease_do_not_run_adapter(tmp_path):
    executor, manifest, api, adapter, _root = setup(tmp_path)
    api.task["base_sha"] = "f" * 40
    with pytest.raises(NativeExecutorBlocked, match="task_identity_drift"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert adapter.calls == [] and not any(call[0] == "claim" for call in api.calls)

    class Expired(Api):
        def claim_exact(self, worker_id, task_id, correlation_id):
            self.calls.append(("claim", worker_id, task_id))
            claimed = {**self.task, "state": "leased"}
            claimed["leased_by"] = worker_id
            return {"claimed": True, "task": claimed,
                    "lease": {"task_id": task_id, "worker_id": worker_id,
                    "role": "builder", "lease_token": "fixture-lease-token",
                    "lease_expires_at": (
                        datetime.now(UTC) - timedelta(seconds=1)).isoformat()}}

    executor2, manifest2, _api2, adapter2, _root2 = setup(
        tmp_path / "expired2")
    expired = Expired(dict(executor2.api.task))
    executor2.api = expired
    with pytest.raises(NativeExecutorBlocked, match="lease_invalid"):
        executor2.run(manifest2, role="builder", worker_id="fixture-builder")
    assert adapter2.calls == []


def test_real_store_builder_validator_transitions_and_replay(tmp_path):
    root, target, initial = repository(tmp_path)
    store = WorkerPoolStore(tmp_path / "pool.db", expected_rules_sha="a" * 40)
    for worker_id, role in (("native-builder", "builder"),
                            ("native-validator", "validator")):
        store.register_worker(
            worker_id=worker_id, host="isolated", role=role, profile="NORMAL",
            correlation_id=f"corr-{worker_id}", controller_version="test",
            rules_sha="a" * 40, gateway_ok=True, state_validated=True,
            worktree_root=str(root))
    queued, created = store.enqueue_task(
        repository=REPO, issue_number=1705, request_id="native-integration",
        correlation_id="corr-enqueue", priority=10, base_sha=initial)
    assert created is True

    class StoreApi:
        def get_task(self, task_id):
            return store.get_task(task_id)

        def claim_exact(self, worker_id, task_id, correlation_id):
            role = store.get_worker(worker_id)["role"]
            task, lease = store.claim_task(worker_id=worker_id, role=role,
                                           correlation_id=correlation_id,
                                           only_task_id=task_id)
            return {"claimed": task is not None, "task": task,
                    "lease": None if lease is None else {
                        "task_id": lease.task_id, "lease_token": lease.lease_token,
                        "worker_id": lease.worker_id, "role": lease.role,
                        "lease_expires_at": lease.lease_expires_at}}

        def start(self, task_id, worker_id, lease_token, correlation_id):
            store.start_task(task_id=task_id, worker_id=worker_id,
                             lease_token=lease_token, correlation_id=correlation_id)

        def submit_validation(self, task_id, worker_id, lease_token, produced_sha,
                              correlation_id):
            store.submit_for_validation(task_id=task_id, worker_id=worker_id,
                                        lease_token=lease_token, produced_sha=produced_sha,
                                        correlation_id=correlation_id)

        def complete(self, task_id, worker_id, lease_token, correlation_id):
            store.complete_task(task_id=task_id, worker_id=worker_id,
                                lease_token=lease_token, correlation_id=correlation_id)

        def fail(self, task_id, worker_id, lease_token, correlation_id, reason):
            store.fail_task(task_id=task_id, worker_id=worker_id,
                            lease_token=lease_token, correlation_id=correlation_id,
                            reason=reason)

    adapter = Adapter()
    manifest = {"schema_version": 1, "task_id": queued["task_id"],
                "repository": REPO, "base_sha": initial, "workspace_sha": initial,
                "adapter_id": adapter.descriptor.adapter_id,
                "adapter_contract_sha256": adapter.descriptor.contract_sha256,
                "before_sha256": {"src/task.py": hashlib.sha256(
                    target.read_bytes()).hexdigest()}}
    receipts = ReceiptStore(tmp_path / "receipts")
    commands = {"fixture.build": lambda: None, "fixture.validate": lambda: None}
    builder = NativeOneShotExecutor(
        api=StoreApi(), workspace=LocalWorkspace(root), receipts=receipts,
        adapters={adapter.descriptor.adapter_id: adapter},
        command_registries={adapter.descriptor.adapter_id: commands},
        boundary=ExecutionBoundary("native-test", "native-store-builder", initial,
                                   True, True))
    built = builder.run(manifest, role="builder", worker_id="native-builder")
    assert store.get_task(queued["task_id"])["state"] == "validating"
    assert builder.run(manifest, role="builder", worker_id="native-builder")["replayed"]

    validator_root = tmp_path / "validator-store"
    git(root, "worktree", "add", "--detach", str(validator_root), built["produced_sha"])
    validator_manifest = {**manifest, "workspace_sha": built["produced_sha"],
                          "before_sha256": {"src/task.py": hashlib.sha256(
                              (validator_root / "src" / "task.py").read_bytes()).hexdigest()}}
    validator_adapter = Adapter()
    validator = NativeOneShotExecutor(
        api=StoreApi(), workspace=LocalWorkspace(validator_root), receipts=receipts,
        adapters={validator_adapter.descriptor.adapter_id: validator_adapter},
        command_registries={validator_adapter.descriptor.adapter_id: commands},
        boundary=ExecutionBoundary("native-test", "native-store-validator",
                                   built["produced_sha"], True, True))
    validator.run(validator_manifest, role="validator", worker_id="native-validator")
    final = store.get_task(queued["task_id"])
    assert final["state"] == "completed" and final["lease_token"] is None


class FencedApi(Api):
    def __init__(self, task, *, recovery=None, reject_fail=False):
        self.recovery = recovery or {"latest_checkpoint": None, "effects": []}
        prior_generations = [
            item.get("lease_generation", 0)
            for item in [self.recovery["latest_checkpoint"], *self.recovery["effects"]]
            if type(item) is dict
        ]
        super().__init__({
            **task,
            "fencing_required": True,
            "lease_generation": max(prior_generations, default=0),
            "attempt_id": None,
        })
        self.reject_fail = reject_fail
        self.latest_checkpoint = self.recovery["latest_checkpoint"]

    def claim_exact(self, worker_id, task_id, correlation_id):
        raise AssertionError("legacy claim must not receive fenced tasks")

    def claim_fenced_exact(self, worker_id, task_id, correlation_id):
        generation = int(self.task["lease_generation"]) + 1
        attempt_id = f"attempt-{generation}"
        role = "validator" if "validator" in worker_id else "builder"
        claimed = {
            **self.task,
            "state": "validating" if role == "validator" else "leased",
            "leased_by": worker_id,
            "lease_generation": generation,
            "attempt_id": attempt_id,
        }
        self.task.update(claimed)
        self.calls.append(("fenced_claim", worker_id, generation, attempt_id))
        return {
            "claimed": True,
            "task": dict(claimed),
            "lease": {
                "task_id": task_id,
                "worker_id": worker_id,
                "role": role,
                "lease_token": "fixture-fenced-token",
                "lease_expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                "lease_generation": generation,
                "attempt_id": attempt_id,
            },
            "recovery_context": {
                "latest_checkpoint": self.latest_checkpoint,
                "effects": list(self.recovery["effects"]),
            },
        }

    def get_latest_checkpoint(self, task_id):
        self.calls.append(("checkpoint_get", task_id))
        return self.latest_checkpoint

    def start(self, task_id, worker_id, lease_token, correlation_id, **fence):
        self.calls.append(("start", fence))
        self.task["state"] = "running"

    def record_checkpoint(
        self, task_id, worker_id, lease_token, lease_generation, attempt_id,
        checkpoint_id, sequence, stage, next_action, correlation_id, **fields,
    ):
        checkpoint = {
            "task_id": task_id,
            "lease_generation": lease_generation,
            "attempt_id": attempt_id,
            "checkpoint_id": checkpoint_id,
            "sequence": sequence,
            "stage": stage,
            "next_action": next_action,
            "correlation_id": correlation_id,
            "artifact_kind": fields.get("artifact_kind"),
            "artifact_ref": fields.get("artifact_ref"),
            "artifact_sha256": fields.get("artifact_sha256"),
            "effect_id": fields.get("effect_id"),
            "effect_state": fields.get("effect_state", "none"),
            "effect_result_ref": fields.get("effect_result_ref"),
            "reconciliation_outcome": None,
        }
        self.latest_checkpoint = checkpoint
        if checkpoint["effect_id"] is not None:
            self.recovery["effects"] = [checkpoint]
        self.calls.append(("checkpoint", sequence, stage, dict(fields)))
        return checkpoint

    def submit_validation(self, task_id, worker_id, lease_token, produced_sha,
                          correlation_id, **fence):
        self.calls.append(("validation", produced_sha, fence))
        self.task.update(state="validating", produced_sha=produced_sha,
                         leased_by=None, attempt_id=None)

    def complete(self, task_id, worker_id, lease_token, correlation_id, **fence):
        self.calls.append(("complete", fence))
        self.task.update(state="completed", leased_by=None, attempt_id=None)

    def fail(self, task_id, worker_id, lease_token, correlation_id, reason, **fence):
        self.calls.append(("fail", reason, fence))
        if self.reject_fail:
            raise NativeExecutorBlocked("stale_failure_rejected")
        self.task.update(state="queued", leased_by=None, attempt_id=None)

    def block(self, task_id, worker_id, lease_token, correlation_id, reason, **fence):
        self.calls.append(("block", reason, fence))
        self.task.update(state="blocked", leased_by=None, attempt_id=None)


class ArtifactTransport:
    artifact = DurableArtifact("git_bundle.v1", "artifact://fixture/bundle", "c" * 64)

    def __init__(self, *, fail_publish=False, fail_preflight=False,
                 materialized_sha=None):
        self.calls = []
        self.fail_publish = fail_publish
        self.fail_preflight = fail_preflight
        self.materialized_sha = materialized_sha

    def preflight(self):
        self.calls.append(("preflight",))
        if self.fail_preflight:
            raise RuntimeError("private preflight failure")

    def publish(self, **kwargs):
        self.calls.append(("publish", kwargs["effect_id"], kwargs["result"].produced_sha))
        if self.fail_publish:
            raise RuntimeError("private publish failure")
        return self.artifact

    def verify(self, **kwargs):
        self.calls.append(("verify", kwargs["produced_sha"], kwargs["artifact"]))

    def materialize(self, **kwargs):
        self.calls.append(("materialize", kwargs["artifact"]))
        root = kwargs["workspace"].root
        path = root / "src" / "task.py"
        if self.materialized_sha is None:
            path.write_text("VALUE = 2\n", encoding="utf-8")
            git(root, "add", ".")
            git(root, "commit", "-m", "materialized fixture result")
        else:
            git(root, "checkout", "--detach", self.materialized_sha)
        return AdapterResult(
            git(root, "rev-parse", "HEAD"), ("src/task.py",),
            ("fixture.build",), "a" * 64,
        )


class InMemoryArtifactS3:
    """Minimal S3 surface for exercising the concrete transport in the executor."""

    def __init__(self):
        self.objects = {}
        self.put_calls = 0

    def put_object(self, **kwargs):
        assert kwargs["Bucket"] == "durable-artifacts"
        assert kwargs["IfNoneMatch"] == "*"
        self.put_calls += 1
        key = kwargs["Key"]
        if key in self.objects:
            error = RuntimeError("precondition failed")
            error.response = {
                "Error": {"Code": "PreconditionFailed"},
                "ResponseMetadata": {"HTTPStatusCode": 412},
            }
            raise error
        self.objects[key] = bytes(kwargs["Body"])
        return {}

    def get_object(self, **kwargs):
        assert kwargs["Bucket"] == "durable-artifacts"
        value = self.objects[kwargs["Key"]]
        return {"ContentLength": len(value), "Body": io.BytesIO(value)}


def recovered_builder_checkpoint(produced_sha, *, task_id=TASK):
    artifact = ArtifactTransport.artifact
    return {
        "task_id": task_id,
        "checkpoint_id": "native-builder-result-prior",
        "lease_generation": 1,
        "attempt_id": "attempt-1",
        "sequence": 2,
        "stage": "builder_result",
        "next_action": "submit_validation",
        **artifact.checkpoint_fields(),
        "effect_id": NativeOneShotExecutor._artifact_effect_id(TASK, produced_sha),
        "effect_state": "confirmed",
        "effect_result_ref": artifact.artifact_ref,
        "reconciliation_outcome": None,
        "reconciliation_readback_ref": None,
    }


def fenced_setup(tmp_path, *, adapter=None, recovery=None, reject_fail=False,
                 with_transport=True, attest_adapter=True):
    legacy_executor, manifest, legacy, adapter, root = setup(
        tmp_path, adapter=adapter,
    )
    api = FencedApi(legacy.task, recovery=recovery, reject_fail=reject_fail)
    transport = ArtifactTransport() if with_transport else None
    executor = NativeOneShotExecutor(
        api=api,
        workspace=legacy_executor.workspace,
        receipts=legacy_executor.receipts,
        adapters=legacy_executor.adapters,
        command_registries=legacy_executor.command_registries,
        boundary=legacy_executor.boundary,
        artifact_transport=transport,
        fenced_adapter_ids=(
            frozenset({adapter.descriptor.adapter_id})
            if attest_adapter else frozenset()
        ),
    )
    return executor, manifest, api, adapter, root, transport


def test_fenced_builder_requires_trusted_adapter_attestation_before_claim(tmp_path):
    executor, manifest, api, adapter, _root, transport = fenced_setup(
        tmp_path, attest_adapter=False,
    )
    with pytest.raises(NativeExecutorBlocked, match="fenced_adapter_not_attested"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls == [("get", TASK)]
    assert adapter.calls == []
    assert transport.calls == []


def test_fenced_builder_requires_configured_durable_transport_before_claim(tmp_path):
    executor, manifest, api, adapter, _root, _transport = fenced_setup(
        tmp_path, with_transport=False,
    )
    with pytest.raises(NativeExecutorBlocked, match="durable_artifact_transport_required"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls == [("get", TASK)]
    assert adapter.calls == []


def test_fenced_builder_preflights_durable_transport_before_claim(tmp_path):
    executor, manifest, api, adapter, _root, transport = fenced_setup(tmp_path)
    transport.fail_preflight = True

    with pytest.raises(
        NativeExecutorBlocked, match="durable_artifact_preflight_failed"
    ):
        executor.run(manifest, role="builder", worker_id="fixture-builder")

    assert api.calls == [("get", TASK)]
    assert adapter.calls == []
    assert transport.calls == [("preflight",)]


def test_fenced_builder_propagates_fence_and_checkpoints_artifact_effect(tmp_path):
    executor, manifest, api, adapter, _root, transport = fenced_setup(tmp_path)
    result = executor.run(manifest, role="builder", worker_id="fixture-builder")

    assert result["schema_version"] == 2
    assert result["fencing_required"] is True
    assert result["lease_generation"] == 1
    assert result["attempt_id"] == "attempt-1"
    assert result["artifact_ref"] == transport.artifact.artifact_ref
    assert adapter.calls == ["build"]
    assert [call[0] for call in transport.calls] == ["preflight", "publish", "verify"]
    checkpoints = [call for call in api.calls if call[0] == "checkpoint"]
    assert [(item[1], item[2], item[3]["effect_state"]) for item in checkpoints] == [
        (1, "artifact_publish", "pending"),
        (2, "builder_result", "confirmed"),
    ]
    start = next(call for call in api.calls if call[0] == "start")
    handoff = next(call for call in api.calls if call[0] == "validation")
    assert start[1] == handoff[2] == {
        "lease_generation": 1, "attempt_id": "attempt-1",
    }
    assert api.calls.index(checkpoints[-1]) < api.calls.index(handoff)

    before = len(api.calls)
    before_transport = len(transport.calls)
    replay = executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert replay["replayed"] is True
    assert api.calls[before:] == [("get", TASK), ("checkpoint_get", TASK)]
    assert transport.calls[before_transport:][0][0] == "verify"

    api.task["lease_generation"] = 2
    with pytest.raises(NativeExecutorBlocked, match="receipt_lease_stale"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")


def test_fenced_publish_ambiguity_records_unknown_and_blocks_without_fail(tmp_path):
    executor, manifest, api, adapter, _root, transport = fenced_setup(tmp_path)
    transport.fail_publish = True

    with pytest.raises(
        NativeExecutorBlocked, match="artifact_publish_ambiguous"
    ):
        executor.run(manifest, role="builder", worker_id="fixture-builder")

    assert adapter.calls == ["build"]
    checkpoints = [call for call in api.calls if call[0] == "checkpoint"]
    assert [call[3]["effect_state"] for call in checkpoints] == [
        "pending", "unknown",
    ]
    assert api.calls[-1][0:2] == ("block", "artifact_publish_ambiguous")
    assert not any(call[0] == "fail" for call in api.calls)
    assert api.task["state"] == "blocked"


def test_fenced_builder_resumes_confirmed_artifact_without_adapter_or_publish(tmp_path):
    executor, manifest, api, adapter, root, transport = fenced_setup(tmp_path)
    target = root / "src" / "task.py"
    target.write_text("VALUE = 2\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "prebuilt durable artifact")
    produced = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "--detach", manifest["base_sha"])
    checkpoint = recovered_builder_checkpoint(produced)
    api.recovery = {"latest_checkpoint": checkpoint, "effects": [checkpoint]}
    api.latest_checkpoint = checkpoint
    api.task["lease_generation"] = 1
    transport.materialized_sha = produced

    result = executor.run(manifest, role="builder", worker_id="fixture-builder")

    assert result["produced_sha"] == produced
    assert result["lease_generation"] == 2
    assert adapter.calls == []
    assert [call[0] for call in transport.calls] == [
        "preflight", "materialize", "verify",
    ]
    assert not any(call[0] == "publish" for call in transport.calls)
    assert api.task["state"] == "validating"


def test_fenced_concrete_s3_transport_resumes_in_independent_workspace(tmp_path):
    executor, manifest, api, adapter, root, _fixture_transport = fenced_setup(tmp_path)
    target_root = tmp_path / "portable-resume"
    git(tmp_path, "clone", "--quiet", "--no-hardlinks", str(root), str(target_root))
    git(target_root, "remote", "remove", "origin")
    git(target_root, "checkout", "--detach", manifest["workspace_sha"])
    assert (root / ".git").resolve() != (target_root / ".git").resolve()

    client = InMemoryArtifactS3()
    transport = S3GitBundleArtifactTransport(
        S3ArtifactConfig(
            bucket="durable-artifacts", region="us-east-2",
            key_prefix="worker/checkpoints",
        ),
        client,
    )
    executor.artifact_transport = transport
    built = executor.run(
        manifest, role="builder", worker_id="fixture-builder",
    )
    produced = built["produced_sha"]
    checkpoint = api.latest_checkpoint
    assert checkpoint["stage"] == "builder_result"
    assert checkpoint["effect_state"] == "confirmed"
    assert LocalWorkspace(target_root).head() == manifest["workspace_sha"]

    recovery = {"latest_checkpoint": checkpoint, "effects": [checkpoint]}
    resume_api = FencedApi(
        {
            "task_id": TASK,
            "repository": REPO,
            "base_sha": manifest["base_sha"],
            "state": "queued",
            "produced_sha": None,
        },
        recovery=recovery,
    )
    resume_adapter = Adapter()
    resume_executor = NativeOneShotExecutor(
        api=resume_api,
        workspace=LocalWorkspace(target_root),
        receipts=ReceiptStore(tmp_path / "portable-resume-state"),
        adapters={resume_adapter.descriptor.adapter_id: resume_adapter},
        command_registries={resume_adapter.descriptor.adapter_id: {
            "fixture.build": lambda: None,
            "fixture.validate": lambda: "valid",
        }},
        boundary=ExecutionBoundary(
            "portable-resume", "native-one-shot-test",
            manifest["workspace_sha"], True, True,
        ),
        artifact_transport=transport,
        fenced_adapter_ids=frozenset({resume_adapter.descriptor.adapter_id}),
    )

    resumed = resume_executor.run(
        manifest, role="builder", worker_id="replacement-builder",
    )

    assert resumed["produced_sha"] == produced
    assert resumed["lease_generation"] == 2
    assert resume_adapter.calls == []
    assert LocalWorkspace(target_root).head() == produced
    assert LocalWorkspace(target_root).status() == ""
    assert git(target_root, "remote") == ""
    assert len(client.objects) == 2
    assert client.put_calls == 3
    assert resume_api.task["state"] == "validating"


def test_fenced_validator_verifies_artifact_checkpoints_and_completes(tmp_path):
    builder_executor, manifest, api, _builder, root, transport = fenced_setup(tmp_path)
    built = builder_executor.run(
        manifest, role="builder", worker_id="fixture-builder"
    )
    produced = built["produced_sha"]
    validator_root = tmp_path / "fenced-validator-repo"
    git(root, "worktree", "add", "--detach", str(validator_root), produced)
    validator_manifest = {
        **manifest,
        "workspace_sha": produced,
        "before_sha256": {
            "src/task.py": hashlib.sha256(
                (validator_root / "src" / "task.py").read_bytes()
            ).hexdigest(),
        },
    }
    validator_adapter = Adapter()
    validator = NativeOneShotExecutor(
        api=api,
        workspace=LocalWorkspace(validator_root),
        receipts=builder_executor.receipts,
        adapters={validator_adapter.descriptor.adapter_id: validator_adapter},
        command_registries={validator_adapter.descriptor.adapter_id: {
            "fixture.build": lambda: None,
            "fixture.validate": lambda: "valid",
        }},
        boundary=ExecutionBoundary(
            "native-test", "native-one-shot-test", produced, True, True,
        ),
        artifact_transport=transport,
        fenced_adapter_ids=frozenset({validator_adapter.descriptor.adapter_id}),
    )

    result = validator.run(
        validator_manifest, role="validator", worker_id="fixture-validator"
    )

    assert result["result"] == "NATIVE_ONE_SHOT_VALIDATED"
    assert result["lease_generation"] == 2
    assert validator_adapter.calls == ["validate"]
    validator_checkpoint = next(
        call for call in api.calls
        if call[0] == "checkpoint" and call[2] == "validator_result"
    )
    assert validator_checkpoint[1] == 1
    complete = next(call for call in api.calls if call[0] == "complete")
    assert complete[1] == {"lease_generation": 2, "attempt_id": "attempt-2"}
    assert api.task["state"] == "completed"

    before = len(api.calls)
    replay = validator.run(
        validator_manifest, role="validator", worker_id="fixture-validator"
    )
    assert replay["replayed"] is True
    assert api.calls[before:] == [("get", TASK), ("checkpoint_get", TASK)]
    assert validator_adapter.calls == ["validate"]


def test_fenced_replay_requires_current_trust_and_canonical_artifact(tmp_path):
    executor, manifest, api, adapter, _root, transport = fenced_setup(tmp_path)
    executor.run(manifest, role="builder", worker_id="fixture-builder")
    receipt_path = executor.receipts.paths(TASK, "builder")[1]
    original = json.loads(receipt_path.read_text(encoding="utf-8"))

    executor.fenced_adapter_ids = frozenset()
    with pytest.raises(NativeExecutorBlocked, match="fenced_adapter_not_attested"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls[-1] == ("get", TASK)

    executor.fenced_adapter_ids = frozenset({adapter.descriptor.adapter_id})
    executor.artifact_transport = None
    with pytest.raises(
        NativeExecutorBlocked, match="durable_artifact_transport_required"
    ):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls[-1] == ("get", TASK)

    executor.artifact_transport = transport
    tampered = {**original, "artifact_ref": "artifact://fixture/tampered"}
    receipt_path.write_text(
        json.dumps(tampered, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    before_transport = len(transport.calls)
    with pytest.raises(NativeExecutorBlocked, match="receipt_checkpoint_drift"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls[-1] == ("checkpoint_get", TASK)
    assert len(transport.calls) == before_transport


@pytest.mark.parametrize("drift", ["task", "ledger"])
def test_fenced_recovery_rejects_cross_task_or_divergent_ledger(tmp_path, drift):
    checkpoint = recovered_builder_checkpoint("d" * 40)
    effect = dict(checkpoint)
    if drift == "task":
        checkpoint = {**checkpoint, "task_id": "cwp-" + "2" * 24}
        effect = dict(checkpoint)
    else:
        effect["checkpoint_id"] = "divergent-effect-checkpoint"
    recovery = {"latest_checkpoint": checkpoint, "effects": [effect]}
    executor, manifest, api, adapter, _root, transport = fenced_setup(
        tmp_path, recovery=recovery,
    )

    with pytest.raises(NativeExecutorBlocked, match="recovery_context_invalid"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert adapter.calls == []
    assert transport.calls == [("preflight",)]
    assert api.calls[-1][0:2] == ("block", "unsafe_recovery_context")


@pytest.mark.parametrize("effect_state,stage", [
    ("unknown", "external_effect"),
    ("pending", "external_effect"),
    ("confirmed", "external_effect"),
])
def test_fenced_recovery_blocks_unsafe_effect_before_adapter(tmp_path, effect_state, stage):
    effect = {
        "task_id": TASK,
        "checkpoint_id": "prior-effect-checkpoint",
        "lease_generation": 1,
        "attempt_id": "attempt-1",
        "sequence": 1,
        "effect_id": "external-effect",
        "effect_state": effect_state,
        "effect_result_ref": (
            "readback://external/effect" if effect_state == "confirmed" else None
        ),
        "reconciliation_outcome": None,
        "reconciliation_readback_ref": None,
        "stage": stage,
        "artifact_kind": None,
        "artifact_ref": None,
        "artifact_sha256": None,
        "next_action": "reconcile_external_effect",
    }
    recovery = {"latest_checkpoint": effect, "effects": [effect]}
    executor, manifest, api, adapter, _root, _transport = fenced_setup(
        tmp_path, recovery=recovery,
    )
    with pytest.raises(NativeExecutorBlocked, match="recovery_effect_unsafe"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert adapter.calls == []
    assert api.calls[-1][0:2] == ("block", "unsafe_recovery_context")


def test_fenced_reconciled_not_applied_blocks_before_start(tmp_path):
    latest = {
        "task_id": TASK,
        "checkpoint_id": "prior-artifact-publish",
        "lease_generation": 1,
        "attempt_id": "attempt-1",
        "sequence": 1,
        "effect_id": "prior-artifact-effect",
        "effect_state": "unknown",
        "effect_result_ref": None,
        "reconciliation_outcome": None,
        "reconciliation_readback_ref": None,
        "stage": "artifact_publish",
        "artifact_kind": None,
        "artifact_ref": None,
        "artifact_sha256": None,
        "next_action": "publish_artifact",
    }
    effect = {
        **latest,
        "reconciliation_outcome": "not_applied",
        "reconciliation_readback_ref": "readback://artifact/not-applied",
    }
    executor, manifest, api, adapter, _root, transport = fenced_setup(
        tmp_path,
        recovery={"latest_checkpoint": latest, "effects": [effect]},
    )

    with pytest.raises(
        NativeExecutorBlocked, match="recovery_effect_not_resumable"
    ):
        executor.run(manifest, role="builder", worker_id="fixture-builder")

    assert adapter.calls == []
    assert transport.calls == [("preflight",)]
    assert not any(call[0] in {"start", "fail"} for call in api.calls)
    assert api.calls[-1][0:2] == ("block", "unsafe_recovery_context")
    assert api.task["state"] == "blocked"


def test_stale_fail_rejection_does_not_mask_adapter_failure(tmp_path):
    executor, manifest, api, _adapter, _root, _transport = fenced_setup(
        tmp_path, adapter=Adapter(interrupt=True), reject_fail=True,
    )
    with pytest.raises(NativeExecutorBlocked, match="fixture_interrupted"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls[-1][0:2] == ("fail", "adapter_failed")


def test_stale_fail_rejection_preserves_closed_checkpoint_error(tmp_path):
    executor, manifest, api, _adapter, _root, _transport = fenced_setup(
        tmp_path, reject_fail=True,
    )

    def ambiguous_checkpoint(*_args, **_kwargs):
        raise NativeExecutorBlocked("worker_checkpoint_ambiguous")

    api.record_checkpoint = ambiguous_checkpoint
    with pytest.raises(NativeExecutorBlocked, match="worker_checkpoint_ambiguous"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls[-1][0:2] == ("fail", "adapter_failed")
