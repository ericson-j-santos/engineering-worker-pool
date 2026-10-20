"""Governed one-shot Builder/Validator coordinator.

The queue manifest names a locally installed adapter contract; it never carries
commands.  Product handlers stay outside this package and receive only a bounded
context whose command keys were registered locally before execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TASK_ID = re.compile(r"^cwp-[0-9a-f]{24}$")
NAME = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
CORRELATION = re.compile(r"^[a-z0-9][a-z0-9-]{7,127}$")
FENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class NativeExecutorBlocked(RuntimeError):
    """Closed, non-sensitive failure code."""


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise NativeExecutorBlocked(code)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _safe_path(value: str) -> str:
    _need(type(value) is str and 1 <= len(value) <= 240, "manifest_path")
    path = PurePosixPath(value)
    _need(not path.is_absolute() and "\\" not in value and ".." not in path.parts,
          "manifest_path_traversal")
    _need(all(part not in ("", ".") and not part.startswith(".") for part in path.parts),
          "manifest_path_hidden")
    return path.as_posix()


@dataclass(frozen=True)
class ExecutionBoundary:
    session_id: str
    correlation_id: str
    expected_head: str
    state_validated: bool
    gateway_verified: bool

    def validate(self) -> None:
        _need(bool(re.fullmatch(r"[A-Za-z0-9._-]{3,64}", self.session_id)),
              "session_invalid")
        _need(bool(CORRELATION.fullmatch(self.correlation_id)), "correlation_invalid")
        _need(bool(SHA40.fullmatch(self.expected_head)), "boundary_head_invalid")
        _need(self.state_validated is True and self.gateway_verified is True,
              "governed_boundary_unproved")


@dataclass(frozen=True)
class AdapterDescriptor:
    adapter_id: str
    allowed_paths: tuple[str, ...]
    builder_commands: tuple[str, ...]
    validator_commands: tuple[str, ...]
    roles: tuple[str, ...] = ("builder", "validator")

    def normalized(self) -> dict[str, Any]:
        _need(bool(NAME.fullmatch(self.adapter_id)), "adapter_id")
        paths = tuple(_safe_path(path) for path in self.allowed_paths)
        builder_commands = tuple(self.builder_commands)
        validator_commands = tuple(self.validator_commands)
        _need(paths == tuple(sorted(set(paths))) and bool(paths), "adapter_paths")
        for commands in (builder_commands, validator_commands):
            _need(commands == tuple(sorted(set(commands)))
                  and all(NAME.fullmatch(key) for key in commands), "adapter_commands")
        _need(set(self.roles).issubset({"builder", "validator"}) and bool(self.roles),
              "adapter_roles")
        _need(("builder" not in self.roles or bool(builder_commands))
              and ("validator" not in self.roles or bool(validator_commands)),
              "adapter_role_commands")
        return {"adapter_id": self.adapter_id, "allowed_paths": list(paths),
                "builder_commands": list(builder_commands),
                "validator_commands": list(validator_commands),
                "roles": list(self.roles)}

    @property
    def command_keys(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.builder_commands + self.validator_commands)))

    def commands_for(self, role: str) -> tuple[str, ...]:
        _need(role in self.roles, "adapter_role_unsupported")
        return self.builder_commands if role == "builder" else self.validator_commands

    @property
    def contract_sha256(self) -> str:
        return _digest(self.normalized())


@dataclass(frozen=True)
class TaskManifest:
    schema_version: int
    task_id: str
    repository: str
    base_sha: str
    workspace_sha: str
    adapter_id: str
    adapter_contract_sha256: str
    before_sha256: Mapping[str, str]

    @classmethod
    def parse(cls, raw: Any) -> TaskManifest:
        fields = {"schema_version", "task_id", "repository", "base_sha", "workspace_sha",
                  "adapter_id", "adapter_contract_sha256", "before_sha256"}
        _need(type(raw) is dict and set(raw) == fields, "manifest_schema")
        before = raw["before_sha256"]
        _need(type(before) is dict and 1 <= len(before) <= 32, "manifest_hashes")
        normalized: dict[str, str] = {}
        for path, digest in before.items():
            safe = _safe_path(path)
            _need(type(digest) is str and bool(SHA256.fullmatch(digest)), "manifest_hash")
            normalized[safe] = digest
        value = cls(raw["schema_version"], raw["task_id"], raw["repository"],
                    raw["base_sha"], raw["workspace_sha"], raw["adapter_id"],
                    raw["adapter_contract_sha256"], normalized)
        _need(value.schema_version == 1, "manifest_version")
        _need(type(value.task_id) is str and bool(TASK_ID.fullmatch(value.task_id)),
              "manifest_task_id")
        _need(type(value.repository) is str and bool(re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", value.repository)),
            "manifest_repository")
        _need(type(value.base_sha) is str and bool(SHA40.fullmatch(value.base_sha)),
              "manifest_base_sha")
        _need(type(value.workspace_sha) is str and bool(SHA40.fullmatch(value.workspace_sha)),
              "manifest_workspace_sha")
        _need(type(value.adapter_id) is str and bool(NAME.fullmatch(value.adapter_id)),
              "manifest_adapter")
        _need(type(value.adapter_contract_sha256) is str
              and bool(SHA256.fullmatch(value.adapter_contract_sha256)),
              "manifest_adapter_contract")
        return value


@dataclass(frozen=True)
class AdapterResult:
    produced_sha: str
    changed_paths: tuple[str, ...]
    invoked_commands: tuple[str, ...]
    evidence_sha256: str


@dataclass(frozen=True)
class DurableArtifact:
    """Opaque reference produced by a separately attested durable transport."""

    artifact_kind: str
    artifact_ref: str
    artifact_sha256: str

    @classmethod
    def parse(cls, raw: Any) -> DurableArtifact:
        _need(type(raw) is dict and set(raw) == {
            "artifact_kind", "artifact_ref", "artifact_sha256",
        }, "durable_artifact_invalid")
        artifact = cls(raw["artifact_kind"], raw["artifact_ref"], raw["artifact_sha256"])
        _need(bool(NAME.fullmatch(artifact.artifact_kind))
              and type(artifact.artifact_ref) is str
              and 1 <= len(artifact.artifact_ref) <= 2048
              and "\n" not in artifact.artifact_ref
              and "\r" not in artifact.artifact_ref
              and bool(SHA256.fullmatch(artifact.artifact_sha256)),
              "durable_artifact_invalid")
        return artifact

    def checkpoint_fields(self) -> dict[str, str]:
        return {
            "artifact_kind": self.artifact_kind,
            "artifact_ref": self.artifact_ref,
            "artifact_sha256": self.artifact_sha256,
        }


class DurableArtifactTransport(Protocol):
    """Host-provided transport; a local commit SHA alone is not durable."""

    def preflight(self) -> None: ...
    def publish(self, *, task_id: str, manifest: TaskManifest,
                lease_generation: int, attempt_id: str,
                effect_id: str, workspace: LocalWorkspace,
                result: AdapterResult) -> DurableArtifact: ...
    def materialize(self, *, task_id: str, manifest: TaskManifest,
                    workspace: LocalWorkspace,
                    artifact: DurableArtifact) -> AdapterResult: ...
    def verify(self, *, task_id: str, manifest: TaskManifest,
               workspace: LocalWorkspace,
               produced_sha: str, artifact: DurableArtifact) -> None: ...


class WorkerApi(Protocol):
    def get_task(self, task_id: str) -> dict[str, Any]: ...
    def claim_exact(self, worker_id: str, task_id: str, correlation_id: str) -> dict[str, Any]: ...
    def claim_fenced_exact(self, worker_id: str, task_id: str,
                           correlation_id: str) -> dict[str, Any]: ...
    def get_latest_checkpoint(self, task_id: str) -> dict[str, Any] | None: ...
    def record_checkpoint(self, task_id: str, worker_id: str, lease_token: str,
                          lease_generation: int, attempt_id: str,
                          checkpoint_id: str, sequence: int, stage: str,
                          next_action: str, correlation_id: str, *,
                          artifact_kind: str | None = None,
                          artifact_ref: str | None = None,
                          artifact_sha256: str | None = None,
                          effect_id: str | None = None,
                          effect_state: str = "none",
                          effect_result_ref: str | None = None) -> dict[str, Any]: ...
    def start(self, task_id: str, worker_id: str, lease_token: str,
              correlation_id: str, *, lease_generation: int | None = None,
              attempt_id: str | None = None) -> None: ...
    def submit_validation(self, task_id: str, worker_id: str, lease_token: str,
                          produced_sha: str, correlation_id: str, *,
                          lease_generation: int | None = None,
                          attempt_id: str | None = None) -> None: ...
    def complete(self, task_id: str, worker_id: str, lease_token: str,
                 correlation_id: str, *, lease_generation: int | None = None,
                 attempt_id: str | None = None) -> None: ...
    def fail(self, task_id: str, worker_id: str, lease_token: str,
             correlation_id: str, reason: str, *,
             lease_generation: int | None = None,
             attempt_id: str | None = None) -> None: ...
    def block(self, task_id: str, worker_id: str, lease_token: str,
              correlation_id: str, reason: str, *,
              lease_generation: int | None = None,
              attempt_id: str | None = None) -> None: ...


class NativeAdapter(Protocol):
    descriptor: AdapterDescriptor
    def build(self, context: AdapterContext) -> AdapterResult: ...
    def validate(self, context: AdapterContext, produced_sha: str) -> AdapterResult: ...


class LocalWorkspace:
    """Read and validate a detached product worktree without using a remote."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _git(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.root, text=True, capture_output=True,
                timeout=15, check=False,
                env={"PATH": os.environ.get("PATH", os.defpath),
                     "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                     "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired):
            raise NativeExecutorBlocked("workspace_git_unavailable") from None
        _need(result.returncode == 0 and len(result.stdout) <= 1_048_576,
              "workspace_git_failed")
        return result.stdout.strip()

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").lower()

    def status(self) -> str:
        return self._git("status", "--porcelain", "--untracked-files=all")

    def changed_paths(self, base_sha: str, produced_sha: str) -> tuple[str, ...]:
        output = self._git("diff", "--name-only", "--no-renames", base_sha,
                           produced_sha)
        return tuple(sorted(line for line in output.splitlines() if line))

    def validate_replay(self, produced_sha: str) -> None:
        _need(self.root.is_dir() and not self.root.is_symlink(), "workspace_root")
        _need(self._git("remote") == "", "workspace_remote_forbidden")
        _need(self.status() == "", "workspace_dirty")
        _need(self.head() == produced_sha, "replay_head_drift")

    def validate_before_claim(self, manifest: TaskManifest,
                              descriptor: AdapterDescriptor) -> None:
        _need(self.root.is_dir() and not self.root.is_symlink(), "workspace_root")
        _need(self._git("remote") == "", "workspace_remote_forbidden")
        _need(self.status() == "", "workspace_dirty")
        _need(self.head() == manifest.workspace_sha, "workspace_head_drift")
        _need(set(manifest.before_sha256) == set(descriptor.allowed_paths),
              "manifest_adapter_path_mismatch")
        for relative, expected in manifest.before_sha256.items():
            path = self.root / relative
            _need(path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1,
                  "workspace_file_invalid")
            _need(path.resolve().is_relative_to(self.root), "workspace_path_escape")
            _need(hashlib.sha256(path.read_bytes()).hexdigest() == expected,
                  "workspace_file_drift")


