"""Opt-in HTTPS composition for native workers; no discovery or provisioning.

Only trusted deployment code supplies the authority, CA and credential providers.
Task manifests cannot provide routes, credentials, commands or gateway attestations.
The local API and Windows DPAPI contracts remain unchanged.
"""
from __future__ import annotations

import ipaddress
import json
import re
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

import certifi
import httpx

from app.native_gateway_preflight import attest_execution_boundary
from app.native_one_shot import (
    LocalWorkspace, NativeAdapter, NativeExecutorBlocked, NativeOneShotExecutor,
    ReceiptStore, TaskManifest,
)
from app.native_pool_http import LocalWorkerApi, MAX_RESPONSE_BYTES, TOKEN_RE
from app.s3_git_bundle_artifact import (
    S3ArtifactConfig, S3Client, S3GitBundleArtifactTransport,
)


def _need(ok: bool, code: str) -> None:
    if not ok:
        raise NativeExecutorBlocked(code)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("nonfinite")


@dataclass(frozen=True, repr=False)
class HttpsWorkerRoute:
    """Deployment-owned exact origin. ca_pem is a CA certificate, never a key."""
    origin: str
    ca_pem: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _need(type(self.origin) is str and 1 <= len(self.origin) <= 512
              and self.origin.isascii() and "%" not in self.origin,
              "worker_origin_invalid")
        try:
            parsed = urlsplit(self.origin)
            host, port = parsed.hostname, parsed.port
        except ValueError:
            raise NativeExecutorBlocked("worker_origin_invalid") from None
        _need(parsed.scheme == "https" and bool(host)
              and parsed.username is None and parsed.password is None
              and parsed.path == "" and not parsed.query and not parsed.fragment,
              "worker_origin_invalid")
        host = str(host)
        try:
            address = ipaddress.ip_address(host)
            authority = "[" + host + "]" if address.version == 6 else host
            _need(str(address) == host, "worker_origin_invalid")
        except ValueError:
            _need(len(host) <= 253 and all(re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label,
            ) for label in host.split(".")), "worker_origin_invalid")
            authority = host
        _need(port is None or (1 <= port <= 65535 and port != 443),
              "worker_origin_invalid")
        expected = "https://" + authority + (":" + str(port) if port else "")
        _need(self.origin == expected, "worker_origin_invalid")
        _need(self.ca_pem is None or (
            type(self.ca_pem) is str and 1 <= len(self.ca_pem) <= 1_048_576
            and "PRIVATE KEY" not in self.ca_pem
        ), "worker_ca_invalid")

    def ssl_context(self) -> ssl.SSLContext:
        # Unlike create_default_context(), this does not enable SSLKEYLOGFILE.
        # CA selection is explicit; SSL_CERT_FILE/DIR and proxies are not inherited.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            if self.ca_pem is None:
                context.load_verify_locations(cafile=certifi.where())
            else:
                context.load_verify_locations(cadata=self.ca_pem)
        except (OSError, ValueError, ssl.SSLError):
            raise NativeExecutorBlocked("worker_ca_invalid") from None
        _need(context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
              and context.keylog_filename is None, "worker_tls_unverified")
        return context


@dataclass(frozen=True, repr=False)
class PosixWorkerTokenFile:
    """Reuse the existing ownership, mode, hardlink and O_NOFOLLOW checks."""
    token_file: Path

    def __call__(self) -> str:
        return LocalWorkerApi._token(self)


