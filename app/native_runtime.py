"""Compose the fixed synthetic native profile with the real governance boundary.

This module is an infrastructure canary, not a product adapter. The caller must
enter through an already governed, versioned entry point and provide the trusted
installed rules bundle and a prepared, remote-free synthetic Git source. Tasks
cannot choose a module, command, endpoint, credential, or adapter implementation.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
from contextlib import redirect_stdout
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, Mapping

from app.native_gateway_commands import GovernedCommand, NativeGatewayCommandRegistry
from app.native_one_shot import (
    AdapterContext, AdapterDescriptor, AdapterResult, LocalWorkspace,
    NativeExecutorBlocked, NativeOneShotExecutor, ReceiptStore, TaskManifest,
)
from app.native_pool_http import LocalWorkerApi
from app.s3_git_bundle_artifact import S3GitBundleArtifactTransport
from app.secure_file import open_exclusive


SYNTHETIC_REPOSITORY = "ci-isolated/native-fixture"
SYNTHETIC_PROFILE = "ci.native.patch.v1"
SOURCE_PATH = "src/calculator.py"
ORIGINAL_SOURCE = "def add(a, b):\n    return a - b\n"
REPAIRED_SOURCE = "def add(a, b):\n    return a + b\n"
FIX_SCRIPT = (
    "from pathlib import Path\n"
    "p = Path('src/calculator.py')\n"
    "before = 'def add(a, b):\\n    return a - b\\n'\n"
    "after = 'def add(a, b):\\n    return a + b\\n'\n"
    "if p.read_text(encoding='utf-8') != before:\n"
    "    raise SystemExit(3)\n"
    "p.write_bytes(after.encode('utf-8'))\n"
)
VALIDATE_SCRIPT = (
    "from pathlib import Path\n"
    "import runpy\n"
    "p = Path('src/calculator.py')\n"
    "fun = runpy.run_path(str(p))['add']\n"
    "if fun(2, 3) != 5 or fun(-5, 7) != 2:\n"
    "    raise SystemExit(4)\n"
)
SYNTHETIC_FILES: Mapping[str, str] = MappingProxyType({
    SOURCE_PATH: ORIGINAL_SOURCE,
    "scripts/ci_fix.py": FIX_SCRIPT,
    "scripts/ci_validate.py": VALIDATE_SCRIPT,
})
SYNTHETIC_DESCRIPTOR = AdapterDescriptor(
    SYNTHETIC_PROFILE, (SOURCE_PATH,),
    ("ci.commit", "ci.fix", "ci.stage"), ("ci.validate",),
)
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise NativeExecutorBlocked(code)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def synthetic_manifest(
    *, task_id: str, base_sha: str, workspace_sha: str, role: str,
) -> dict[str, Any]:
    """Reconstruct the original trusted definition; never infer it from A's disk."""
    _need(role in {"builder", "validator"}, "native_runtime_role_invalid")
    _need(role != "builder" or base_sha == workspace_sha,
          "builder_workspace_not_base")
    raw = {
        "schema_version": 1, "task_id": task_id,
        "repository": SYNTHETIC_REPOSITORY, "base_sha": base_sha,
        "workspace_sha": workspace_sha, "adapter_id": SYNTHETIC_PROFILE,
        "adapter_contract_sha256": SYNTHETIC_DESCRIPTOR.contract_sha256,
        "before_sha256": {
            SOURCE_PATH: _sha(ORIGINAL_SOURCE if role == "builder" else REPAIRED_SOURCE),
        },
    }
    TaskManifest.parse(raw)
    return raw


def _physical(path: Path, *, directory: bool = False) -> Path:
    value = Path(path)
    try:
        _need(value.is_absolute() and value.resolve(strict=True) == value
              and not value.is_symlink(), "native_runtime_path_invalid")
        if directory:
            _need(value.is_dir(), "native_runtime_path_invalid")
        else:
            state = value.stat()
            _need(stat.S_ISREG(state.st_mode) and state.st_nlink == 1,
                  "native_runtime_file_invalid")
    except OSError:
        raise NativeExecutorBlocked("native_runtime_path_unavailable") from None
    return value


