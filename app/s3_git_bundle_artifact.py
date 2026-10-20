"""Immutable S3 CAS transport for fenced native Git results.

The task never supplies a bucket, endpoint, key, or credential.  A trusted host
constructs this transport, and checkpoints carry only a content-addressed
descriptor.  Each object is a self-contained canonical header followed by a
Git bundle, so another executor can recover without a shared filesystem.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit

from app.native_one_shot import (
    AdapterResult,
    DurableArtifact,
    LocalWorkspace,
    NativeExecutorBlocked,
    TaskManifest,
)


ARTIFACT_KIND = "git_bundle_v1"
ARTIFACT_REF_PREFIX = "cas+git-bundle://sha256/"
MAGIC = b"CWP-GIT-BUNDLE-V1\n"
PREFLIGHT_VALUE = b"CWP-S3-PREFLIGHT-V1\n"
HEADER_LIMIT = 65_536
BUNDLE_HEADER_LIMIT = 65_536
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SAFE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
SAFE_PREFIX = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,255}$")
SAFE_REGION = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
SAFE_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


class S3Client(Protocol):
    def put_object(self, **kwargs: Any) -> Any: ...
    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...


def _blocked(code: str) -> None:
    raise NativeExecutorBlocked(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        _blocked(code)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite number")


def _is_precondition_failed(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return False
    error_value = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = error_value.get("Code") if isinstance(error_value, Mapping) else None
    status = (metadata.get("HTTPStatusCode")
              if isinstance(metadata, Mapping) else None)
    return code in {"PreconditionFailed", "412"} or status == 412


def _safe_relative_path(value: Any) -> str:
    _require(type(value) is str and 1 <= len(value) <= 240,
             "artifact_manifest_invalid")
    path = PurePosixPath(value)
    _require(not path.is_absolute() and "\\" not in value
             and ".." not in path.parts
             and all(part not in {"", "."} and not part.startswith(".")
                     for part in path.parts), "artifact_manifest_invalid")
    return path.as_posix()


def _task_manifest_value(manifest: TaskManifest) -> dict[str, Any]:
    return {
        "schema_version": manifest.schema_version,
        "task_id": manifest.task_id,
        "repository": manifest.repository,
        "base_sha": manifest.base_sha,
        "workspace_sha": manifest.workspace_sha,
        "adapter_id": manifest.adapter_id,
        "adapter_contract_sha256": manifest.adapter_contract_sha256,
        "before_sha256": dict(sorted(manifest.before_sha256.items())),
    }


@dataclass(frozen=True)
class S3ArtifactConfig:
    bucket: str
    region: str
    key_prefix: str = "codex-artifacts"
    endpoint_url: str | None = None
    max_artifact_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        _require(type(self.bucket) is str and bool(SAFE_BUCKET.fullmatch(self.bucket))
                 and ".." not in self.bucket and ".-" not in self.bucket
                 and "-." not in self.bucket,
                 "artifact_store_config_invalid")
        _require(type(self.region) is str and bool(SAFE_REGION.fullmatch(self.region)),
                 "artifact_store_config_invalid")
        _require(type(self.key_prefix) is str
                 and bool(SAFE_PREFIX.fullmatch(self.key_prefix))
                 and not self.key_prefix.endswith("/")
                 and all(part not in {"", ".", ".."}
                         for part in self.key_prefix.split("/")),
                 "artifact_store_config_invalid")
        _require(type(self.max_artifact_bytes) is int
                 and not isinstance(self.max_artifact_bytes, bool)
                 and 1_024 <= self.max_artifact_bytes <= 1_073_741_824,
                 "artifact_store_config_invalid")
        if self.endpoint_url is not None:
            _require(type(self.endpoint_url) is str
                     and 1 <= len(self.endpoint_url) <= 2_048
                     and self.endpoint_url.isascii()
                     and not any(character.isspace()
                                 for character in self.endpoint_url),
                     "artifact_store_config_invalid")
            try:
                parsed = urlsplit(self.endpoint_url)
                hostname = parsed.hostname
            except ValueError:
                _blocked("artifact_store_config_invalid")
            _require(parsed.scheme == "https" and bool(hostname)
                     and parsed.username is None and parsed.password is None
                     and parsed.query == "" and parsed.fragment == ""
                     and parsed.path in {"", "/"},
                     "artifact_store_config_invalid")


class S3GitBundleArtifactTransport:
    """Append-only Git bundle transport backed by an S3-compatible store."""

    def __init__(self, config: S3ArtifactConfig, client: S3Client):
        _require(type(config) is S3ArtifactConfig and client is not None,
                 "artifact_store_config_invalid")
        self.config = config
        self.client = client

    @classmethod
    def from_boto3(cls, config: S3ArtifactConfig) -> S3GitBundleArtifactTransport:
        """Build a SigV4/path-style client using only boto3's credential chain."""
        try:
            import boto3  # type: ignore[import-not-found]
            from botocore.config import Config  # type: ignore[import-not-found]
        except ImportError:
            _blocked("artifact_store_dependency_missing")
        try:
            client = boto3.client(
                "s3",
                region_name=config.region,
                endpoint_url=config.endpoint_url,
                verify=True,
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    retries={"mode": "standard", "total_max_attempts": 1},
                    # Only the endpoint validated above may override the SDK
                    # default.  In particular, ignore AWS_ENDPOINT_URL and
                    # endpoint_url values from shared botocore configuration.
                    ignore_configured_endpoint_urls=True,
                ),
            )
        except Exception:
            _blocked("artifact_store_config_invalid")
        return cls(config, client)

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None,
    ) -> S3GitBundleArtifactTransport:
        """Load only non-secret routing settings; boto3 resolves credentials."""
        values = os.environ if environ is None else environ
        try:
            maximum = int(values.get(
                "CODEX_ARTIFACT_S3_MAX_BYTES", str(64 * 1024 * 1024),
            ))
        except (TypeError, ValueError):
            _blocked("artifact_store_config_invalid")
        config = S3ArtifactConfig(
            bucket=values.get("CODEX_ARTIFACT_S3_BUCKET", ""),
            region=(values.get("CODEX_ARTIFACT_S3_REGION")
                    or values.get("AWS_REGION")
                    or values.get("AWS_DEFAULT_REGION") or ""),
            key_prefix=values.get(
                "CODEX_ARTIFACT_S3_PREFIX", "codex-artifacts",
            ),
            endpoint_url=(values.get("CODEX_ARTIFACT_S3_ENDPOINT")
                          or values.get("AWS_ENDPOINT_URL_S3") or None),
            max_artifact_bytes=maximum,
        )
        return cls.from_boto3(config)

    def preflight(self) -> None:
        """Prove conditional write authorization and exact object readback."""
        digest = hashlib.sha256(PREFLIGHT_VALUE).hexdigest()
        key = (f"{self.config.key_prefix}/preflight-v1/sha256/"
               f"{digest[:2]}/{digest}.probe")
        try:
            self.client.put_object(
                Bucket=self.config.bucket,
                Key=key,
                Body=PREFLIGHT_VALUE,
                ContentLength=len(PREFLIGHT_VALUE),
                ContentType="application/vnd.reqsys.artifact-preflight-v1",
                Metadata={"sha256": digest, "artifact-kind": "preflight_v1"},
                IfNoneMatch="*",
            )
        except Exception as error:
            # An explicit 412 proves that the conditional write reached the
            # object store. AccessDenied and transport failures stay blocked,
            # even if another credential created the probe previously.
            if not _is_precondition_failed(error):
                _blocked("artifact_store_unavailable")
        try:
            observed = self._download(key)
        except NativeExecutorBlocked:
            _blocked("artifact_store_unavailable")
        _require(observed == PREFLIGHT_VALUE, "artifact_store_unavailable")

    def _key(self, digest: str) -> str:
        _require(bool(SHA256.fullmatch(digest)), "durable_artifact_invalid")
        return (f"{self.config.key_prefix}/git-bundle-v1/sha256/"
                f"{digest[:2]}/{digest}.artifact")

    @staticmethod
    def _descriptor_digest(artifact: DurableArtifact) -> str:
        _require(type(artifact) is DurableArtifact
                 and artifact.artifact_kind == ARTIFACT_KIND
                 and bool(SHA256.fullmatch(artifact.artifact_sha256))
                 and artifact.artifact_ref
                 == ARTIFACT_REF_PREFIX + artifact.artifact_sha256,
                 "durable_artifact_invalid")
        return artifact.artifact_sha256

    def _download(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.config.bucket, Key=key)
            length = response.get("ContentLength")
            body = response.get("Body")
            _require(type(length) is int and not isinstance(length, bool)
                     and 0 < length <= self.config.max_artifact_bytes
                     and body is not None and callable(getattr(body, "read", None)),
                     "artifact_store_read_invalid")
            chunks: list[bytes] = []
            remaining = self.config.max_artifact_bytes + 1
            try:
                while remaining > 0:
                    requested = min(1_048_576, remaining)
                    chunk = body.read(requested)
                    _require(type(chunk) is bytes and len(chunk) <= requested,
                             "artifact_store_read_invalid")
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                value = b"".join(chunks)
                extra = body.read(1)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
            _require(len(value) == length
                     and len(value) <= self.config.max_artifact_bytes
                     and extra in {b"", None}, "artifact_store_read_invalid")
            return value
        except NativeExecutorBlocked:
            raise
        except Exception:
            _blocked("artifact_store_read_failed")

    def _put_cas(self, key: str, value: bytes, digest: str) -> None:
        put_failed = False
        try:
            self.client.put_object(
                Bucket=self.config.bucket,
                Key=key,
                Body=value,
                ContentLength=len(value),
                ContentType="application/vnd.reqsys.git-bundle-v1",
                Metadata={"sha256": digest, "artifact-kind": ARTIFACT_KIND},
                IfNoneMatch="*",
            )
        except Exception:
            put_failed = True
        try:
            observed = self._download(key)
        except NativeExecutorBlocked:
            _blocked("artifact_publish_ambiguous")
        if observed != value:
            _blocked("artifact_cas_conflict" if put_failed
                     else "artifact_publish_ambiguous")

    @staticmethod
    def _git(
        workspace: LocalWorkspace, hooks: Path, *args: str,
        allowed_codes: tuple[int, ...] = (0,), timeout: int = 60,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "git", "-c", f"core.hooksPath={hooks}",
            "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false",
            "-c", f"core.attributesFile={os.devnull}",
            "-c", "protocol.allow=never", "-c", "protocol.ext.allow=never",
            # Recovery fetches only the verified, transport-created bundle path.
            # No task-controlled URL or submodule operation reaches this helper.
            "-c", "protocol.file.allow=always", *args,
        ]
        try:
            result = subprocess.run(
                command, cwd=workspace.root, text=True, capture_output=True,
                timeout=timeout, check=False,
                env={
                    "PATH": os.environ.get("PATH", os.defpath),
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_SYSTEM": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_ALLOW_PROTOCOL": "file",
                    "LC_ALL": "C",
                },
            )
        except (OSError, subprocess.TimeoutExpired):
            _blocked("artifact_git_failed")
        _require(result.returncode in allowed_codes
                 and len(result.stdout) <= 1_048_576
                 and len(result.stderr) <= 1_048_576,
                 "artifact_git_failed")
        return result

    @classmethod
    def _require_repository_root(
        cls, workspace: LocalWorkspace, hooks: Path,
    ) -> None:
        top_level = cls._git(
            workspace, hooks, "rev-parse", "--show-toplevel",
        ).stdout.strip()
        _require(bool(top_level)
                 and Path(top_level).resolve() == workspace.root.resolve(),
                 "artifact_workspace_invalid")

    @classmethod
    def _validate_workspace_for_publish(
        cls, workspace: LocalWorkspace, manifest: TaskManifest,
        result: AdapterResult, hooks: Path,
    ) -> None:
        _require(workspace.root.is_dir() and not workspace.root.is_symlink()
                 and workspace.status() == ""
                 and workspace.head() == result.produced_sha,
                 "artifact_workspace_invalid")
        cls._require_repository_root(workspace, hooks)
        _require(cls._git(workspace, hooks, "remote").stdout.strip() == "",
                 "artifact_workspace_invalid")
        for ancestor_sha in (manifest.base_sha, manifest.workspace_sha):
            ancestor = cls._git(
                workspace, hooks, "merge-base", "--is-ancestor",
                ancestor_sha, result.produced_sha,
                allowed_codes=(0, 1),
            )
            _require(ancestor.returncode == 0, "artifact_base_not_ancestor")
        _require(workspace.changed_paths(manifest.base_sha, result.produced_sha)
                 == result.changed_paths, "artifact_result_paths")

    @classmethod
    def _bundle_bytes(
        cls, workspace: LocalWorkspace, manifest: TaskManifest,
        result: AdapterResult, maximum: int,
    ) -> bytes:
        with tempfile.TemporaryDirectory(prefix="cwp-artifact-") as name:
            temporary = Path(name)
            hooks = temporary / "hooks"
            hooks.mkdir(mode=0o700)
            cls._validate_workspace_for_publish(workspace, manifest, result, hooks)
            descriptor, bundle_name = tempfile.mkstemp(
                prefix="bundle-", suffix=".gitbundle", dir=temporary,
            )
            os.close(descriptor)
            bundle = Path(bundle_name)
            bundle.chmod(0o600)
            # The recovery workspace is required to have workspace_sha.  Carry
            # only the produced delta instead of copying all history reachable
            # from HEAD (which could disclose unrelated historical content).
            cls._git(
                workspace, hooks, "bundle", "create", "--version=2", str(bundle),
                "HEAD", f"^{manifest.workspace_sha}",
            )
            size = bundle.stat().st_size
            _require(0 < size <= maximum, "artifact_too_large")
            value = bundle.read_bytes()
            _require(len(value) == size, "artifact_git_failed")
            cls._validate_bundle_header(
                value, manifest.workspace_sha, result.produced_sha,
            )
            cls._verify_bundle(workspace, bundle, result.produced_sha, hooks)
            return value

    @staticmethod
    def _validate_bundle_header(
        bundle: bytes, workspace_sha: str, produced_sha: str,
    ) -> None:
        """Accept exactly one v2 prerequisite and one advertised HEAD ref."""
        _require(type(bundle) is bytes
                 and bool(SHA40.fullmatch(workspace_sha))
                 and bool(SHA40.fullmatch(produced_sha)),
                 "artifact_bundle_invalid")
        boundary = bundle.find(b"\n\n")
        _require(0 < boundary <= BUNDLE_HEADER_LIMIT
                 and bundle[boundary + 2:boundary + 6] == b"PACK",
                 "artifact_bundle_invalid")
        lines = bundle[:boundary].split(b"\n")
        _require(bool(lines) and lines[0] == b"# v2 git bundle",
                 "artifact_bundle_invalid")
        prerequisites: list[str] = []
        references: list[tuple[str, bytes]] = []
        reference_seen = False
        for line in lines[1:]:
            if line.startswith(b"-") and not reference_seen:
                _require(len(line) >= 42 and line[41:42] == b" "
                         and b"\x00" not in line and b"\r" not in line,
                         "artifact_bundle_invalid")
                try:
                    sha = line[1:41].decode("ascii")
                except UnicodeDecodeError:
                    _blocked("artifact_bundle_invalid")
                _require(bool(SHA40.fullmatch(sha)), "artifact_bundle_invalid")
                prerequisites.append(sha)
                continue
            reference_seen = True
            parts = line.split(b" ", 1)
            _require(len(parts) == 2 and parts[1] == b"HEAD",
                     "artifact_bundle_invalid")
            try:
                sha = parts[0].decode("ascii")
            except UnicodeDecodeError:
                _blocked("artifact_bundle_invalid")
            _require(bool(SHA40.fullmatch(sha)), "artifact_bundle_invalid")
            references.append((sha, parts[1]))
        _require(prerequisites == [workspace_sha]
                 and references == [(produced_sha, b"HEAD")],
                 "artifact_bundle_invalid")

    @classmethod
    def _verify_bundle(
        cls, workspace: LocalWorkspace, bundle: Path,
        produced_sha: str, hooks: Path,
    ) -> None:
        cls._git(workspace, hooks, "bundle", "verify", str(bundle))
        heads = cls._git(
            workspace, hooks, "bundle", "list-heads", str(bundle),
        ).stdout.splitlines()
        parsed: list[tuple[str, str]] = []
        for line in heads:
            parts = line.strip().split(" ", 1)
            _require(len(parts) == 2 and bool(SHA40.fullmatch(parts[0])),
                     "artifact_bundle_invalid")
            parsed.append((parts[0], parts[1]))
        _require(parsed == [(produced_sha, "HEAD")],
                 "artifact_bundle_invalid")

    @staticmethod
    def _result_value(result: AdapterResult) -> dict[str, Any]:
        _require(type(result) is AdapterResult
                 and type(result.produced_sha) is str
                 and bool(SHA40.fullmatch(result.produced_sha))
                 and type(result.changed_paths) is tuple
                 and type(result.invoked_commands) is tuple
                 and type(result.evidence_sha256) is str
                 and bool(SHA256.fullmatch(result.evidence_sha256)),
                 "artifact_result_invalid")
        paths = tuple(_safe_relative_path(path) for path in result.changed_paths)
        _require(bool(paths) and paths == tuple(sorted(set(paths))),
                 "artifact_result_invalid")
        commands = tuple(result.invoked_commands)
        _require(1 <= len(commands) <= 64 and len(commands) == len(set(commands))
                 and all(type(item) is str and bool(SAFE_NAME.fullmatch(item))
                         for item in commands), "artifact_result_invalid")
        return {
            "produced_sha": result.produced_sha,
            "changed_paths": list(paths),
            "invoked_commands": list(commands),
            "evidence_sha256": result.evidence_sha256,
        }

    @classmethod
    def _header(
        cls, *, manifest: TaskManifest, lease_generation: int,
        attempt_id: str, effect_id: str, result: AdapterResult,
        bundle: bytes,
    ) -> dict[str, Any]:
        _require(type(lease_generation) is int
                 and not isinstance(lease_generation, bool)
                 and lease_generation >= 1
                 and type(attempt_id) is str
                 and bool(SAFE_ID.fullmatch(attempt_id))
                 and type(effect_id) is str and bool(SAFE_ID.fullmatch(effect_id)),
                 "artifact_fence_invalid")
        return {
            "schema_version": 1,
            "artifact_kind": ARTIFACT_KIND,
            "task_manifest": _task_manifest_value(manifest),
            "lease_generation": lease_generation,
            "attempt_id": attempt_id,
            "effect_id": effect_id,
            "result": cls._result_value(result),
            "bundle": {
                "sha256": hashlib.sha256(bundle).hexdigest(),
                "size": len(bundle),
            },
        }

    @staticmethod
    def _encode(header: Mapping[str, Any], bundle: bytes, maximum: int) -> bytes:
        header_bytes = _canonical(header)
        _require(0 < len(header_bytes) <= HEADER_LIMIT,
                 "artifact_manifest_invalid")
        value = MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes + bundle
        _require(len(value) <= maximum, "artifact_too_large")
        return value

    @classmethod
    def _decode(
        cls, value: bytes, digest: str, manifest: TaskManifest,
        produced_sha: str | None = None, *,
        allow_result_workspace: bool = False,
    ) -> tuple[dict[str, Any], bytes, AdapterResult]:
        _require(type(value) is bytes and hashlib.sha256(value).hexdigest() == digest
                 and value.startswith(MAGIC)
                 and len(value) >= len(MAGIC) + 4,
                 "artifact_integrity_invalid")
        offset = len(MAGIC)
        header_length = struct.unpack(">I", value[offset:offset + 4])[0]
        start = offset + 4
        end = start + header_length
        _require(0 < header_length <= HEADER_LIMIT and end < len(value),
                 "artifact_integrity_invalid")
        header_bytes, bundle = value[start:end], value[end:]
        try:
            header = json.loads(
                header_bytes.decode("ascii"),
                object_pairs_hook=_pairs_without_duplicates,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
            _blocked("artifact_manifest_invalid")
        _require(type(header) is dict and _canonical(header) == header_bytes
                 and set(header) == {
                     "schema_version", "artifact_kind", "task_manifest",
                     "lease_generation", "attempt_id", "effect_id", "result",
                     "bundle",
                 }
                 and header["schema_version"] == 1
                 and header["artifact_kind"] == ARTIFACT_KIND
                 and type(header["lease_generation"]) is int
                 and not isinstance(header["lease_generation"], bool)
                 and header["lease_generation"] >= 1
                 and type(header["attempt_id"]) is str
                 and bool(SAFE_ID.fullmatch(header["attempt_id"]))
                 and type(header["effect_id"]) is str
                 and bool(SAFE_ID.fullmatch(header["effect_id"])),
                 "artifact_manifest_invalid")
        bundle_header = header["bundle"]
        _require(type(bundle_header) is dict
                 and set(bundle_header) == {"sha256", "size"}
                 and type(bundle_header["size"]) is int
                 and not isinstance(bundle_header["size"], bool)
                 and bundle_header["size"] == len(bundle) and len(bundle) > 0
                 and type(bundle_header["sha256"]) is str
                 and bool(SHA256.fullmatch(bundle_header["sha256"]))
                 and hashlib.sha256(bundle).hexdigest() == bundle_header["sha256"],
                 "artifact_integrity_invalid")
        raw_result = header["result"]
        _require(type(raw_result) is dict and set(raw_result) == {
            "produced_sha", "changed_paths", "invoked_commands", "evidence_sha256",
        }, "artifact_manifest_invalid")
        result = AdapterResult(
            raw_result["produced_sha"], tuple(raw_result["changed_paths"])
            if type(raw_result["changed_paths"]) is list else (),
            tuple(raw_result["invoked_commands"])
            if type(raw_result["invoked_commands"]) is list else (),
            raw_result["evidence_sha256"],
        )
        cls._result_value(result)
        _require(produced_sha is None or result.produced_sha == produced_sha,
                 "artifact_result_invalid")
        expected_manifest = _task_manifest_value(manifest)
        if header["task_manifest"] != expected_manifest:
            # Publication remains immutable.  Only verification may receive a
            # manifest describing the produced checkout instead of its input.
            _require(allow_result_workspace and produced_sha is not None
                     and manifest.workspace_sha == result.produced_sha,
                     "artifact_manifest_invalid")
            try:
                publication = TaskManifest.parse(header["task_manifest"])
            except NativeExecutorBlocked:
                _blocked("artifact_manifest_invalid")
            publication_value = _task_manifest_value(publication)
            _require(publication_value == header["task_manifest"]
                     and set(manifest.before_sha256) == set(publication.before_sha256)
                     and {
                         **expected_manifest,
                         "workspace_sha": publication.workspace_sha,
                         "before_sha256": publication_value["before_sha256"],
                     } == publication_value, "artifact_manifest_invalid")
        return header, bundle, result

    @staticmethod
    def _write_bundle(directory: Path, bundle: bytes) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix="received-", suffix=".gitbundle", dir=directory,
        )
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(bundle)
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(0o600)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return path

    @classmethod
    def _reject_external_filters(
        cls, workspace: LocalWorkspace, hooks: Path, produced_sha: str,
    ) -> None:
        result = cls._git(
            workspace, hooks, "grep", "-n", "-I", "-E",
            r"(^|[[:space:]])filter([[:space:]]|=|$)", produced_sha, "--",
            ".gitattributes", ":(glob)**/.gitattributes",
            allowed_codes=(0, 1),
        )
        _require(result.returncode == 1, "artifact_checkout_filter_forbidden")
        local_attributes = cls._git(
            workspace, hooks, "rev-parse", "--git-path", "info/attributes",
        ).stdout.strip()
        _require(bool(local_attributes), "artifact_workspace_invalid")
        path = Path(local_attributes)
        if not path.is_absolute():
            path = workspace.root / path
        if path.exists() or path.is_symlink():
            _require(path.is_file() and not path.is_symlink()
                     and path.stat().st_size == 0,
                     "artifact_checkout_attributes_forbidden")

    @classmethod
    def _require_checkout_pristine(
        cls, workspace: LocalWorkspace, hooks: Path,
    ) -> None:
        status = cls._git(
            workspace, hooks, "status", "--porcelain=v1",
            "--untracked-files=all", "--ignored=matching",
        ).stdout
        _require(status.strip() == "", "artifact_workspace_ignored_files")

    def publish(
        self, *, task_id: str, lease_generation: int, attempt_id: str,
        effect_id: str, manifest: TaskManifest, workspace: LocalWorkspace,
        result: AdapterResult,
    ) -> DurableArtifact:
        _require(type(manifest) is TaskManifest and task_id == manifest.task_id,
                 "artifact_task_mismatch")
        bundle = self._bundle_bytes(
            workspace, manifest, result,
            self.config.max_artifact_bytes - len(MAGIC) - 4 - HEADER_LIMIT,
        )
        header = self._header(
            manifest=manifest, lease_generation=lease_generation,
            attempt_id=attempt_id, effect_id=effect_id, result=result,
            bundle=bundle,
        )
        value = self._encode(header, bundle, self.config.max_artifact_bytes)
        digest = hashlib.sha256(value).hexdigest()
        self._put_cas(self._key(digest), value, digest)
        return DurableArtifact(
            ARTIFACT_KIND, ARTIFACT_REF_PREFIX + digest, digest,
        )

    def verify(
        self, *, task_id: str, manifest: TaskManifest,
        workspace: LocalWorkspace, produced_sha: str,
        artifact: DurableArtifact,
    ) -> None:
        _require(type(manifest) is TaskManifest and task_id == manifest.task_id,
                 "artifact_task_mismatch")
        digest = self._descriptor_digest(artifact)
        value = self._download(self._key(digest))
        header, bundle, result = self._decode(
            value, digest, manifest, produced_sha, allow_result_workspace=True,
        )
        publication = header["task_manifest"]
        self._validate_bundle_header(
            bundle, publication["workspace_sha"], produced_sha,
        )
        _require(workspace.status() == "" and workspace.head() == produced_sha,
                 "artifact_workspace_invalid")
        if publication != _task_manifest_value(manifest):
            # The result manifest must describe these exact checkout bytes,
            # including unchanged paths from the original adapter scope.
            for relative, expected in manifest.before_sha256.items():
                path = workspace.root / _safe_relative_path(relative)
                try:
                    _require(path.is_file() and not path.is_symlink()
                             and path.stat().st_nlink == 1
                             and path.resolve().is_relative_to(workspace.root),
                             "artifact_workspace_invalid")
                    actual = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    _blocked("artifact_workspace_invalid")
                _require(actual == expected, "artifact_workspace_invalid")
        with tempfile.TemporaryDirectory(prefix="cwp-verify-") as name:
            directory = Path(name)
            hooks = directory / "hooks"
            hooks.mkdir(mode=0o700)
            path = self._write_bundle(directory, bundle)
            self._require_repository_root(workspace, hooks)
            self._verify_bundle(workspace, path, produced_sha, hooks)
            _require(self._git(workspace, hooks, "remote").stdout.strip() == "",
                     "artifact_workspace_invalid")
            for ancestor_sha in (manifest.base_sha, publication["workspace_sha"]):
                ancestor = self._git(
                    workspace, hooks, "merge-base", "--is-ancestor",
                    ancestor_sha, produced_sha, allowed_codes=(0, 1),
                )
                _require(ancestor.returncode == 0, "artifact_base_not_ancestor")
        _require(workspace.changed_paths(manifest.base_sha, produced_sha)
                 == result.changed_paths, "artifact_result_paths")

    def materialize(
        self, *, task_id: str, manifest: TaskManifest,
        workspace: LocalWorkspace, artifact: DurableArtifact,
    ) -> AdapterResult:
        _require(type(manifest) is TaskManifest and task_id == manifest.task_id,
                 "artifact_task_mismatch")
        digest = self._descriptor_digest(artifact)
        value = self._download(self._key(digest))
        _header, bundle, result = self._decode(value, digest, manifest)
        self._validate_bundle_header(
            bundle, manifest.workspace_sha, result.produced_sha,
        )
        _require(workspace.root.is_dir() and not workspace.root.is_symlink()
                 and workspace.status() == ""
                 and workspace.head() == manifest.workspace_sha,
                 "artifact_workspace_invalid")
        with tempfile.TemporaryDirectory(prefix="cwp-materialize-") as name:
            directory = Path(name)
            hooks = directory / "hooks"
            hooks.mkdir(mode=0o700)
            self._require_repository_root(workspace, hooks)
            self._require_checkout_pristine(workspace, hooks)
            _require(self._git(workspace, hooks, "remote").stdout.strip() == "",
                     "artifact_workspace_invalid")
            path = self._write_bundle(directory, bundle)
            self._verify_bundle(workspace, path, result.produced_sha, hooks)
            self._git(
                workspace, hooks, "fetch", "--no-tags", "--no-write-fetch-head",
                str(path), "HEAD",
            )
            for ancestor_sha in (manifest.base_sha, manifest.workspace_sha):
                ancestor = self._git(
                    workspace, hooks, "merge-base", "--is-ancestor",
                    ancestor_sha, result.produced_sha, allowed_codes=(0, 1),
                )
                _require(ancestor.returncode == 0, "artifact_base_not_ancestor")
            self._reject_external_filters(workspace, hooks, result.produced_sha)
            self._git(
                workspace, hooks, "checkout", "--detach", "--force",
                result.produced_sha,
            )
            _require(self._git(workspace, hooks, "remote").stdout.strip() == "",
                     "artifact_workspace_invalid")
            self._require_checkout_pristine(workspace, hooks)
        _require(workspace.status() == "" and workspace.head() == result.produced_sha
                 and workspace.changed_paths(manifest.base_sha, result.produced_sha)
                 == result.changed_paths, "artifact_materialization_invalid")
        return result
