"""Native executor boundary verified against a pinned local Command Gateway.

This is a pre-claim verifier over the Gateway's own session reservation,
preflight snapshot, fresh read-only inspect event and live Git state.
The authority module and policy hashes MUST come from a trusted deployment,
not a task manifest, GitHub issue or unverified workflow input.
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from app.native_one_shot import ExecutionBoundary, NativeExecutorBlocked

HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TAIL_BYTES = 1_048_576


def _need(ok: bool, reason: str) -> None:
    if not ok:
        raise NativeExecutorBlocked(reason)


def _digest(path: Path) -> str:
    _need(path.is_file() and not path.is_symlink(), "native_authority_file_invalid")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        raise NativeExecutorBlocked("native_authority_file_invalid") from None


def _timestamp(value: Any, now: datetime, window: int) -> datetime:
    _need(type(value) is str and len(value) <= 45, "native_attestation_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise NativeExecutorBlocked("native_attestation_timestamp") from None
    _need(parsed.tzinfo is not None and parsed.utcoffset() is not None,
          "native_attestation_timestamp")
    delta = (now - parsed.astimezone(UTC)).total_seconds()
    _need(-5 <= delta <= window, "native_attestation_expired")
    return parsed.astimezone(UTC)


def _inspect_event(log: Path, *, session_id: str, correlation_id: str,
                   normalized_root: str, state: dict[str, Any], now: datetime,
                   window: int, not_before: datetime) -> None:
    _need(log.is_file() and not log.is_symlink(), "native_gateway_event_missing")
    try:
        with log.open("rb") as stream:
            size = stream.seek(0, 2)
            start = max(0, size - TAIL_BYTES)
            stream.seek(start)
            raw = stream.read(TAIL_BYTES)
    except OSError:
        raise NativeExecutorBlocked("native_gateway_event_missing") from None
    lines = raw.splitlines()
    if start and lines:
        lines = lines[1:]  # First line may be truncated at the tail boundary.
    for raw_line in reversed(lines):
        try:
            event = json.loads(raw_line)
        except (ValueError, UnicodeDecodeError):
            raise NativeExecutorBlocked("native_gateway_event_invalid") from None
        if type(event) is not dict:
            raise NativeExecutorBlocked("native_gateway_event_invalid")
        if event.get("session_id") != session_id or event.get("correlation_id") != correlation_id:
            continue
        _need(event.get("action") == "inspect" and event.get("result") == "ok"
              and event.get("risk") == 1
              and event.get("cwd") == normalized_root
              and event.get("before") == state,
              "native_gateway_inspection_unverified")
        _need(_timestamp(event.get("timestamp"), now, window) >= not_before,
              "native_gateway_inspection_stale")
        return
    raise NativeExecutorBlocked("native_gateway_event_missing")


def attest_execution_boundary(
    *, gateway: ModuleType, policy_path: Path, workspace: Path,
    session_id: str, correlation_id: str, expected_head: str,
    trusted_gateway_sha256: str, trusted_policy_sha256: str,
    max_age_seconds: int = 300,
) -> ExecutionBoundary:
    """Admit a materialized, unchanged session after a real Gateway inspection.

    Must execute in the already-authorized operational path, not as a shell
    fallback. This does NOT prove that later adapter commands use the Gateway.
    """
    _need(type(gateway) is ModuleType
          and gateway.__name__ == "command_gateway"
          and type(getattr(gateway, "__file__", None)) is str,
          "native_authority_invalid")
    _need(type(session_id) is str and bool(re.fullmatch(r"[A-Za-z0-9._-]{3,64}", session_id))
          and type(correlation_id) is str
          and bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{7,127}", correlation_id))
          and type(expected_head) is str and bool(HEX40.fullmatch(expected_head)),
          "native_identity_invalid")
    _need(type(max_age_seconds) is int and 1 <= max_age_seconds <= 300,
          "native_window_invalid")
    _need(type(trusted_gateway_sha256) is str
          and bool(HEX64.fullmatch(trusted_gateway_sha256))
          and type(trusted_policy_sha256) is str
          and bool(HEX64.fullmatch(trusted_policy_sha256)),
          "native_trusted_digest_invalid")

    policy_path = Path(policy_path).resolve()
    workspace = Path(workspace).absolute()
    _need(workspace.is_dir() and not workspace.is_symlink()
          and workspace.resolve() == workspace,
          "native_workspace_invalid")
    _need(_digest(Path(gateway.__file__).resolve()) == trusted_gateway_sha256
          and _digest(policy_path) == trusted_policy_sha256,
          "native_authority_changed")
    try:
        policy = gateway.load_policy(policy_path)
        _need(policy.get("require_session_bootstrap") is True
              and policy.get("require_preflight_snapshot") is True
              and policy.get("risk2_requires_clean_tree") is True,
              "native_policy_unprotected")
        gateway.assert_allowed_path(workspace, policy)
        reservation = gateway.enforce_session(policy, session_id, workspace, 2)
        snapshot = gateway.validate_session_snapshot(policy, session_id, reservation)
        live = gateway.git_state(workspace, True, policy)
        root = gateway.norm(workspace)
        _need(gateway.norm(str(reservation.get("reserved_worktree", ""))) == root
              and reservation.get("status") == "materialized"
              and type(snapshot) is dict and snapshot.get("result") == "BOOTSTRAP_OK"
              and snapshot.get("state_validated") is True
              and snapshot.get("session_id") == session_id
              and snapshot.get("correlation_id") == correlation_id
              and snapshot.get("reservation_status") == "materialized"
              and snapshot.get("target_path") == root
              and snapshot.get("host") == socket.gethostname()
              and snapshot.get("gateway_sha256") == trusted_gateway_sha256
              and snapshot.get("policy_sha256") == trusted_policy_sha256
              and snapshot.get("rules_version") == policy.get("rules_version")
              and snapshot.get("base_matches_reservation") is True
              and reservation.get("base_head") == expected_head,
              "native_session_not_attested")
        state = asdict(live) if live is not None else {}
        _need(state.get("head") == expected_head
              and state.get("status_count") == 0
              and snapshot.get("target_state") == state,
              "native_workspace_drift")
        now = datetime.now(UTC)
        issued = _timestamp(snapshot.get("timestamp"), now, max_age_seconds)
        event = Path(gateway.state_dir(policy)) / "events.jsonl"
        _inspect_event(event, session_id=session_id, correlation_id=correlation_id,
                       normalized_root=root, state=state, now=now,
                       window=max_age_seconds, not_before=issued)
    except NativeExecutorBlocked:
        raise
    except Exception:
        raise NativeExecutorBlocked("native_gateway_verification_failed") from None

    boundary = ExecutionBoundary(session_id=session_id, correlation_id=correlation_id,
                                 expected_head=expected_head, state_validated=True,
                                 gateway_verified=True)
    boundary.validate()
    return boundary