def _file_hash(path: Path) -> str:
    value = _physical(path)
    try:
        return hashlib.sha256(value.read_bytes()).hexdigest()
    except OSError:
        raise NativeExecutorBlocked("native_runtime_file_unavailable") from None


def _authority(
    launcher: ModuleType, policy_path: Path, launcher_sha: str,
    gateway_sha: str, policy_sha: str,
) -> tuple[ModuleType, Path]:
    _need(type(launcher) is ModuleType and launcher.__name__ == "session_launcher"
          and type(getattr(launcher, "__file__", None)) is str
          and callable(getattr(launcher, "launch", None)),
          "native_runtime_launcher_untrusted")
    gateway = getattr(launcher, "cg", None)
    _need(type(gateway) is ModuleType and gateway.__name__ == "command_gateway"
          and type(getattr(gateway, "__file__", None)) is str,
          "native_runtime_gateway_untrusted")
    _need(all(type(value) is str and HEX64.fullmatch(value)
              for value in (launcher_sha, gateway_sha, policy_sha)),
          "native_runtime_digest_invalid")
    policy_file = _physical(Path(policy_path))
    _need(_file_hash(Path(launcher.__file__)) == launcher_sha
          and _file_hash(Path(gateway.__file__)) == gateway_sha
          and _file_hash(policy_file) == policy_sha,
          "native_runtime_authority_changed")
    return gateway, policy_file


def _specs() -> dict[str, GovernedCommand]:
    return {
        "ci.fix": GovernedCommand(
            "ci.fix", "builder", ("python", "scripts/ci_fix.py"), 2,
            script_sha256=_sha(FIX_SCRIPT),
        ),
        "ci.stage": GovernedCommand(
            "ci.stage", "builder", ("git", "add", "--", SOURCE_PATH), 2,
            allow_dirty=True,
        ),
        "ci.commit": GovernedCommand(
            "ci.commit", "builder",
            ("git", "commit", "-m", "fix(ci-fixture): restore addition"), 2,
            allow_dirty=True, allow_head_change=True,
        ),
        "ci.validate": GovernedCommand(
            "ci.validate", "validator", ("python", "scripts/ci_validate.py"), 1,
            script_sha256=_sha(VALIDATE_SCRIPT),
        ),
    }


class SyntheticAdapter:
    """The implementation and command hashes are code-owned, never task-owned."""

    descriptor = SYNTHETIC_DESCRIPTOR

    def build(self, context: AdapterContext) -> AdapterResult:
        target = context.root / SOURCE_PATH
        _need(target.read_text(encoding="utf-8") == ORIGINAL_SOURCE,
              "native_runtime_baseline_drift")
        context.invoke("ci.fix")
        context.invoke("ci.stage")
        command = context.invoke("ci.commit")
        _need(type(command) is dict
              and command.get("result") == "GOVERNED_COMMAND_VERIFIED"
              and type(command.get("head")) is str
              and bool(HEX40.fullmatch(command["head"])),
              "native_runtime_commit_unverified")
        _need(target.read_text(encoding="utf-8") == REPAIRED_SOURCE,
              "native_runtime_patch_not_applied")
        return AdapterResult(
            command["head"], (SOURCE_PATH,), tuple(context.invoked),
            _file_hash(target),
        )

    def validate(self, context: AdapterContext, produced_sha: str) -> AdapterResult:
        context.invoke("ci.validate")
        target = context.root / SOURCE_PATH
        _need(target.read_text(encoding="utf-8") == REPAIRED_SOURCE,
              "native_runtime_validation_drift")
        return AdapterResult(
            produced_sha, (SOURCE_PATH,), tuple(context.invoked), _file_hash(target),
        )


