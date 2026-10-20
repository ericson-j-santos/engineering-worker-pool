"""Composition contract tests; the separate native harness supplies the real E2E.

The fakes here observe ordering and negative controls. They never count as a
Launcher/Gateway, S3, or shared-authority operational attestation.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app import native_runtime as runtime
from app.native_one_shot import ExecutionBoundary, NativeExecutorBlocked


BASE = "a" * 40
PRODUCED = "b" * 40
TASK = "cwp-" + "1" * 24
RULES = "c" * 40


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def composed(tmp_path, monkeypatch):
    events = []
    source = tmp_path / "source"
    target = tmp_path / "target"
    for root in (source, target):
        for relative, value in runtime.SYNTHETIC_FILES.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")
    launcher = ModuleType("session_launcher")
    launcher_file = tmp_path / "session_launcher.py"
    launcher_file.write_text("# trusted fixture launcher\n")
    launcher.__file__ = str(launcher_file)
    gateway = ModuleType("command_gateway")
    gateway_file = tmp_path / "command_gateway.py"
    gateway_file.write_text("# trusted fixture gateway\n")
    gateway.__file__ = str(gateway_file)
    launcher.cg = gateway
    policy = tmp_path / "policy.json"
    policy.write_text('{"fixture":true}')
    gateway.norm = lambda value: str(value)
    gateway.load_policy = lambda value: {"policy_file": str(value)}
    gateway.assert_allowed_path = lambda *args: events.append("allowed")
    state = SimpleNamespace(
        launch_update={}, fail_registry=False, registration_drift=False,
        invoke_command=False, fail_execution=False, closed=False,
    )

    def launch(**values):
        events.append("launcher")
        state.launch_args = values
        values["phase_callback"]("completed")
        return {
            "result": "SESSION_LAUNCH_OK", "state_validated": True,
            "session_id": values["session_id"],
            "correlation_id": values["correlation_id"],
            "head": values["expected_head"], "source_repo_root": str(source),
            "target_path": str(target), "snapshot_sha256": "d" * 64,
            **state.launch_update,
        }

    launcher.launch = launch

    def inspect(*args, **kwargs):
        events.append("inspect")
        return 0

    gateway.inspect = inspect

    class Registry:
        def __init__(self, **values):
            events.append("attest")
            state.registry_args = values
            if state.fail_registry:
                raise NativeExecutorBlocked("native_session_not_attested")
            self.specs = values["specs"]
            self.boundary = ExecutionBoundary(
                values["session_id"], values["correlation_id"],
                values["expected_head"], True, True,
            )

        def for_role(self, descriptor, role):
            for spec in self.specs.values():
                spec.validate(descriptor)

            def invoke(key):
                events.append("registry-command:" + key)
                return {"result": "GOVERNED_COMMAND_VERIFIED", "head": PRODUCED}

            return {key: (lambda name=key: invoke(name)) for key in self.specs}

    class Workspace:
        def __init__(self, root):
            self.root = root

        def validate_before_claim(self, manifest, descriptor):
            events.append("workspace-validate")

    class Api:
        def __init__(self, **values):
            events.append("api")
            state.api_args = values
            self.registered = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            state.closed = True
            events.append("close")

        def _request(self, method, route, payload=None):
            events.append(method + " " + route)
            if (method, route) == ("POST", "/v1/workers"):
                self.registered = dict(payload)
                return dict(payload)
            assert (method, route) == ("GET", "/v1/snapshot")
            worker = {**self.registered, "online": True}
            if state.registration_drift:
                worker["rules_sha"] = "e" * 40
            return {"workers": [worker]}

    class Executor:
        def __init__(self, **values):
            events.append("executor")
            state.executor_args = values
            self.values = values

        def run(self, manifest, *, role, worker_id):
            events.append("run")
            # The supervisor needs this before an interrupted run returns.
            evidence_path = self.values["receipts"].root / "session-evidence.json"
            assert evidence_path.is_file()
            state.evidence_before_run = json.loads(evidence_path.read_bytes())
            if state.invoke_command:
                commands = self.values["command_registries"][runtime.SYNTHETIC_PROFILE]
                commands["ci.fix"]()
            if state.fail_execution:
                raise NativeExecutorBlocked("fixture_execution_failure")
            return {"task_id": manifest["task_id"], "produced_sha": PRODUCED}

    def transport():
        events.append("transport")
        return "concrete-transport-fixture"

    monkeypatch.setattr(runtime, "NativeGatewayCommandRegistry", Registry)
    monkeypatch.setattr(runtime, "LocalWorkspace", Workspace)
    monkeypatch.setattr(runtime, "LocalWorkerApi", Api)
    monkeypatch.setattr(runtime, "NativeOneShotExecutor", Executor)
    monkeypatch.setattr(runtime.S3GitBundleArtifactTransport, "from_environment", transport)
    args = {
        "launcher": launcher, "policy_path": policy,
        "trusted_launcher_sha256": sha(launcher_file),
        "trusted_gateway_sha256": sha(gateway_file),
        "trusted_policy_sha256": sha(policy), "trusted_rules_sha": RULES,
        "source_repo": source,
        "manifest": runtime.synthetic_manifest(
            task_id=TASK, base_sha=BASE, workspace_sha=BASE, role="builder",
        ),
        "role": "builder", "worker_id": "worker-a",
        "session_id": "session-native-a",
        "correlation_id": "native-runtime-contract",
        "token_file": tmp_path / "private-token",
        "api_port": 18097, "receipt_root": tmp_path / "receipts",
        "runner_name": "GitHub Actions runner 123",
    }
    return args, state, events


@pytest.mark.parametrize("configured_timeout", [None, 90])
def test_composition_requires_attestation_before_provider_api_registration_and_run(
        composed, configured_timeout):
    args, state, events = composed
    if configured_timeout is not None:
        args["api_timeout_seconds"] = configured_timeout
    result = runtime.run_synthetic_once(**args)
    ordered = [
        "launcher", "inspect", "attest", "workspace-validate", "transport",
        "api", "POST /v1/workers", "GET /v1/snapshot", "executor", "run", "close",
    ]
    assert [item for item in events if item != "allowed"] == ordered
    assert state.launch_args["expected_head"] == BASE
    assert "sync_ref" not in state.launch_args
    assert state.api_args == {
        "port": 18097, "token_file": args["token_file"],
        "timeout_seconds": 8 if configured_timeout is None else configured_timeout,
    }
    assert state.executor_args["boundary"].expected_head == BASE
    assert state.executor_args["fenced_adapter_ids"] == frozenset({runtime.SYNTHETIC_PROFILE})
    assert result["runtime_command_count"] == 0
    assert result["runtime_commands_invoked"] == []
    assert state.closed


def test_session_evidence_exists_before_execution_is_private_and_canonical(composed):
    args, state, _ = composed
    result = runtime.run_synthetic_once(**args)
    path = args["receipt_root"] / "session-evidence.json"
    raw = path.read_bytes()
    expected = json.dumps(
        state.evidence_before_run, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    ).encode("ascii")
    assert raw == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert result["session_evidence"] == state.evidence_before_run
    assert result["session_evidence"]["product_cutover"] is False
    for forbidden in ("token", "credential", "secret", "target_path", "workspace_path"):
        assert forbidden not in raw.decode().lower()


def test_actual_registry_delegation_is_counted_separately_from_artifact_receipt(composed):
    args, state, events = composed
    state.invoke_command = True
    result = runtime.run_synthetic_once(**args)
    assert result["runtime_commands_invoked"] == ["ci.fix"]
    assert result["runtime_command_count"] == 1
    assert events.count("registry-command:ci.fix") == 1


@pytest.mark.parametrize("field,value", [
    ("repository", "unapproved/product"),
    ("adapter_id", "unapproved.profile"),
    ("before_sha256", {runtime.SOURCE_PATH: "f" * 64}),
])
def test_task_cannot_change_fixed_profile_before_any_action(composed, field, value):
    args, _, events = composed
    args["manifest"] = {**args["manifest"], field: value}
    with pytest.raises(NativeExecutorBlocked, match="native_runtime_profile_untrusted"):
        runtime.run_synthetic_once(**args)
    assert events == []


@pytest.mark.parametrize("field", [
    "trusted_launcher_sha256", "trusted_gateway_sha256", "trusted_policy_sha256",
])
def test_each_trusted_file_digest_is_checked_before_launcher(composed, field):
    args, _, events = composed
    args[field] = "0" * 64
    with pytest.raises(NativeExecutorBlocked, match="native_runtime_authority_changed"):
        runtime.run_synthetic_once(**args)
    assert events == []


def test_launcher_block_has_phase_and_does_not_contact_api_or_provider(composed):
    args, _, events = composed

    def blocked(**values):
        events.append("launcher")
        values["phase_callback"]("validate_session_source")
        raise RuntimeError("details are not exposed")

    args["launcher"].launch = blocked
    with pytest.raises(
        NativeExecutorBlocked, match="native_runtime_launcher_failed:validate_session_source",
    ):
        runtime.run_synthetic_once(**args)
    assert events == ["launcher"]


@pytest.mark.parametrize("update", [
    {"state_validated": False},
    {"head": PRODUCED},
    {"session_id": "different-session"},
    {"snapshot_sha256": None},
])
def test_incomplete_launcher_receipt_cannot_reach_registry(composed, update):
    args, state, events = composed
    state.launch_update = update
    with pytest.raises(NativeExecutorBlocked, match="native_runtime_launch_unverified"):
        runtime.run_synthetic_once(**args)
    assert events == ["launcher"]


def test_real_registry_attestation_failure_propagates_before_provider_or_api(composed):
    args, state, events = composed
    state.fail_registry = True
    with pytest.raises(NativeExecutorBlocked, match="native_session_not_attested"):
        runtime.run_synthetic_once(**args)
    assert events == ["launcher", "inspect", "attest"]


def test_worker_registration_must_be_read_back_before_evidence_or_run(composed):
    args, state, events = composed
    state.registration_drift = True
    with pytest.raises(NativeExecutorBlocked, match="native_runtime_registration_unverified"):
        runtime.run_synthetic_once(**args)
    assert "run" not in events
    assert not (args["receipt_root"] / "session-evidence.json").exists()
    assert state.closed


def test_api_closes_on_execution_failure_and_bootstrap_evidence_survives(composed):
    args, state, _ = composed
    state.fail_execution = True
    with pytest.raises(NativeExecutorBlocked, match="fixture_execution_failure"):
        runtime.run_synthetic_once(**args)
    assert state.closed
    assert (args["receipt_root"] / "session-evidence.json").is_file()


def test_changed_fixed_script_is_rejected_before_launcher(composed):
    args, _, events = composed
    (args["source_repo"] / "scripts/ci_fix.py").write_text("raise SystemExit(0)\n")
    with pytest.raises(NativeExecutorBlocked, match="native_runtime_fixture_drift"):
        runtime.run_synthetic_once(**args)
    assert events == []


def test_symlinked_source_is_rejected_before_launcher(composed):
    args, _, events = composed
    link = args["source_repo"].parent / "source-link"
    link.symlink_to(args["source_repo"], target_is_directory=True)
    args["source_repo"] = link
    with pytest.raises(NativeExecutorBlocked, match="native_runtime_path_invalid"):
        runtime.run_synthetic_once(**args)
    assert events == []


def test_manifest_factory_keeps_original_base_and_hashes_separate_from_validator():
    builder = runtime.synthetic_manifest(
        task_id=TASK, base_sha=BASE, workspace_sha=BASE, role="builder",
    )
    validator = runtime.synthetic_manifest(
        task_id=TASK, base_sha=BASE, workspace_sha=PRODUCED, role="validator",
    )
    assert builder["base_sha"] == validator["base_sha"] == BASE
    assert builder["before_sha256"] != validator["before_sha256"]
    assert builder["adapter_contract_sha256"] == validator["adapter_contract_sha256"]
    assert validator["workspace_sha"] == PRODUCED


def test_fixed_validation_script_uses_explicit_failure_even_with_optimization(tmp_path):
    # Compile-time optimization cannot remove the explicit semantic check.
    path = tmp_path / runtime.SOURCE_PATH
    path.parent.mkdir()
    path.write_text(runtime.ORIGINAL_SOURCE)
    previous = Path.cwd()
    try:
        os.chdir(tmp_path)
        code = compile(runtime.VALIDATE_SCRIPT, "<fixed-synthetic-validator>", "exec", optimize=2)
        with pytest.raises(SystemExit) as error:
            exec(code, {"__name__": "__main__"})
        assert error.value.code == 4
        path.write_text(runtime.REPAIRED_SOURCE)
        exec(code, {"__name__": "__main__"})
    finally:
        os.chdir(previous)


def test_recovered_builder_accepts_actual_execution_order_without_rewriting_it():
    from app.native_one_shot import AdapterResult, NativeOneShotExecutor
    executed = ("ci.fix", "ci.stage", "ci.commit")
    result = AdapterResult(PRODUCED, (runtime.SOURCE_PATH,), executed, "2" * 64)
    NativeOneShotExecutor._validate_recovered_result(result, runtime.SYNTHETIC_DESCRIPTOR)
    assert result.invoked_commands == executed


def test_recovered_builder_rejects_missing_extra_duplicate_or_malformed_commands():
    from app.native_one_shot import AdapterResult, NativeOneShotExecutor
    for commands in (
        ("ci.fix", "ci.stage"),
        ("ci.fix", "ci.stage", "ci.commit", "ci.other"),
        ("ci.fix", "ci.stage", "ci.commit", "ci.commit"),
        ("ci.fix", "ci.fix", "ci.commit"),
        ["ci.fix", "ci.stage", "ci.commit"],
        ("ci.fix", "ci.stage", None),
    ):
        result = AdapterResult(PRODUCED, (runtime.SOURCE_PATH,), commands, "2" * 64)
        with pytest.raises(NativeExecutorBlocked, match="^artifact_result_commands$"):
            NativeOneShotExecutor._validate_recovered_result(
                result, runtime.SYNTHETIC_DESCRIPTOR)
