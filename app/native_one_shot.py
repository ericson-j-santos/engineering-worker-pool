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


class WorkerApi(Protocol):
    def get_task(self, task_id: str) -> dict[str, Any]: ...
    def claim_exact(self, worker_id: str, task_id: str, correlation_id: str) -> dict[str, Any]: ...
    def start(self, task_id: str, worker_id: str, lease_token: str, correlation_id: str) -> None: ...
    def submit_validation(self, task_id: str, worker_id: str, lease_token: str,
                          produced_sha: str, correlation_id: str) -> None: ...
    def complete(self, task_id: str, worker_id: str, lease_token: str,
                 correlation_id: str) -> None: ...
    def fail(self, task_id: str, worker_id: str, lease_token: str,
             correlation_id: str, reason: str) -> None: ...


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
                 boundary: ExecutionBoundary):
        self.api, self.workspace, self.receipts = api, workspace, receipts
        self.adapters, self.command_registries = dict(adapters), dict(command_registries)
        self.boundary = boundary

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
            if receipt_path.exists():
                _need(not receipt_path.is_symlink() and receipt_path.is_file(),
                      "receipt_invalid")
                previous = json.loads(receipt_path.read_text(encoding="utf-8"))
                required = {"schema_version", "result", "task_id", "role", "worker_id",
                            "manifest_sha256", "adapter_contract_sha256", "produced_sha",
                            "evidence_sha256", "changed_paths", "invoked_commands",
                            "session_id", "correlation_id", "gateway_verified",
                            "workspace_id", "replayed"}
                _need(set(previous) == required
                      and previous.get("schema_version") == 1
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
                self.workspace.validate_replay(produced)
                return {**previous, "replayed": True}

            # All local identity, path and content drift checks happen before
            # the first API method, including get_task and claim_exact.
            self.workspace.validate_before_claim(manifest, descriptor)
            if role == "validator":
                builder_receipt = self.receipts.paths(manifest.task_id, "builder")[1]
                if builder_receipt.exists():
                    prior = json.loads(builder_receipt.read_text(encoding="utf-8"))
                    current_id = hashlib.sha256(str(self.workspace.root).encode()).hexdigest()
                    _need(prior.get("workspace_id") != current_id,
                          "validator_workspace_not_distinct")
            context = AdapterContext(self.workspace.root, manifest, descriptor,
                                     self.command_registries.get(manifest.adapter_id, {}), role)

            task = self.api.get_task(manifest.task_id)
            _need(task.get("task_id") == manifest.task_id
                  and task.get("repository") == manifest.repository
                  and task.get("base_sha") == manifest.base_sha,
                  "task_identity_drift")
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
            if not (claimed_task.get("task_id") == manifest.task_id
                    and claimed_task.get("repository") == manifest.repository
                    and claimed_task.get("base_sha") == manifest.base_sha
                    and claimed_task.get("state") == (
                        "leased" if role == "builder" else "validating")
                    and claimed_task.get("leased_by") == worker_id):
                self.api.fail(manifest.task_id, worker_id, token,
                              self.boundary.correlation_id, "claimed_task_drift")
                raise NativeExecutorBlocked("claimed_task_drift")
            try:
                if role == "builder":
                    self.api.start(manifest.task_id, worker_id, token,
                                   self.boundary.correlation_id)
                    result = adapter.build(context)
                    self._validate_result(result, descriptor, context, role)
                    _need(self.workspace.head() == result.produced_sha,
                          "builder_produced_sha_drift")
                    _need(self.workspace.status() == "", "builder_workspace_dirty")
                    _need(self.workspace.changed_paths(manifest.base_sha, result.produced_sha)
                          == result.changed_paths, "builder_diff_mismatch")
                    _need(set(context.invoked) == set(descriptor.commands_for(role)),
                          "required_command_missing")
                    self.api.submit_validation(manifest.task_id, worker_id, token,
                                               result.produced_sha,
                                               self.boundary.correlation_id)
                else:
                    produced = claimed_task.get("produced_sha")
                    _need(type(produced) is str and bool(SHA40.fullmatch(produced)),
                          "validation_sha_missing")
                    _need(manifest.workspace_sha == produced, "validator_workspace_sha_drift")
                    before_head, before_status = self.workspace.head(), self.workspace.status()
                    result = adapter.validate(context, produced)
                    self._validate_result(result, descriptor, context, role)
                    _need(result.produced_sha == produced
                          and self.workspace.head() == before_head == produced
                          and self.workspace.status() == before_status == "",
                          "validator_sha_drift")
                    _need(set(context.invoked) == set(descriptor.commands_for(role)),
                          "required_command_missing")
                    self.api.complete(manifest.task_id, worker_id, token,
                                      self.boundary.correlation_id)
            except Exception as exc:
                allowed = {"adapter_result_sha", "adapter_result_paths",
                           "adapter_result_commands", "adapter_result_evidence",
                           "builder_produced_sha_drift", "builder_workspace_dirty",
                           "builder_diff_mismatch", "required_command_missing",
                           "validation_sha_missing", "validator_workspace_sha_drift",
                           "validator_sha_drift", "command_not_allowlisted"}
                candidate = exc.args[0] if isinstance(exc, NativeExecutorBlocked) else None
                reason = candidate if candidate in allowed else "adapter_failed"
                self.api.fail(manifest.task_id, worker_id, token,
                              self.boundary.correlation_id, str(reason))
                if candidate not in allowed:
                    raise NativeExecutorBlocked("adapter_failed") from None
                raise
            receipt = {
                "schema_version": 1,
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
            self.receipts.write(receipt_path, receipt)
            return receipt
        finally:
            lock.unlink(missing_ok=True)