def _register_attested_worker(
    api: LocalWorkerApi, *, worker_id: str, runner_name: str, role: str,
    trusted_rules_sha: str, workspace: Path, registry: NativeGatewayCommandRegistry,
) -> None:
    boundary = registry.boundary
    boundary.validate()
    payload = {
        "worker_id": worker_id, "host": runner_name, "role": role,
        "profile": "NORMAL", "capacity_score": 50,
        "rules_sha": trusted_rules_sha,
        "gateway_ok": boundary.gateway_verified,
        "state_validated": boundary.state_validated,
        "worktree_root": str(workspace),
        "correlation_id": boundary.correlation_id,
    }
    # Reuse the single fixed-loopback, credential-safe transport implementation.
    # This is a fixed API route; it does not accept a caller-supplied URL/client.
    registered = api._request("POST", "/v1/workers", payload)
    _need(all(registered.get(key) == value for key, value in payload.items()),
          "native_runtime_registration_unverified")
    snapshot = api._request("GET", "/v1/snapshot")
    workers = snapshot.get("workers")
    _need(type(workers) is list, "native_runtime_registration_unverified")
    matches = [item for item in workers
               if type(item) is dict and item.get("worker_id") == worker_id]
    _need(len(matches) == 1
          and all(matches[0].get(key) == value for key, value in payload.items())
          and matches[0].get("online") is True,
          "native_runtime_registration_unverified")


