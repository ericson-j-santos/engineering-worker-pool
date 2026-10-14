from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.native_one_shot import (
    AdapterDescriptor,
    AdapterResult,
    ExecutionBoundary,
    LocalWorkspace,
    NativeExecutorBlocked,
    NativeOneShotExecutor,
    ReceiptStore,
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
    assert len(api.calls) == first_count and builder.calls == ["build"]

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
    assert api.calls == calls_before
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
