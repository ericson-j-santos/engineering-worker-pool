"""Read-only receipt replay for the existing remote native composition.

A completed Builder has legitimately changed its checkout. It cannot satisfy
the unchanged-workspace admission gate again. This separate path verifies the
original reservation and current result, then grants GET/verify capability only.
It never weakens RemoteNativeExecutor.run(), refreshes an expired session, or
turns a missing/invalid receipt into a fresh execution.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import socket
import stat
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from app.native_gateway_preflight import _digest, _inspect_event, _timestamp
from app.native_one_shot import (
    ExecutionBoundary, NativeExecutorBlocked, NativeOneShotExecutor, ReceiptStore,
    TaskManifest,
)
from app.native_remote_runtime import (
    GatewayAuthority, HttpsWorkerApi, RemoteNativeExecutor, _reject_constant, _unique,
)
from app.s3_git_bundle_artifact import S3GitBundleArtifactTransport

MAX_RECEIPT_BYTES = 65_536


def _need(ok: bool, code: str) -> None:
    if not ok:
        raise NativeExecutorBlocked(code)


def _receipt_bytes(path: Path) -> bytes:
    """Bounded POSIX file read; Windows needs a separately qualified provider."""
    _need(os.name == "posix" and hasattr(os, "O_NOFOLLOW"),
          "readback_platform_unqualified")
    fd = None
    try:
        _need(path.absolute() == path.resolve() and not path.is_symlink(),
              "readback_receipt_invalid")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(fd)
        _need(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
              and before.st_nlink == 1 and before.st_mode & 0o077 == 0
              and 0 < before.st_size <= MAX_RECEIPT_BYTES,
              "readback_receipt_invalid")
        data = bytearray()
        while len(data) <= MAX_RECEIPT_BYTES:
            chunk = os.read(fd, min(8192, MAX_RECEIPT_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(fd)
        observed = path.stat(follow_symlinks=False)
        signature = lambda item: (
            item.st_dev, item.st_ino, item.st_mode, item.st_uid, item.st_nlink,
            item.st_size, item.st_mtime_ns, item.st_ctime_ns,
        )
        _need(signature(before) == signature(after) == signature(observed)
              and len(data) == before.st_size, "readback_receipt_changed")
        return bytes(data)
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, NativeExecutorBlocked):
            raise
        raise NativeExecutorBlocked("readback_receipt_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)


@contextlib.contextmanager
def _attest_result(runtime: RemoteNativeExecutor, manifest: TaskManifest,
                   produced: str):
    """Fresh inspect of the result without replacing the original preflight."""
    authority = runtime.authority
    _need(type(authority) is GatewayAuthority, "native_authority_invalid")
    gateway = authority.gateway
    _need(type(gateway) is ModuleType and gateway.__name__ == "command_gateway"
          and type(getattr(gateway, "__file__", None)) is str,
          "native_authority_invalid")
    _need(manifest.workspace_sha == authority.expected_head,
          "boundary_manifest_drift")
    _need(type(authority.session_id) is str
          and bool(re.fullmatch(r"[A-Za-z0-9._-]{3,64}", authority.session_id))
          and type(authority.correlation_id) is str
          and bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{7,127}", authority.correlation_id))
          and all(type(value) is str and bool(re.fullmatch(r"[0-9a-f]{64}", value))
                  for value in (authority.gateway_sha256, authority.policy_sha256)),
          "native_identity_invalid")
    boundary = ExecutionBoundary(
        authority.session_id, authority.correlation_id, authority.expected_head,
        True, True,
    )
    boundary.validate()
    _need(type(produced) is str and bool(re.fullmatch(r"[0-9a-f]{40}", produced)),
          "receipt_produced_sha")
    workspace = Path(runtime.workspace.root).absolute()
    policy_path = Path(authority.policy_path).absolute()
    _need(workspace.is_dir() and workspace.resolve() == workspace
          and policy_path.resolve() == policy_path,
          "native_workspace_invalid")

    def pinned() -> None:
        _need(_digest(Path(gateway.__file__).resolve()) == authority.gateway_sha256
              and _digest(policy_path) == authority.policy_sha256,
              "native_authority_changed")

    pinned()
    try:
        policy = gateway.load_policy(policy_path)
        _need(policy.get("require_session_bootstrap") is True
              and policy.get("require_preflight_snapshot") is True
              and policy.get("risk2_requires_clean_tree") is True,
              "native_policy_unprotected")
        gateway.assert_allowed_path(workspace, policy)
        reservation = gateway.enforce_session(
            policy, authority.session_id, workspace, 2,
        )
        snapshot = gateway.validate_session_snapshot(
            policy, authority.session_id, reservation,
        )
        root = gateway.norm(workspace)
        _need(type(snapshot) is dict and snapshot.get("result") == "BOOTSTRAP_OK"
              and snapshot.get("state_validated") is True
              and snapshot.get("session_id") == authority.session_id
              and snapshot.get("correlation_id") == authority.correlation_id
              and snapshot.get("reservation_status") == "materialized"
              and reservation.get("status") == "materialized"
              and snapshot.get("target_path") == root
              and gateway.norm(str(reservation.get("reserved_worktree", ""))) == root
              and snapshot.get("host") == socket.gethostname()
              and snapshot.get("gateway_sha256") == authority.gateway_sha256
              and snapshot.get("policy_sha256") == authority.policy_sha256
              and snapshot.get("rules_version") == policy.get("rules_version")
              and snapshot.get("base_matches_reservation") is True
              and reservation.get("base_head") == authority.expected_head,
              "native_session_not_attested")
        now = datetime.now(UTC)
        issued = _timestamp(snapshot.get("timestamp"), now, 300)
        saved = snapshot.get("target_state")
        live_state = gateway.git_state(workspace, True, policy)
        live = asdict(live_state)
        _need(type(saved) is dict and saved.get("head") == authority.expected_head
              and saved.get("status_count") == 0
              and live.get("head") == produced and live.get("status_count") == 0
              and type(live.get("status_digest")) is str
              and bool(re.fullmatch(r"[0-9a-f]{64}", live["status_digest"]))
              and {**saved, "head": produced,
                   "status_digest": live["status_digest"]} == live,
              "native_workspace_drift")
        # Keep the workspace locked until receipt/canonical/artifact verification
        # and the final independent local readback have all completed.
        with gateway.repository_lock(live_state, policy, authority.correlation_id):
            _need(asdict(gateway.git_state(workspace, True, policy)) == live,
                  "native_workspace_drift")
            with contextlib.redirect_stdout(io.StringIO()):
                _need(gateway.inspect(
                    workspace, policy, authority.correlation_id, authority.session_id,
                ) == 0, "native_gateway_inspection_unverified")
            _need(asdict(gateway.git_state(workspace, True, policy)) == live,
                  "native_workspace_drift")
            pinned()
            _inspect_event(
                Path(gateway.state_dir(policy)) / "events.jsonl",
                session_id=authority.session_id, correlation_id=authority.correlation_id,
                normalized_root=root, state=live, now=datetime.now(UTC), window=300,
                not_before=issued,
            )
            yield boundary
            pinned()
            _need(asdict(gateway.git_state(workspace, True, policy)) == live,
                  "native_workspace_drift")
    except NativeExecutorBlocked:
        raise
    except Exception:
        raise NativeExecutorBlocked("native_gateway_verification_failed") from None


class _ReadbackApi(HttpsWorkerApi):
    def _request(self, method, path, payload=None, *, expected_statuses=(200,)):
        _need(method == "GET" and payload is None, "readback_effect_forbidden")
        return super()._request(
            method, path, payload, expected_statuses=expected_statuses,
        )


class _VerifyOnlyArtifacts:
    def __init__(self, transport: S3GitBundleArtifactTransport):
        self.transport = transport

    def verify(self, **kwargs):
        return self.transport.verify(**kwargs)

    def preflight(self, **kwargs):
        raise NativeExecutorBlocked("readback_effect_forbidden")

    publish = preflight
    materialize = preflight


class _BorrowedLock:
    def unlink(self, *, missing_ok=False):
        # The outer owner alone releases the real lock after final readback.
        return None


class _LockedReceipt:
    """Reuse the held task/role lock without acquiring a second reservation."""
    def __init__(self, lock, path, raw, task_id, role):
        self.lock, self.path, self.raw = lock, path, raw
        self.identity = (task_id, role)
        self.used = False

    def acquire(self, task_id, role):
        _need(not self.used and (task_id, role) == self.identity,
              "readback_receipt_identity")
        _need(_receipt_bytes(self.path) == self.raw, "readback_receipt_changed")
        self.used = True
        return _BorrowedLock(), self.path

    @staticmethod
    def write(*args, **kwargs):
        raise NativeExecutorBlocked("readback_effect_forbidden")


def readback_remote_result(runtime: RemoteNativeExecutor, raw_manifest: Any, *,
                           role: str, worker_id: str) -> dict[str, Any]:
    """Revalidate an existing result only. Missing receipt never means execute.

    The original executor retains all receipt/manifest/workspace/checkpoint/fence
    and canonical task checks. No adapter command registry or mutating HTTP/S3
    capability is supplied, even if a concurrent invalidation removes a receipt.
    """
    _need(type(runtime) is RemoteNativeExecutor
          and type(runtime.receipts) is ReceiptStore
          and type(runtime.artifacts) is S3GitBundleArtifactTransport,
          "readback_runtime_invalid")
    _need(type(role) is str and role in {"builder", "validator"} and type(worker_id) is str
          and bool(re.fullmatch(r"[a-z][a-z0-9_.-]{2,127}", worker_id)),
          "remote_runtime_identity_invalid")
    manifest = TaskManifest.parse(raw_manifest)
    lock, path = runtime.receipts.acquire(manifest.task_id, role)
    try:
        raw = _receipt_bytes(path)
        try:
            receipt = json.loads(raw, object_pairs_hook=_unique,
                                 parse_constant=_reject_constant)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise NativeExecutorBlocked("readback_receipt_invalid") from None
        _need(type(receipt) is dict, "readback_receipt_invalid")
        with _attest_result(runtime, manifest, receipt.get("produced_sha")) as boundary:
            pinned_receipt = _LockedReceipt(lock, path, raw, manifest.task_id, role)
            with _ReadbackApi(
                route=runtime.route, token_provider=runtime.token_provider,
            ) as api:
                executor = NativeOneShotExecutor(
                    api=api, workspace=runtime.workspace, receipts=pinned_receipt,
                    adapters=runtime.adapters, command_registries={},
                    boundary=boundary,
                    artifact_transport=_VerifyOnlyArtifacts(runtime.artifacts),
                    fenced_adapter_ids=runtime.fenced_adapter_ids,
                )
                result = executor.run(raw_manifest, role=role, worker_id=worker_id)
            _need(result.get("replayed") is True and _receipt_bytes(path) == raw,
                  "readback_result_unverified")
            runtime.workspace.validate_replay(receipt["produced_sha"])
            return result
    finally:
        lock.unlink(missing_ok=True)