def run_synthetic_once(
    *, launcher: ModuleType, policy_path: Path,
    trusted_launcher_sha256: str, trusted_gateway_sha256: str,
    trusted_policy_sha256: str, trusted_rules_sha: str,
    source_repo: Path, manifest: dict[str, Any], role: str,
    worker_id: str, session_id: str, correlation_id: str,
    token_file: Path, api_port: int, receipt_root: Path, runner_name: str,
    api_timeout_seconds: float = 8,
) -> dict[str, Any]:
    """Launch, inspect, attest, register, and run one fixed synthetic native role.

    Local API/Controller provisioning and the durable authority are external.
    Both hosts point their local API at the same persistent database. The S3
    provider uses its existing credential chain; credentials never enter this
    function's arguments or its receipt. The task definition comes from C.
    """
    parsed = TaskManifest.parse(manifest)
    _need(manifest == synthetic_manifest(
        task_id=parsed.task_id, base_sha=parsed.base_sha,
        workspace_sha=parsed.workspace_sha, role=role,
    ), "native_runtime_profile_untrusted")
    _need(type(worker_id) is str and bool(NAME.fullmatch(worker_id))
          and type(session_id) is str
          and bool(re.fullmatch(r"[A-Za-z0-9._-]{3,64}", session_id))
          and type(correlation_id) is str
          and bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{7,127}", correlation_id)),
          "native_runtime_identity_invalid")
    _need(type(runner_name) is str and 1 <= len(runner_name) <= 128
          and runner_name == runner_name.strip()
          and all(32 <= ord(char) < 127 for char in runner_name),
          "native_runtime_runner_invalid")
    _need(type(trusted_rules_sha) is str and bool(HEX40.fullmatch(trusted_rules_sha)),
          "native_runtime_rules_invalid")
    _need(type(api_port) is int and 1 <= api_port <= 65535,
          "worker_port_invalid")
    _need(type(api_timeout_seconds) in (int, float)
          and 1 <= api_timeout_seconds <= 120, "worker_timeout_invalid")
    source = _physical(Path(source_repo), directory=True)
    gateway, policy_file = _authority(
        launcher, policy_path, trusted_launcher_sha256,
        trusted_gateway_sha256, trusted_policy_sha256,
    )
    # These are reads only, before the first technical action (the Launcher).
    expected_files = dict(SYNTHETIC_FILES)
    expected_files[SOURCE_PATH] = ORIGINAL_SOURCE if role == "builder" else REPAIRED_SOURCE
    for relative, expected in expected_files.items():
        _need(_file_hash(source / relative) == _sha(expected),
              "native_runtime_fixture_drift")

    phase = "launch"
    def record_phase(value: str) -> None:
        nonlocal phase
        if type(value) is str and re.fullmatch(r"[a-z_]{1,64}", value):
            phase = value

    try:
        with redirect_stdout(io.StringIO()):
            launch = launcher.launch(
                repo=source, policy_path=policy_file, session_id=session_id,
                session_prefix=None, correlation_id=correlation_id,
                expected_head=parsed.workspace_sha, phase_callback=record_phase,
            )
    except Exception:
        raise NativeExecutorBlocked("native_runtime_launcher_failed:" + phase) from None
    _need(type(launch) is dict
          and launch.get("result") == "SESSION_LAUNCH_OK"
          and launch.get("state_validated") is True
          and launch.get("session_id") == session_id
          and launch.get("correlation_id") == correlation_id
          and launch.get("head") == parsed.workspace_sha
          and launch.get("source_repo_root") == gateway.norm(source)
          and type(launch.get("snapshot_sha256")) is str
          and bool(HEX64.fullmatch(launch["snapshot_sha256"]))
          and type(launch.get("target_path")) is str,
          "native_runtime_launch_unverified")
    workspace = _physical(Path(launch["target_path"]), directory=True)
    _need(workspace != source, "native_runtime_workspace_not_materialized")
    policy = gateway.load_policy(policy_file)
    with redirect_stdout(io.StringIO()):
        inspected = gateway.inspect(
            workspace, policy, correlation_id=correlation_id, session_id=session_id,
        )
    _need(inspected == 0, "native_runtime_inspection_failed")
    # The constructor independently reads the live Git state, reserved worktree,
    # digest-validated snapshot, and fresh inspect event. No caller sets booleans.
    registry = NativeGatewayCommandRegistry(
        gateway=gateway, policy_path=policy_file, workspace=workspace,
        session_id=session_id, correlation_id=correlation_id,
        expected_head=parsed.workspace_sha,
        trusted_gateway_sha256=trusted_gateway_sha256,
        trusted_policy_sha256=trusted_policy_sha256, specs=_specs(),
    )
    adapter = SyntheticAdapter()
    command_impls = registry.for_role(adapter.descriptor, role)
    runtime_commands: list[str] = []
    def count_command(key: str):
        def invoke():
            runtime_commands.append(key)
            return command_impls[key]()
        return invoke
    commands = {key: count_command(key) for key in command_impls}
    receipts = Path(receipt_root)
    _need(receipts.is_absolute() and receipts.resolve() == receipts
          and not receipts.is_symlink(), "native_runtime_receipt_path_invalid")
    gateway.assert_allowed_path(receipts, policy)
    local_workspace = LocalWorkspace(workspace)
    # Preserve the existing no-remotes/content guards before registration too.
    local_workspace.validate_before_claim(parsed, adapter.descriptor)
    transport = S3GitBundleArtifactTransport.from_environment()
    with LocalWorkerApi(port=api_port, token_file=Path(token_file),
                        timeout_seconds=api_timeout_seconds) as api:
        _register_attested_worker(
            api, worker_id=worker_id, runner_name=runner_name, role=role,
            trusted_rules_sha=trusted_rules_sha, workspace=workspace, registry=registry,
        )
        receipt_store = ReceiptStore(receipts)
        evidence = {
            "session_id": registry.boundary.session_id,
            "correlation_id": registry.boundary.correlation_id,
            "snapshot_sha256": launch["snapshot_sha256"],
            "expected_head": registry.boundary.expected_head,
            "gateway_sha256": trusted_gateway_sha256,
            "policy_sha256": trusted_policy_sha256,
            "rules_sha": trusted_rules_sha,
            "state_validated": registry.boundary.state_validated,
            "gateway_verified": registry.boundary.gateway_verified,
            "runner_name": runner_name,
            "native_runtime_mode": "synthetic_infrastructure",
            "product_cutover": False,
        }
        encoded = json.dumps(
            evidence, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("ascii")
        try:
            descriptor = open_exclusive(receipts / "session-evidence.json")
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            raise NativeExecutorBlocked("native_runtime_evidence_unavailable") from None
        executor = NativeOneShotExecutor(
            api=api, workspace=local_workspace, receipts=receipt_store,
            adapters={SYNTHETIC_PROFILE: adapter},
            command_registries={SYNTHETIC_PROFILE: commands},
            boundary=registry.boundary, artifact_transport=transport,
            fenced_adapter_ids=frozenset({SYNTHETIC_PROFILE}),
        )
        with redirect_stdout(io.StringIO()):
            result = executor.run(manifest, role=role, worker_id=worker_id)
    return {
        **result, "native_runtime_mode": "synthetic_infrastructure",
        "product_cutover": False,
        # Internal handoff only; callers must omit paths from public receipts.
        "target_path": str(workspace),
        "session_evidence": evidence,
        "runtime_commands_invoked": runtime_commands,
        "runtime_command_count": len(runtime_commands),
    }