class AdapterContext:
    def __init__(self, root: Path, manifest: TaskManifest, descriptor: AdapterDescriptor,
                 commands: Mapping[str, Callable[[], Any]], role: str):
        self.root, self.manifest, self.descriptor = root, manifest, descriptor
        self._commands = dict(commands)
        self._allowed = descriptor.commands_for(role)
        self.invoked: list[str] = []
        _need(set(self._commands) == set(descriptor.command_keys), "local_command_registry")

    def invoke(self, key: str) -> Any:
        _need(key in self._commands and key in self._allowed,
              "command_not_allowlisted")
        self.invoked.append(key)
        return self._commands[key]()


class ReceiptStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def paths(self, task_id: str, role: str) -> tuple[Path, Path]:
        stem = f"{task_id}-{role}"
        return self.root / f"{stem}.lock", self.root / f"{stem}.json"

    def acquire(self, task_id: str, role: str) -> tuple[Path, Path]:
        lock, receipt = self.paths(task_id, role)
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise NativeExecutorBlocked("executor_busy") from None
        os.close(descriptor)
        return lock, receipt

    @staticmethod
    def write(path: Path, value: dict[str, Any]) -> None:
        _need(not path.exists() and not path.is_symlink(), "receipt_exists")
        descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp",
                                            dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class NativeOneShotExecutor:
    def __init__(self, *, api: WorkerApi, workspace: LocalWorkspace,
                 receipts: ReceiptStore, adapters: Mapping[str, NativeAdapter],
                 command_registries: Mapping[str, Mapping[str, Callable[[], Any]]],
                 boundary: ExecutionBoundary,
                 artifact_transport: DurableArtifactTransport | None = None,
                 fenced_adapter_ids: frozenset[str] = frozenset()):
        self.api, self.workspace, self.receipts = api, workspace, receipts
        self.adapters, self.command_registries = dict(adapters), dict(command_registries)
        self.boundary = boundary
        self.artifact_transport = artifact_transport
        self.fenced_adapter_ids = frozenset(fenced_adapter_ids)
        _need(all(type(item) is str and bool(NAME.fullmatch(item))
                  for item in self.fenced_adapter_ids),
              "fenced_adapter_attestation_invalid")
        _need(self.fenced_adapter_ids.issubset(self.adapters),
              "fenced_adapter_attestation_unknown")

    @staticmethod
    def _fencing_required(task: Mapping[str, Any]) -> bool:
        value = task.get("fencing_required", False)
        if type(value) is bool:
            return value
        # Store-level adapters expose SQLite booleans as 0/1; HTTP adapters
        # must still normalize them before crossing the public boundary.
        _need(type(value) is int and value in {0, 1}, "task_fencing_invalid")
        return bool(value)

    @staticmethod
    def _validate_checkpoint_shape(checkpoint: Mapping[str, Any], task_id: str) -> None:
        generation = checkpoint.get("lease_generation")
        attempt_id = checkpoint.get("attempt_id")
        sequence = checkpoint.get("sequence")
        effect_id = checkpoint.get("effect_id")
        effect_state = checkpoint.get("effect_state")
        effect_result_ref = checkpoint.get("effect_result_ref")
        _need(checkpoint.get("task_id") == task_id
              and type(generation) is int and not isinstance(generation, bool)
              and generation >= 1
              and type(attempt_id) is str and bool(FENCE_ID.fullmatch(attempt_id))
              and type(checkpoint.get("checkpoint_id")) is str
              and bool(FENCE_ID.fullmatch(checkpoint["checkpoint_id"]))
              and type(sequence) is int and not isinstance(sequence, bool)
              and sequence >= 1
              and type(checkpoint.get("stage")) is str
              and bool(FENCE_ID.fullmatch(checkpoint["stage"]))
              and type(checkpoint.get("next_action")) is str
              and 1 <= len(checkpoint["next_action"]) <= 2048,
              "recovery_context_invalid")
        artifact_values = tuple(checkpoint.get(key) for key in (
            "artifact_kind", "artifact_ref", "artifact_sha256",
        ))
        _need(all(value is None for value in artifact_values)
              or all(value is not None for value in artifact_values),
              "recovery_context_invalid")
        if artifact_values[0] is not None:
            DurableArtifact.parse({
                "artifact_kind": artifact_values[0],
                "artifact_ref": artifact_values[1],
                "artifact_sha256": artifact_values[2],
            })
        _need(effect_state in {"none", "pending", "unknown", "confirmed", "not_applied"}
              and ((effect_state == "none" and effect_id is None
                    and effect_result_ref is None)
                   or (effect_state != "none" and type(effect_id) is str
                       and bool(FENCE_ID.fullmatch(effect_id))
                       and ((effect_state in {"pending", "unknown"}
                             and effect_result_ref is None)
                            or (effect_state in {"confirmed", "not_applied"}
                                and type(effect_result_ref) is str
                                and 1 <= len(effect_result_ref) <= 2048)))),
              "recovery_context_invalid")

    @classmethod
    def _recovery_context(
        cls, raw: Any, task_id: str, current_generation: int,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        _need(type(raw) is dict and set(raw) == {"latest_checkpoint", "effects"},
              "recovery_context_invalid")
        latest, effects = raw["latest_checkpoint"], raw["effects"]
        _need(latest is None or type(latest) is dict, "recovery_context_invalid")
        _need(type(effects) is list and len(effects) <= 64
              and all(type(item) is dict for item in effects),
              "recovery_context_invalid")
        if latest is not None:
            cls._validate_checkpoint_shape(latest, task_id)
            _need(latest["lease_generation"] < current_generation,
                  "recovery_context_invalid")
        effect_ids: set[str] = set()
        for item in effects:
            cls._validate_checkpoint_shape(item, task_id)
            effect_id = item.get("effect_id")
            state = item.get("effect_state")
            outcome = item.get("reconciliation_outcome")
            readback_ref = item.get("reconciliation_readback_ref")
            _need(type(effect_id) is str and effect_id not in effect_ids
                  and item["lease_generation"] < current_generation
                  and state in {"pending", "unknown", "confirmed", "not_applied"}
                  and outcome in {None, "confirmed", "not_applied"}
                  and (outcome is None or state in {"pending", "unknown"})
                  and (outcome is None or (type(readback_ref) is str
                                           and 1 <= len(readback_ref) <= 2048)),
                  "recovery_context_invalid")
            effect_ids.add(effect_id)
        if latest is not None and latest.get("effect_id") is not None:
            matching = next((item for item in effects
                             if item["effect_id"] == latest["effect_id"]), None)
            compared = (
                "checkpoint_id", "lease_generation", "attempt_id", "sequence",
                "stage", "artifact_kind", "artifact_ref", "artifact_sha256",
                "next_action", "effect_id", "effect_state", "effect_result_ref",
            )
            _need(matching is not None
                  and all(matching.get(key) == latest.get(key) for key in compared),
                  "recovery_context_invalid")
        return latest, effects

    @classmethod
    def _validate_recovery_effects(
        cls, latest: Mapping[str, Any] | None, effects: list[dict[str, Any]], role: str,
    ) -> None:
        for item in effects:
            effective = item.get("reconciliation_outcome") or item.get("effect_state")
            if effective == "not_applied":
                _need(latest is None
                      or item.get("effect_id") != latest.get("effect_id"),
                      "recovery_effect_not_resumable")
                continue
            _need(effective == "confirmed"
                  and latest is not None
                  and item.get("effect_id") == latest.get("effect_id")
                  and latest.get("effect_state") == "confirmed"
                  and latest.get("stage") == "builder_result"
                  and latest.get("next_action") == "submit_validation",
                  "recovery_effect_unsafe")
            cls._artifact_from_checkpoint(latest)

    @staticmethod
    def _artifact_from_checkpoint(checkpoint: Mapping[str, Any]) -> DurableArtifact:
        return DurableArtifact.parse({
            "artifact_kind": checkpoint.get("artifact_kind"),
            "artifact_ref": checkpoint.get("artifact_ref"),
            "artifact_sha256": checkpoint.get("artifact_sha256"),
        })

    @staticmethod
    def _lease_fence(fenced: bool, lease: Mapping[str, Any],
                     claimed_task: Mapping[str, Any]) -> tuple[int | None, str | None]:
        generation, attempt_id = lease.get("lease_generation"), lease.get("attempt_id")
        if not fenced:
            return None, None
        _need(type(generation) is int and not isinstance(generation, bool) and generation >= 1
              and type(attempt_id) is str and bool(FENCE_ID.fullmatch(attempt_id))
              and claimed_task.get("lease_generation") == generation
              and claimed_task.get("attempt_id") == attempt_id,
              "lease_fence_invalid")
        return generation, attempt_id

    @staticmethod
    def _mutation_fence(generation: int | None,
                        attempt_id: str | None) -> dict[str, Any]:
        if generation is None:
            return {}
        return {"lease_generation": generation, "attempt_id": attempt_id}

    @staticmethod
    def _checkpoint_id(attempt_id: str, role: str, stage: str) -> str:
        suffix = hashlib.sha256(f"{attempt_id}:{role}:{stage}".encode()).hexdigest()[:32]
        return f"native-{role}-{stage}-{suffix}"

    @staticmethod
    def _artifact_effect_id(task_id: str, produced_sha: str) -> str:
        suffix = hashlib.sha256(f"{task_id}:{produced_sha}".encode()).hexdigest()[:32]
        return f"native-artifact-{suffix}"

    @staticmethod
    def _validate_recovered_result(result: AdapterResult, descriptor: AdapterDescriptor) -> None:
        _need(type(result) is AdapterResult and bool(SHA40.fullmatch(result.produced_sha)),
              "artifact_result_sha")
        paths = tuple(_safe_path(path) for path in result.changed_paths)
        _need(bool(paths) and paths == tuple(sorted(set(paths)))
              and set(paths).issubset(descriptor.allowed_paths),
              "artifact_result_paths")
        _need(result.invoked_commands == descriptor.commands_for("builder"),
              "artifact_result_commands")
        _need(bool(SHA256.fullmatch(result.evidence_sha256)), "artifact_result_evidence")

    @staticmethod
    def _validate_result(result: AdapterResult, descriptor: AdapterDescriptor,
                         context: AdapterContext, role: str) -> None:
        _need(type(result) is AdapterResult and bool(SHA40.fullmatch(result.produced_sha)),
              "adapter_result_sha")
        paths = tuple(_safe_path(path) for path in result.changed_paths)
        _need(bool(paths) and paths == tuple(sorted(set(paths)))
              and set(paths).issubset(descriptor.allowed_paths),
              "adapter_result_paths")
        _need(result.invoked_commands == tuple(context.invoked)
              and set(result.invoked_commands).issubset(descriptor.commands_for(role)),
              "adapter_result_commands")
        _need(bool(SHA256.fullmatch(result.evidence_sha256)), "adapter_result_evidence")

    def run(self, raw_manifest: Any, *, role: str, worker_id: str) -> dict[str, Any]:
        _need(role in ("builder", "validator"), "role_invalid")
        _need(bool(NAME.fullmatch(worker_id)), "worker_id_invalid")
        self.boundary.validate()
        manifest = TaskManifest.parse(raw_manifest)
        _need(manifest.workspace_sha == self.boundary.expected_head, "boundary_manifest_drift")
        _need(role != "builder" or manifest.workspace_sha == manifest.base_sha,
              "builder_workspace_not_base")
        adapter = self.adapters.get(manifest.adapter_id)
        _need(adapter is not None, "adapter_unavailable")
        descriptor = adapter.descriptor
        _need(descriptor.adapter_id == manifest.adapter_id
              and descriptor.contract_sha256 == manifest.adapter_contract_sha256,
              "adapter_contract_mismatch")
        _need(role in descriptor.roles, "adapter_role_unsupported")

        lock, receipt_path = self.receipts.acquire(manifest.task_id, role)
        try:
            has_receipt = receipt_path.exists()
            context: AdapterContext | None = None
            if not has_receipt:
                # All local identity, path and content drift checks happen before
                # the first claim. A canonical GET is still required before a
                # receipt can authorize replay.
                self.workspace.validate_before_claim(manifest, descriptor)
                if role == "validator":
                    builder_receipt = self.receipts.paths(manifest.task_id, "builder")[1]
                    if builder_receipt.exists():
                        prior = json.loads(builder_receipt.read_text(encoding="utf-8"))
                        current_id = hashlib.sha256(str(self.workspace.root).encode()).hexdigest()
                        _need(prior.get("workspace_id") != current_id,
                              "validator_workspace_not_distinct")
                context = AdapterContext(
                    self.workspace.root, manifest, descriptor,
                    self.command_registries.get(manifest.adapter_id, {}), role,
                )

            task = self.api.get_task(manifest.task_id)
            _need(task.get("task_id") == manifest.task_id
                  and task.get("repository") == manifest.repository
                  and task.get("base_sha") == manifest.base_sha,
                  "task_identity_drift")
            fenced = self._fencing_required(task)
            if fenced:
                # This attestation is injected by trusted executor configuration,
                # never by the task manifest. It asserts that adapter actions are
                # local/idempotent or independently fenced before they may run.
                _need(manifest.adapter_id in self.fenced_adapter_ids,
                      "fenced_adapter_not_attested")
                _need(self.artifact_transport is not None,
                      "durable_artifact_transport_required")
                _need(callable(getattr(self.api, "get_latest_checkpoint", None)),
                      "fenced_worker_api_unsupported")

            if has_receipt:
                _need(not receipt_path.is_symlink() and receipt_path.is_file(),
                      "receipt_invalid")
                previous = json.loads(receipt_path.read_text(encoding="utf-8"))
                required = {
                    "schema_version", "result", "task_id", "role", "worker_id",
                    "manifest_sha256", "adapter_contract_sha256", "produced_sha",
                    "evidence_sha256", "changed_paths", "invoked_commands",
                    "session_id", "correlation_id", "gateway_verified",
                    "workspace_id", "replayed",
                }
                if fenced:
                    required |= {
                        "fencing_required", "lease_generation", "attempt_id",
                        "artifact_kind", "artifact_ref", "artifact_sha256",
                    }
                expected_state = {"completed"} if role == "validator" else {
                    "validating", "completed",
                }
                _need(set(previous) == required
                      and previous.get("schema_version") == (2 if fenced else 1)
                      and previous.get("manifest_sha256") == _digest(raw_manifest)
                      and previous.get("task_id") == manifest.task_id
                      and previous.get("role") == role
                      and previous.get("worker_id") == worker_id
                      and previous.get("adapter_contract_sha256")
                      == descriptor.contract_sha256
                      and previous.get("result") == (
                          "NATIVE_ONE_SHOT_BUILT" if role == "builder"
                          else "NATIVE_ONE_SHOT_VALIDATED")
                      and previous.get("workspace_id") == hashlib.sha256(
                          str(self.workspace.root).encode()).hexdigest()
                      and previous.get("session_id") == self.boundary.session_id
                      and previous.get("correlation_id") == self.boundary.correlation_id
                      and previous.get("gateway_verified") is True
                      and previous.get("replayed") is False
                      and type(previous.get("evidence_sha256")) is str
                      and bool(SHA256.fullmatch(previous["evidence_sha256"]))
                      and type(previous.get("changed_paths")) is list
                      and all(type(path) is str for path in previous["changed_paths"])
                      and set(previous["changed_paths"]).issubset(descriptor.allowed_paths)
                      and len(previous["changed_paths"]) == len(
                          set(previous["changed_paths"]))
                      and type(previous.get("invoked_commands")) is list
                      and all(type(key) is str for key in previous["invoked_commands"])
                      and set(previous["invoked_commands"]) == set(
                          descriptor.commands_for(role))
                      and len(previous["invoked_commands"]) == len(
                          descriptor.commands_for(role)),
                      "receipt_mismatch")
                produced = previous.get("produced_sha")
                _need(type(produced) is str and bool(SHA40.fullmatch(produced)),
                      "receipt_produced_sha")
                _need(task.get("state") in expected_state
                      and task.get("produced_sha") == produced,
                      "receipt_canonical_state")
                if fenced:
                    _need(previous.get("fencing_required") is True
                          and type(previous.get("lease_generation")) is int
                          and previous["lease_generation"] >= 1
                          and type(previous.get("attempt_id")) is str
                          and task.get("lease_generation")
                          == previous["lease_generation"],
                          "receipt_lease_stale")
                    artifact = DurableArtifact.parse({
                        "artifact_kind": previous.get("artifact_kind"),
                        "artifact_ref": previous.get("artifact_ref"),
                        "artifact_sha256": previous.get("artifact_sha256"),
                    })
                self.workspace.validate_replay(produced)
                _need(tuple(previous["changed_paths"])
                      == self.workspace.changed_paths(manifest.base_sha, produced),
                      "receipt_mismatch")
                if fenced:
                    checkpoint = self.api.get_latest_checkpoint(manifest.task_id)
                    _need(type(checkpoint) is dict,
                          "receipt_checkpoint_missing")
                    self._validate_checkpoint_shape(checkpoint, manifest.task_id)
                    expected_stage = (
                        "builder_result" if role == "builder" else "validator_result"
                    )
                    expected_next = "submit_validation" if role == "builder" else "complete"
                    _need(checkpoint.get("lease_generation")
                          == previous["lease_generation"]
                          and checkpoint.get("attempt_id") == previous["attempt_id"]
                          and checkpoint.get("stage") == expected_stage
                          and checkpoint.get("next_action") == expected_next
                          and checkpoint.get("artifact_kind") == artifact.artifact_kind
                          and checkpoint.get("artifact_ref") == artifact.artifact_ref
                          and checkpoint.get("artifact_sha256")
                          == artifact.artifact_sha256
                          and ((role == "builder"
                                and checkpoint.get("effect_state") == "confirmed"
                                and checkpoint.get("effect_id")
                                == self._artifact_effect_id(
                                    manifest.task_id, produced,
                                )
                                and checkpoint.get("effect_result_ref")
                                == artifact.artifact_ref)
                               or (role == "validator"
                                   and checkpoint.get("effect_state") == "none")),
                          "receipt_checkpoint_drift")
                    self.artifact_transport.verify(
                        task_id=manifest.task_id, manifest=manifest,
                        workspace=self.workspace,
                        produced_sha=produced, artifact=artifact,
                    )
                return {**previous, "replayed": True}

            _need(context is not None, "executor_context_missing")
            if fenced:
                _need(callable(getattr(self.api, "claim_fenced_exact", None))
                      and callable(getattr(self.api, "record_checkpoint", None))
                      and callable(getattr(self.api, "block", None)),
                      "fenced_worker_api_unsupported")
                # Validate trusted storage configuration plus conditional write
                # and exact readback before acquiring a lease. Publication still
                # performs its own independent readback.
                _need(callable(getattr(self.artifact_transport, "preflight", None)),
                      "durable_artifact_transport_invalid")
                try:
                    self.artifact_transport.preflight()
                except Exception:
                    raise NativeExecutorBlocked(
                        "durable_artifact_preflight_failed"
                    ) from None
                claim = self.api.claim_fenced_exact(
                    worker_id, manifest.task_id, self.boundary.correlation_id,
                )
            else:
                claim = self.api.claim_exact(worker_id, manifest.task_id,
                                             self.boundary.correlation_id)
            _need(claim.get("claimed") is True, "claim_unavailable")
            claimed_task = claim.get("task") or {}
            lease = claim.get("lease") or {}
            token = lease.get("lease_token")
            expires_at = lease.get("lease_expires_at")
            try:
                expires = datetime.fromisoformat(str(expires_at))
            except ValueError:
                expires = datetime.min.replace(tzinfo=UTC)
            _need(type(token) is str and 16 <= len(token) <= 128
                  and lease.get("task_id") == manifest.task_id
                  and lease.get("worker_id") == worker_id
                  and lease.get("role") == role
                  and expires.tzinfo is not None and expires > datetime.now(UTC),
                  "lease_invalid")
            _need(self._fencing_required(claimed_task) is fenced,
                  "claimed_task_fencing_drift")
            generation, attempt_id = self._lease_fence(fenced, lease, claimed_task)
            fence = self._mutation_fence(generation, attempt_id)
            latest: dict[str, Any] | None = None
            if fenced:
                try:
                    latest, effects = self._recovery_context(
                        claim.get("recovery_context"), manifest.task_id, generation,
                    )
                    self._validate_recovery_effects(latest, effects, role)
                except Exception:
                    try:
                        self.api.block(
                            manifest.task_id, worker_id, token,
                            self.boundary.correlation_id, "unsafe_recovery_context",
                            **fence,
                        )
                    except Exception:
                        pass
                    raise
            if not (claimed_task.get("task_id") == manifest.task_id
                    and claimed_task.get("repository") == manifest.repository
                    and claimed_task.get("base_sha") == manifest.base_sha
                    and claimed_task.get("state") == (
                        "leased" if role == "builder" else "validating")
                    and claimed_task.get("leased_by") == worker_id):
                try:
                    self.api.fail(manifest.task_id, worker_id, token,
                                  self.boundary.correlation_id, "claimed_task_drift",
                                  **fence)
                except Exception:
                    pass
                raise NativeExecutorBlocked("claimed_task_drift")
            artifact: DurableArtifact | None = None
            try:
                if role == "builder":
                    self.api.start(manifest.task_id, worker_id, token,
                                   self.boundary.correlation_id, **fence)
                    if fenced and latest is not None:
                        _need(latest.get("stage") == "builder_result"
                              and latest.get("next_action") == "submit_validation",
                              "fenced_resume_checkpoint_unsupported")
                        artifact = self._artifact_from_checkpoint(latest)
                        result = self.artifact_transport.materialize(
                            task_id=manifest.task_id, manifest=manifest,
                            workspace=self.workspace,
                            artifact=artifact,
                        )
                        self._validate_recovered_result(result, descriptor)
                    else:
                        result = adapter.build(context)
                        self._validate_result(result, descriptor, context, role)
                    _need(self.workspace.head() == result.produced_sha,
                          "builder_produced_sha_drift")
                    _need(self.workspace.status() == "", "builder_workspace_dirty")
                    _need(self.workspace.changed_paths(manifest.base_sha, result.produced_sha)
                          == result.changed_paths, "builder_diff_mismatch")
                    if latest is None:
                        _need(set(context.invoked) == set(descriptor.commands_for(role)),
                              "required_command_missing")
                    if fenced:
                        effect_id = self._artifact_effect_id(
                            manifest.task_id, result.produced_sha,
                        )
                        if latest is not None:
                            _need(latest.get("effect_id") == effect_id,
                                  "artifact_effect_identity_drift")
                        if artifact is None:
                            self.api.record_checkpoint(
                                manifest.task_id, worker_id, token, generation, attempt_id,
                                self._checkpoint_id(attempt_id, role, "artifact-pending"),
                                1, "artifact_publish", "publish_artifact",
                                self.boundary.correlation_id, effect_id=effect_id,
                                effect_state="pending",
                            )
                            try:
                                raw_artifact = self.artifact_transport.publish(
                                    task_id=manifest.task_id,
                                    manifest=manifest,
                                    lease_generation=generation,
                                    attempt_id=attempt_id,
                                    effect_id=effect_id,
                                    workspace=self.workspace,
                                    result=result,
                                )
                                artifact = DurableArtifact.parse(
                                    raw_artifact.checkpoint_fields()
                                    if type(raw_artifact) is DurableArtifact
                                    else raw_artifact
                                )
                                self.artifact_transport.verify(
                                    task_id=manifest.task_id,
                                    manifest=manifest,
                                    workspace=self.workspace,
                                    produced_sha=result.produced_sha,
                                    artifact=artifact,
                                )
                            except Exception:
                                # Publication may already have happened. Mark the
                                # effect unknown and block; never retry or fail it.
                                try:
                                    self.api.record_checkpoint(
                                        manifest.task_id, worker_id, token,
                                        generation, attempt_id,
                                        self._checkpoint_id(
                                            attempt_id, role, "artifact-unknown",
                                        ),
                                        2, "artifact_publish",
                                        "reconcile_artifact",
                                        self.boundary.correlation_id,
                                        effect_id=effect_id,
                                        effect_state="unknown",
                                    )
                                except Exception:
                                    pass
                                try:
                                    self.api.block(
                                        manifest.task_id, worker_id, token,
                                        self.boundary.correlation_id,
                                        "artifact_publish_ambiguous", **fence,
                                    )
                                except Exception:
                                    pass
                                raise NativeExecutorBlocked(
                                    "artifact_publish_ambiguous"
                                ) from None
                        else:
                            self.artifact_transport.verify(
                                task_id=manifest.task_id, manifest=manifest,
                                workspace=self.workspace,
                                produced_sha=result.produced_sha, artifact=artifact,
                            )
                        self.api.record_checkpoint(
                            manifest.task_id, worker_id, token, generation, attempt_id,
                            self._checkpoint_id(attempt_id, role, "result"),
                            2 if latest is None else 1, "builder_result",
                            "submit_validation", self.boundary.correlation_id,
                            **artifact.checkpoint_fields(),
                            effect_id=effect_id, effect_state="confirmed",
                            effect_result_ref=artifact.artifact_ref,
                        )
                    self.api.submit_validation(manifest.task_id, worker_id, token,
                                               result.produced_sha,
                                               self.boundary.correlation_id, **fence)
                else:
                    produced = claimed_task.get("produced_sha")
                    _need(type(produced) is str and bool(SHA40.fullmatch(produced)),
                          "validation_sha_missing")
                    _need(manifest.workspace_sha == produced, "validator_workspace_sha_drift")
                    if fenced:
                        _need(latest is not None
                              and latest.get("stage") == "builder_result"
                              and latest.get("next_action") == "submit_validation",
                              "validator_artifact_checkpoint_missing")
                        artifact = self._artifact_from_checkpoint(latest)
                        self.artifact_transport.verify(
                            task_id=manifest.task_id, manifest=manifest,
                            workspace=self.workspace,
                            produced_sha=produced, artifact=artifact,
                        )
                    before_head, before_status = self.workspace.head(), self.workspace.status()
                    result = adapter.validate(context, produced)
                    self._validate_result(result, descriptor, context, role)
                    _need(result.produced_sha == produced
                          and self.workspace.head() == before_head == produced
                          and self.workspace.status() == before_status == "",
                          "validator_sha_drift")
                    _need(set(context.invoked) == set(descriptor.commands_for(role)),
                          "required_command_missing")
                    if fenced:
                        self.api.record_checkpoint(
                            manifest.task_id, worker_id, token, generation, attempt_id,
                            self._checkpoint_id(attempt_id, role, "result"),
                            1, "validator_result",
                            "complete", self.boundary.correlation_id,
                            **artifact.checkpoint_fields(),
                        )
                    self.api.complete(manifest.task_id, worker_id, token,
                                      self.boundary.correlation_id, **fence)
            except Exception as exc:
                allowed = {"adapter_result_sha", "adapter_result_paths",
                           "adapter_result_commands", "adapter_result_evidence",
                           "builder_produced_sha_drift", "builder_workspace_dirty",
                           "builder_diff_mismatch", "required_command_missing",
                           "validation_sha_missing", "validator_workspace_sha_drift",
                           "validator_sha_drift", "command_not_allowlisted",
                           "artifact_publish_ambiguous"}
                candidate = exc.args[0] if isinstance(exc, NativeExecutorBlocked) else None
                reason = candidate if candidate in allowed else "adapter_failed"
                fail_rejected = False
                if candidate != "artifact_publish_ambiguous":
                    try:
                        self.api.fail(manifest.task_id, worker_id, token,
                                      self.boundary.correlation_id, str(reason), **fence)
                    except Exception:
                        # A stale failure is expected after lease loss. Preserve
                        # a closed original error rather than replacing it.
                        fail_rejected = True
                if fail_rejected and candidate is not None:
                    raise
                if candidate not in allowed:
                    raise NativeExecutorBlocked("adapter_failed") from None
                raise
            receipt = {
                "schema_version": 2 if fenced else 1,
                "result": "NATIVE_ONE_SHOT_BUILT" if role == "builder"
                          else "NATIVE_ONE_SHOT_VALIDATED",
                "task_id": manifest.task_id, "role": role, "worker_id": worker_id,
                "manifest_sha256": _digest(raw_manifest),
                "adapter_contract_sha256": descriptor.contract_sha256,
                "produced_sha": result.produced_sha,
                "evidence_sha256": result.evidence_sha256,
                "changed_paths": list(result.changed_paths),
                "invoked_commands": list(result.invoked_commands),
                "session_id": self.boundary.session_id,
                "correlation_id": self.boundary.correlation_id,
                "gateway_verified": True, "replayed": False,
                "workspace_id": hashlib.sha256(str(self.workspace.root).encode()).hexdigest(),
            }
            if fenced:
                receipt.update({
                    "fencing_required": True,
                    "lease_generation": generation,
                    "attempt_id": attempt_id,
                    **artifact.checkpoint_fields(),
                })
            self.receipts.write(receipt_path, receipt)
            return receipt
        finally:
            lock.unlink(missing_ok=True)
