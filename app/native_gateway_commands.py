"""Locally installed, role-scoped command registry mediated by Command Gateway.

This module does NOT select tasks or authorize deployment. Specs are trusted,
versioned local code and never decoded from a queue manifest or model output.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Callable, Mapping

from app.native_gateway_preflight import attest_execution_boundary
from app.native_one_shot import (
    AdapterDescriptor, ExecutionBoundary, NAME, NativeExecutorBlocked, SHA256,
)

MAX_EVENT_BYTES = 1_048_576


def _need(ok: bool, code: str) -> None:
    if not ok:
        raise NativeExecutorBlocked(code)


@dataclass(frozen=True)
class GovernedCommand:
    key: str
    role: str
    argv: tuple[str, ...]
    risk: int
    timeout_seconds: int = 30
    allow_dirty: bool = False
    allow_head_change: bool = False
    script_sha256: str | None = None

    def validate(self, descriptor: AdapterDescriptor) -> None:
        _need(type(self.key) is str and bool(NAME.fullmatch(self.key))
              and self.role in ("builder", "validator")
              and self.key in descriptor.commands_for(self.role),
              "native_command_contract")
        _need(type(self.argv) is tuple and len(self.argv) >= 2
              and all(type(arg) is str and arg and len(arg) <= 200
                      and "\x00" not in arg and "\n" not in arg
                      for arg in self.argv),
              "native_command_arguments")
        _need(type(self.risk) is int and self.risk in (1, 2)
              and type(self.timeout_seconds) is int
              and 1 <= self.timeout_seconds <= 120
              and type(self.allow_dirty) is bool
              and type(self.allow_head_change) is bool
              and (self.role != "validator" or self.risk == 1),
              "native_command_risk")
        argv = self.argv
        if argv[0] in ("python", "python3"):
            _need(len(argv) == 2 and argv[1].endswith(".py")
                  and type(self.script_sha256) is str
                  and bool(SHA256.fullmatch(self.script_sha256))
                  and not self.allow_head_change,
                  "native_script_contract")
            path = PurePosixPath(argv[1])
            _need(not path.is_absolute()
                  and all(part and part not in (".", "..")
                          and not part.startswith(".") for part in path.parts)
                  and "\\" not in argv[1]
                  and not argv[1].startswith("-"),
                  "native_script_path")
        elif argv[0] == "git":
            _need(self.script_sha256 is None, "native_git_contract")
            if len(argv) >= 4 and argv[:3] == ("git", "add", "--"):
                _need(self.role == "builder" and self.risk == 2
                      and not self.allow_head_change
                      and set(argv[3:]).issubset(descriptor.allowed_paths),
                      "native_git_contract")
            elif len(argv) == 4 and argv[:3] == ("git", "commit", "-m"):
                _need(self.role == "builder" and self.risk == 2
                      and self.allow_dirty and self.allow_head_change
                      and 1 <= len(argv[3]) <= 100,
                      "native_git_contract")
            elif argv == ("git", "status", "--porcelain"):
                _need(self.risk == 1 and not self.allow_dirty
                      and not self.allow_head_change,
                      "native_git_contract")
            else:
                raise NativeExecutorBlocked("native_git_contract")
        else:
            raise NativeExecutorBlocked("native_command_executable")
        _need(not self.allow_head_change or argv[0] == "git",
              "native_command_head_change")


class NativeGatewayCommandRegistry:
    """Produce exactly the AdapterContext callables backed by a trusted Gateway."""

    def __init__(
        self, *, gateway: ModuleType, policy_path: Path, workspace: Path,
        session_id: str, correlation_id: str, expected_head: str,
        trusted_gateway_sha256: str, trusted_policy_sha256: str,
        specs: Mapping[str, GovernedCommand],
    ) -> None:
        self.gateway = gateway
        self.policy_path = Path(policy_path)
        self.workspace = Path(workspace)
        self.session_id = session_id
        self.correlation_id = correlation_id
        self.trusted_gateway_sha256 = trusted_gateway_sha256
        self.trusted_policy_sha256 = trusted_policy_sha256
        _need(type(specs) is dict and specs
              and all(type(key) is str and type(value) is GovernedCommand
                      and key == value.key for key, value in specs.items()),
              "native_registry_invalid")
        self.specs = dict(specs)
        self.boundary: ExecutionBoundary = attest_execution_boundary(
            gateway=gateway, policy_path=self.policy_path, workspace=self.workspace,
            session_id=session_id, correlation_id=correlation_id,
            expected_head=expected_head,
            trusted_gateway_sha256=trusted_gateway_sha256,
            trusted_policy_sha256=trusted_policy_sha256,
        )

    def for_role(self, descriptor: AdapterDescriptor, role: str) -> dict[str, Callable[[], Any]]:
        descriptor.normalized()
        _need(role in descriptor.roles
              and set(self.specs) == set(descriptor.command_keys),
              "native_registry_descriptor_mismatch")
        for spec in self.specs.values():
            spec.validate(descriptor)
        # AdapterContext requires all keys; keys of the other role remain inert.
        return {key: (lambda item=spec: self._invoke(item, role))
                for key, spec in self.specs.items()}

    def _trusted_policy(self) -> dict[str, Any]:
        try:
            if (hashlib.sha256(Path(self.gateway.__file__).read_bytes()).hexdigest()
                    != self.trusted_gateway_sha256
                    or hashlib.sha256(self.policy_path.read_bytes()).hexdigest()
                    != self.trusted_policy_sha256):
                raise NativeExecutorBlocked("native_authority_changed")
            policy = self.gateway.load_policy(self.policy_path)
            _need(policy.get("require_session_bootstrap") is True
                  and policy.get("require_preflight_snapshot") is True
                  and policy.get("risk2_requires_clean_tree") is True,
                  "native_policy_unprotected")
            return policy
        except NativeExecutorBlocked:
            raise
        except Exception:
            raise NativeExecutorBlocked("native_gateway_unavailable") from None

    def _verify_script(self, spec: GovernedCommand) -> None:
        if spec.script_sha256 is None:
            return
        root = self.workspace.resolve()
        path = root.joinpath(*PurePosixPath(spec.argv[1]).parts)
        try:
            _need(path.is_file() and not path.is_symlink()
                  and path.resolve().is_relative_to(root),
                  "native_script_untrusted")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            raise NativeExecutorBlocked("native_script_untrusted") from None
        _need(digest == spec.script_sha256, "native_script_drift")

    def _readback(self, *, spec: GovernedCommand, before: dict[str, Any],
                  after: dict[str, Any], policy: dict[str, Any]) -> None:
        path = Path(self.gateway.state_dir(policy)) / "events.jsonl"
        try:
            with path.open("rb") as source:
                length = source.seek(0, 2)
                source.seek(max(0, length - MAX_EVENT_BYTES))
                data = source.read(MAX_EVENT_BYTES)
            rows = data.splitlines()
            if length > MAX_EVENT_BYTES and rows:
                rows = rows[1:]
        except OSError:
            raise NativeExecutorBlocked("native_command_receipt_missing") from None
        for line in reversed(rows):
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                raise NativeExecutorBlocked("native_command_receipt_invalid") from None
            if type(event) is not dict:
                raise NativeExecutorBlocked("native_command_receipt_invalid")
            if (event.get("session_id") != self.session_id
                    or event.get("correlation_id") != self.correlation_id
                    or event.get("action") != "run"):
                continue
            _need(event.get("result") == "ok"
                  and event.get("gateway_exit_code") == 0
                  and event.get("command_exit_code") == 0
                  and event.get("risk") == spec.risk
                  and event.get("cwd") == self.gateway.norm(self.workspace)
                  and event.get("command") == list(spec.argv)
                  and event.get("before") == before
                  and event.get("after") == after,
                  "native_command_receipt_mismatch")
            try:
                parsed = datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00"))
            except (ValueError, KeyError):
                raise NativeExecutorBlocked("native_command_receipt_stale") from None
            _need(parsed.tzinfo is not None
                  and -5 <= (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds() <= 60,
                  "native_command_receipt_stale")
            return
        raise NativeExecutorBlocked("native_command_receipt_missing")

    def _invoke(self, spec: GovernedCommand, role: str) -> dict[str, str]:
        _need(spec.role == role, "native_command_wrong_role")
        self.boundary.validate()
        policy = self._trusted_policy()
        self._verify_script(spec)
        try:
            self.gateway.assert_allowed_path(self.workspace, policy)
            self.gateway.enforce_session(policy, self.session_id, self.workspace, spec.risk)
            state = self.gateway.git_state(self.workspace, True, policy)
            before = asdict(state)
            _need(before["head"] == self.boundary.expected_head,
                  "native_command_head_drift")
            # Approval is for this fixed argv, never for arbitrary input.
            self.gateway.validate_command(spec.argv, spec.risk, policy)
            result = self.gateway.execute(
                cwd=self.workspace, policy=policy, args=list(spec.argv),
                risk=spec.risk, timeout=spec.timeout_seconds,
                expected_head=self.boundary.expected_head,
                allow_dirty=spec.allow_dirty,
                allow_head_change=spec.allow_head_change,
                correlation_id=self.correlation_id, session_id=self.session_id,
            )
            _need(result == 0, "native_gateway_command_failed")
            observed = self.gateway.git_state(self.workspace, True, policy)
            after = asdict(observed)
            _need((before["head"] != after["head"]) is spec.allow_head_change,
                  "native_command_post_head")
            self._readback(spec=spec, before=before, after=after, policy=policy)
            return {"result": "GOVERNED_COMMAND_VERIFIED", "head": after["head"]}
        except NativeExecutorBlocked:
            raise
        except Exception:
            raise NativeExecutorBlocked("native_gateway_command_unverified") from None
