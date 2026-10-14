"""Fail-closed loopback HTTP transport for the governed native one-shot executor.

This module only implements exact task lifecycle calls. It never registers a
worker, selects arbitrary queued work, manages credentials or initiates a task.
ExecutionBoundary and the product adapter must be attested separately.
"""
from __future__ import annotations

import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.native_one_shot import CORRELATION, NAME, SHA40, TASK_ID, NativeExecutorBlocked

TOKEN_RE = re.compile(r"[!-~]{16,4096}")
MAX_RESPONSE_BYTES = 65_536


def _need(condition: bool, reason: str) -> None:
    if not condition:
        raise NativeExecutorBlocked(reason)


def _id(task_id: str) -> str:
    _need(type(task_id) is str and bool(TASK_ID.fullmatch(task_id)),
          "worker_task_id_invalid")
    return task_id


def _worker(worker_id: str, correlation_id: str) -> None:
    _need(type(worker_id) is str and bool(NAME.fullmatch(worker_id)),
          "worker_id_invalid")
    _need(type(correlation_id) is str and bool(CORRELATION.fullmatch(correlation_id)),
          "correlation_invalid")


class LocalWorkerApi:
    """Exact-claim WorkerApi client; fixed IPv4 loopback only and no retries."""

    def __init__(self, *, port: int, token_file: Path,
                 client: httpx.Client | None = None) -> None:
        _need(type(port) is int and 1 <= port <= 65_535, "worker_port_invalid")
        self.base_url = f"http://127.0.0.1:{port}"
        self.token_file = Path(token_file)
        self._owned = client is None
        self._client = client if client is not None else httpx.Client(
            trust_env=False, follow_redirects=False, timeout=8,
        )

    def close(self) -> None:
        if self._owned:
            self._client.close()

    def __enter__(self) -> LocalWorkerApi:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _token(self) -> str:
        # The token source is trusted configuration, never a queue manifest.
        # Windows ACL attestation is not implemented; block use there instead
        # of claiming that a POSIX mode check validates NTFS permissions.
        _need(os.name == "posix" and hasattr(os, "O_NOFOLLOW"),
              "worker_token_acl_unverified")
        try:
            fd = os.open(self.token_file, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as source:
                state = os.fstat(source.fileno())
                _need(stat.S_ISREG(state.st_mode) and state.st_nlink == 1
                      and state.st_uid == os.getuid()
                      and state.st_mode & 0o077 == 0,
                      "worker_token_permissions")
                raw = source.read(4097)
        except OSError:
            raise NativeExecutorBlocked("worker_token_file_unavailable") from None
        _need(16 <= len(raw) <= 4096, "worker_token_invalid")
        try:
            token = raw.decode("ascii").strip()
        except UnicodeDecodeError:
            raise NativeExecutorBlocked("worker_token_invalid") from None
        _need(bool(TOKEN_RE.fullmatch(token)), "worker_token_invalid")
        return token

    def _request(self, method: str, path: str,
                 payload: dict[str, Any] | None = None) -> dict[str, Any]:
        _need(method in {"GET", "POST"} and path.startswith("/v1/")
              and "//" not in path and "?" not in path and "#" not in path,
              "worker_route_invalid")
        target = self.base_url + path
        try:
            result = self._client.request(
                method, target,
                headers={"Authorization": "Bearer " + self._token(),
                         "Accept": "application/json"},
                json=payload if method == "POST" else None,
                timeout=8,
            )
        except (httpx.HTTPError, OSError):
            # POST effects are ambiguous after a transport error. Never retry.
            raise NativeExecutorBlocked("worker_transport_unverified") from None
        _need(str(result.url) == target and not result.history,
              "worker_response_origin_mismatch")
        if result.status_code == 401:
            raise NativeExecutorBlocked("worker_auth_rejected")
        _need(result.status_code == 200, "worker_http_unverified")
        _need(len(result.content) <= MAX_RESPONSE_BYTES
              and result.headers.get("content-type", "").split(";")[0].strip()
              == "application/json", "worker_response_invalid")
        try:
            data = result.json()
        except ValueError:
            raise NativeExecutorBlocked("worker_response_invalid") from None
        _need(type(data) is dict, "worker_response_invalid")
        return data

    @staticmethod
    def _task(task: dict[str, Any], task_id: str) -> dict[str, Any]:
        _need(task.get("task_id") == task_id
              and type(task.get("state")) is str
              and task["state"] in {
                  "queued", "leased", "running", "validating",
                  "blocked", "completed", "failed",
              }
              and "lease_token" not in task, "worker_task_readback_invalid")
        return task

    def get_task(self, task_id: str) -> dict[str, Any]:
        _id(task_id)
        return self._task(self._request("GET", f"/v1/tasks/{task_id}"), task_id)

    def claim_exact(self, worker_id: str, task_id: str,
                    correlation_id: str) -> dict[str, Any]:
        _worker(worker_id, correlation_id)
        _id(task_id)
        reply = self._request("POST", "/v1/claims/exact", {
            "worker_id": worker_id, "task_id": task_id,
            "correlation_id": correlation_id,
        })
        _need(type(reply.get("claimed")) is bool, "worker_claim_invalid")
        if reply["claimed"] is False:
            _need(reply.get("task") is None and reply.get("lease") is None,
                  "worker_claim_invalid")
            return reply
        task, lease = reply.get("task"), reply.get("lease")
        _need(type(task) is dict and type(lease) is dict
              and task.get("task_id") == task_id
              and task.get("leased_by") == worker_id
              and lease.get("task_id") == task_id
              and lease.get("worker_id") == worker_id
              and type(lease.get("lease_token")) is str
              and 16 <= len(lease["lease_token"]) <= 128
              and type(lease.get("role")) is str
              and lease["role"] in {"builder", "validator"}
              and task.get("state") in {"leased", "validating"},
              "worker_claim_invalid")
        # A claimed lease must be time-bound and explicitly UTC-offset-aware.
        # Never trust the transport's claimed=true without an expiry contract.
        expires = lease.get("lease_expires_at")
        _need(type(expires) is str and 20 <= len(expires) <= 48,
              "worker_claim_invalid")
        try:
            deadline = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            raise NativeExecutorBlocked("worker_claim_invalid") from None
        _need(deadline.tzinfo is not None and deadline.utcoffset() is not None
              and deadline.astimezone(UTC) > datetime.now(UTC),
              "worker_claim_invalid")
        self._task(task, task_id)
        return reply

    def _mutate(self, task_id: str, worker_id: str, lease_token: str,
                correlation_id: str, operation: str, *,
                produced_sha: str | None = None,
                reason: str | None = None) -> None:
        _worker(worker_id, correlation_id)
        _id(task_id)
        _need(type(lease_token) is str and 16 <= len(lease_token) <= 128,
              "worker_lease_invalid")
        _need(operation in {"start", "validation", "complete", "fail"},
              "worker_operation_invalid")
        if operation == "validation":
            _need(type(produced_sha) is str and bool(SHA40.fullmatch(produced_sha)),
                  "worker_validation_sha_invalid")
        if operation == "fail":
            _need(type(reason) is str and bool(re.fullmatch(r"[a-z_]{3,64}", reason)),
                  "worker_failure_reason_invalid")
        body: dict[str, Any] = {"worker_id": worker_id,
                                "lease_token": lease_token,
                                "correlation_id": correlation_id}
        if operation == "validation":
            body["produced_sha"] = produced_sha
        if operation == "fail":
            body["reason"] = reason
        after = self._task(
            self._request("POST", f"/v1/tasks/{task_id}/{operation}", body), task_id,
        )
        expected_states = {
            "start": {"running"}, "validation": {"validating"},
            "complete": {"completed"}, "fail": {"queued", "blocked", "failed"},
        }[operation]
        _need(after["state"] in expected_states, "worker_mutation_unverified")
        independent = self.get_task(task_id)
        _need(independent["state"] == after["state"]
              and independent.get("produced_sha") == after.get("produced_sha"),
              "worker_mutation_readback_mismatch")

    def start(self, task_id: str, worker_id: str, lease_token: str,
              correlation_id: str) -> None:
        self._mutate(task_id, worker_id, lease_token, correlation_id, "start")

    def submit_validation(self, task_id: str, worker_id: str, lease_token: str,
                          produced_sha: str, correlation_id: str) -> None:
        self._mutate(task_id, worker_id, lease_token, correlation_id,
                     "validation", produced_sha=produced_sha)

    def complete(self, task_id: str, worker_id: str, lease_token: str,
                 correlation_id: str) -> None:
        self._mutate(task_id, worker_id, lease_token, correlation_id, "complete")

    def fail(self, task_id: str, worker_id: str, lease_token: str,
             correlation_id: str, reason: str) -> None:
        self._mutate(task_id, worker_id, lease_token, correlation_id,
                     "fail", reason=reason)