class HttpsWorkerApi(LocalWorkerApi):
    """Same exact lifecycle, separate TLS transport, no retries or redirects.

    A provider is trusted installed code (e.g. PosixWorkerTokenFile), not a token
    in a task or a promise that Windows ACLs were verified. The client is owned
    here; arbitrary HTTP clients/SSL contexts are deliberately not injectable.
    """
    def __init__(self, *, route: HttpsWorkerRoute,
                 token_provider: Callable[[], str]) -> None:
        _need(type(route) is HttpsWorkerRoute, "worker_origin_invalid")
        _need(callable(token_provider), "worker_token_provider_invalid")
        self._route = route
        self._provider = token_provider
        self._owned = True
        self._client = httpx.Client(
            transport=httpx.HTTPTransport(
                verify=route.ssl_context(), retries=0, trust_env=False,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            ),
            trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(8.0), headers={"Accept-Encoding": "identity"},
        )

    def _token(self) -> str:
        try:
            value = self._provider()
        except Exception:
            raise NativeExecutorBlocked("worker_token_provider_unavailable") from None
        _need(type(value) is str and bool(TOKEN_RE.fullmatch(value)),
              "worker_token_invalid")
        return value

    def _request(self, method: str, path: str,
                 payload: dict[str, Any] | None = None, *,
                 expected_statuses: tuple[int, ...] = (200,)) -> dict[str, Any]:
        _need(type(method) is str and type(path) is str and len(path) <= 128,
              "worker_route_invalid")
        task = r"/v1/tasks/cwp-[0-9a-f]{24}"
        allowed = (
            method == "GET" and bool(re.fullmatch(task + r"(?:/checkpoints/latest)?", path))
        ) or (
            method == "POST" and (
                path in {"/v1/claims/exact", "/v1/claims/fenced/exact"}
                or bool(re.fullmatch(
                    task + r"/(?:start|validation|complete|fail|block|checkpoints)", path))
            )
        )
        _need(allowed, "worker_route_invalid")
        _need((method == "GET" and payload is None)
              or (method == "POST" and type(payload) is dict), "worker_payload_invalid")
        try:
            body = (json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
                    if method == "POST" else None)
        except (ValueError, TypeError):
            raise NativeExecutorBlocked("worker_payload_invalid") from None
        _need(body is None or len(body) <= MAX_RESPONSE_BYTES, "worker_payload_invalid")
        target = self._route.origin + path
        headers = {"Authorization": "Bearer " + self._token(),
                   "Accept": "application/json", "Accept-Encoding": "identity"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        deadline = time.monotonic() + 8.0
        try:
            with self._client.stream(method, target, headers=headers, content=body) as reply:
                _need(str(reply.url) == target and not reply.history,
                      "worker_response_origin_mismatch")
                if reply.status_code == 401:
                    raise NativeExecutorBlocked("worker_auth_rejected")
                _need(reply.status_code in expected_statuses, "worker_http_unverified")
                _need(reply.headers.get("content-type", "").split(";")[0].strip()
                      == "application/json"
                      and reply.headers.get("content-encoding", "identity") == "identity",
                      "worker_response_invalid")
                chunks = bytearray()
                for chunk in reply.iter_raw():
                    _need(time.monotonic() <= deadline
                          and len(chunks) + len(chunk) <= MAX_RESPONSE_BYTES,
                          "worker_response_invalid")
                    chunks.extend(chunk)
            data = json.loads(bytes(chunks), object_pairs_hook=_unique,
                              parse_constant=_reject_constant)
        except (httpx.HTTPError, OSError):
            # Preserve the existing checkpoint readback-on-ambiguous-POST contract.
            raise NativeExecutorBlocked("worker_transport_unverified") from None
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise NativeExecutorBlocked("worker_response_invalid") from None
        finally:
            self._client.cookies.clear()
        _need(type(data) is dict, "worker_response_invalid")
        return data


@dataclass(frozen=True, repr=False)
class GatewayAuthority:
    """Pinned local trust inputs, supplied by the deployment, not by the task."""
    gateway: ModuleType
    policy_path: Path
    session_id: str
    correlation_id: str
    expected_head: str
    gateway_sha256: str
    policy_sha256: str


class RemoteNativeExecutor:
    """One-shot composition. Neither a new queue nor a host-selection scheduler.

    Caller owns the S3 client's protected identity/lifetime and must install
    Gateway-backed command registries. No bucket, secret, worker or service is
    created. No binding is read from a task manifest.
    """
    def __init__(
        self, *, route: HttpsWorkerRoute, token_provider: Callable[[], str],
        s3_config: S3ArtifactConfig, s3_client: S3Client,
        workspace: LocalWorkspace, receipts: ReceiptStore,
        adapters: Mapping[str, NativeAdapter],
        command_registries: Mapping[str, Mapping[str, Callable[[], Any]]],
        authority: GatewayAuthority, fenced_adapter_ids: frozenset[str],
    ) -> None:
        _need(type(route) is HttpsWorkerRoute and callable(token_provider),
              "remote_runtime_config_invalid")
        _need(type(authority) is GatewayAuthority, "remote_runtime_authority_invalid")
        _need(callable(getattr(s3_client, "put_object", None))
              and callable(getattr(s3_client, "get_object", None)),
              "artifact_store_config_invalid")
        self.route, self.token_provider = route, token_provider
        self.artifacts = S3GitBundleArtifactTransport(s3_config, s3_client)
        self.workspace, self.receipts = workspace, receipts
        self.adapters, self.commands = dict(adapters), dict(command_registries)
        self.authority, self.fenced_adapter_ids = authority, frozenset(fenced_adapter_ids)

    def run(self, raw_manifest: Any, *, role: str, worker_id: str) -> dict[str, Any]:
        manifest = TaskManifest.parse(raw_manifest)
        _need(role in {"builder", "validator"}
              and type(worker_id) is str
              and bool(re.fullmatch(r"[a-z][a-z0-9_.-]{2,127}", worker_id)),
              "remote_runtime_identity_invalid")
        authority = self.authority
        _need(manifest.workspace_sha == authority.expected_head,
              "boundary_manifest_drift")
        boundary = attest_execution_boundary(
            gateway=authority.gateway, policy_path=authority.policy_path,
            workspace=self.workspace.root, session_id=authority.session_id,
            correlation_id=authority.correlation_id, expected_head=authority.expected_head,
            trusted_gateway_sha256=authority.gateway_sha256,
            trusted_policy_sha256=authority.policy_sha256,
        )
        # Network/credential use starts only after independent local attestation.
        with HttpsWorkerApi(route=self.route, token_provider=self.token_provider) as api:
            executor = NativeOneShotExecutor(
                api=api, workspace=self.workspace, receipts=self.receipts,
                adapters=self.adapters, command_registries=self.commands,
                boundary=boundary, artifact_transport=self.artifacts,
                fenced_adapter_ids=self.fenced_adapter_ids,
            )
            return executor.run(raw_manifest, role=role, worker_id=worker_id)
