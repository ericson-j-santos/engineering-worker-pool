"""Immutable issue-RAG receipt CAS on the existing issue-authorized GitHub store.

This is an *additional* evidence object, not the durable Git patch/bundle.
It never enqueues, executes code, grants a lease, publishes a draft PR, or
authorizes a GitHub mutation from the model's output.

The provider is a PUBLIC, issue-scoped GitHub CAS. Only bounded citations from
allowlisted PUBLIC documentation are eligible; no issue body, token, private
content, binary input or model prompt is stored. A private backend requires a
separately reviewed transport and matching E2E.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from app.github_cas_transport import (
    CAS_BUCKET, CAS_PREFIX, CAS_MAX_BYTES, GitHubObjectCAS,
)
from app.issue_draft_pr import (
    HandoffBlocked, IssueBinding, PATH, PROFILE, REPO, require,
)
from app.issue_native_adapter import DESCRIPTOR
from app.issue_rag_handoff import (
    BoundIssueRAG, bind_issue_rag,
)
from app.native_one_shot import (
    DurableArtifact, FENCE_ID, TaskManifest,
)
from app.s3_git_bundle_artifact import (
    PREFLIGHT_VALUE, S3ArtifactConfig, S3GitBundleArtifactTransport,
)

KIND = "issue_rag_context_v1"
REF_PREFIX = "cas+issue-rag://sha256/"
MAX_RECEIPT_BYTES = 32 * 1024
KEY_SUBDIR = "issue-rag-v1"
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise HandoffBlocked("rag_cas_serialization_invalid") from None


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise HandoffBlocked("rag_cas_duplicate_key")
        obj[key] = value
    return obj


def _reject_nonfinite(_: str) -> None:
    raise HandoffBlocked("rag_cas_json_invalid")


def _task_binding(binding: IssueBinding, manifest: TaskManifest, *,
                  generation: int, attempt_id: str,
                  effect_id: str) -> dict[str, Any]:
    require(type(binding) is IssueBinding and
            type(manifest) is TaskManifest and
            manifest.repository == REPO and
            manifest.adapter_id == PROFILE and
            manifest.adapter_contract_sha256 == DESCRIPTOR.contract_sha256 and
            manifest.base_sha == binding.base_sha and
            manifest.workspace_sha == binding.base_sha and
            set(manifest.before_sha256) == {PATH} and
            type(manifest.before_sha256[PATH]) is str and
            SHA256.fullmatch(manifest.before_sha256[PATH]) is not None,
            "rag_cas_manifest_mismatch")
    require(type(generation) is int and generation >= 1 and
            type(attempt_id) is str and FENCE_ID.fullmatch(attempt_id) is not None
            and type(effect_id) is str and FENCE_ID.fullmatch(effect_id) is not None,
            "rag_cas_fence_invalid")
    return {
        "task_id": manifest.task_id,
        "repository": REPO,
        "issue_number": binding.number,
        "request_id": binding.request_id,
        "correlation_id": binding.correlation_id,
        "base_sha": binding.base_sha,
        "adapter_id": PROFILE,
        "adapter_contract_sha256": manifest.adapter_contract_sha256,
        "before_sha256": dict(sorted(manifest.before_sha256.items())),
        "lease_generation": generation,
        "attempt_id": attempt_id,
        "effect_id": effect_id,
    }


class IssueRAGReceiptCAS:
    """Conditional append + independent full-byte readback across two ports.

    Caller must construct separate authenticated GitHub clients. Distinct
    instances alone do not prove a different host or process; cross-host
    qualification is an additional physical E2E gate.
    """

    def __init__(self, *, writer: GitHubObjectCAS, independent: GitHubObjectCAS,
                 binding: IssueBinding) -> None:
        require(isinstance(writer, GitHubObjectCAS)
                and isinstance(independent, GitHubObjectCAS)
                and writer is not independent
                and writer.port is not independent.port
                and writer.binding == independent.binding == binding
                and writer.branch == independent.branch,
                "rag_cas_clients_not_independent")
        self.writer = writer
        self.independent = independent
        self.binding = binding
        self._preflight_ok = False

    @staticmethod
    def _key(hex_digest: str) -> str:
        require(type(hex_digest) is str and SHA256.fullmatch(hex_digest) is not None,
                "rag_cas_digest_invalid")
        return (
            f"{CAS_PREFIX}/{KEY_SUBDIR}/sha256/"
            f"{hex_digest[:2]}/{hex_digest}.context"
        )

    @staticmethod
    def _descriptor(digest: str) -> DurableArtifact:
        return DurableArtifact(KIND, REF_PREFIX + digest, digest)

    def preflight(self) -> None:
        """Prove conditional write/read of the existing issue-bound backend.

        This can create only the already reviewed small shared preflight
        object. It must occur before claiming the issue-task lease.
        """
        self.writer._authorized()
        self.independent._authorized()
        config = S3ArtifactConfig(
            bucket=CAS_BUCKET, region="github",
            key_prefix=CAS_PREFIX, max_artifact_bytes=CAS_MAX_BYTES,
        )
        S3GitBundleArtifactTransport(config, self.writer).preflight()
        key = (
            f"{CAS_PREFIX}/preflight-v1/sha256/"
            f"{_digest(PREFLIGHT_VALUE)[:2]}/{_digest(PREFLIGHT_VALUE)}.probe"
        )
        value = self._read(key)
        require(value == PREFLIGHT_VALUE, "rag_cas_preflight_readback_invalid")
        self._preflight_ok = True

    def _read(self, key: str) -> bytes:
        try:
            self.independent._authorized()
            result = self.independent.get_object(Bucket=CAS_BUCKET, Key=key)
            length = result.get("ContentLength")
            stream = result.get("Body")
            require(type(length) is int and 0 < length <= MAX_RECEIPT_BYTES
                    and stream is not None
                    and callable(getattr(stream, "read", None)),
                    "rag_cas_read_invalid")
            try:
                raw = stream.read(MAX_RECEIPT_BYTES + 1)
                extra = stream.read(1)
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
            require(type(raw) is bytes and len(raw) == length
                    and extra in (b"", None), "rag_cas_read_invalid")
            return raw
        except HandoffBlocked:
            raise
        except Exception:
            raise HandoffBlocked("rag_cas_independent_read_unavailable") from None

    def publish(self, *, manifest: TaskManifest, lease_generation: int,
                attempt_id: str, effect_id: str,
                receipt: Mapping[str, Any],
                independent_source_read: Callable[[str, str], bytes],
                ) -> DurableArtifact:
        require(self._preflight_ok is True, "rag_cas_preflight_required")
        task = _task_binding(
            self.binding, manifest, generation=lease_generation,
            attempt_id=attempt_id, effect_id=effect_id,
        )
        # Authorization and independent source verification PRECEDE any
        # object creation (the prior preflight is independently gated).
        self.writer._authorized()
        self.independent._authorized()
        verified = bind_issue_rag(
            self.binding, receipt, independent_source_read,
        )
        source = {
            "path": verified.source_path, "sha256": verified.source_sha256,
            "blob_sha": verified.source_blob_sha, "line": verified.line,
            "url": verified.url,
        }
        envelope = {
            "schema_version": KIND,
            "task": task,
            "source": source,
            "rag_receipt": dict(receipt),
        }
        raw = _canonical(envelope)
        require(0 < len(raw) <= MAX_RECEIPT_BYTES,
                "rag_cas_envelope_oversized")
        full_digest = _digest(raw)
        key = self._key(full_digest)
        # 412, uncertain connection and completed PUT all take the same
        # full, independent readback route. Never blind-retry the write.
        write_error = False
        try:
            self.writer.put_object(
                Bucket=CAS_BUCKET, Key=key, Body=raw,
                ContentLength=len(raw),
                ContentType="application/vnd.reqsys.issue-rag-context-v1",
                Metadata={"sha256": full_digest, "artifact-kind": KIND},
                IfNoneMatch="*",
            )
        except HandoffBlocked as error:
            # Deterministic policy/ancestry/capacity rejection must NEVER
            # be promoted into success by a readable object. Only an
            # ambiguous provider response can be reconciled by full bytes.
            if str(error) != "cas_write_readback_required":
                raise
            write_error = True
        except Exception:
            write_error = True
        try:
            observed = self._read(key)
        except HandoffBlocked:
            raise HandoffBlocked("rag_cas_publication_ambiguous") from None
        require(observed == raw, "rag_cas_conflict" if write_error
                else "rag_cas_readback_mismatch")
        return self._descriptor(full_digest)

    def materialize(self, *, artifact: DurableArtifact,
                    manifest: TaskManifest, lease_generation: int,
                    attempt_id: str, effect_id: str,
                    independent_source_read: Callable[[str, str], bytes],
                    ) -> tuple[dict[str, Any], BoundIssueRAG]:
        task = _task_binding(
            self.binding, manifest, generation=lease_generation,
            attempt_id=attempt_id, effect_id=effect_id,
        )
        require(type(artifact) is DurableArtifact
                and artifact.artifact_kind == KIND
                and type(artifact.artifact_sha256) is str
                and SHA256.fullmatch(artifact.artifact_sha256) is not None
                and artifact.artifact_ref ==
                REF_PREFIX + artifact.artifact_sha256,
                "rag_cas_artifact_invalid")
        raw = self._read(self._key(artifact.artifact_sha256))
        require(_digest(raw) == artifact.artifact_sha256,
                "rag_cas_artifact_tampered")
        try:
            envelope = json.loads(
                raw.decode("ascii"), object_pairs_hook=_unique_pairs,
                parse_constant=_reject_nonfinite,
            )
        except HandoffBlocked:
            raise
        except (UnicodeError, ValueError, TypeError):
            raise HandoffBlocked("rag_cas_json_invalid") from None
        require(type(envelope) is dict
                and set(envelope) == {
                    "schema_version", "task", "source", "rag_receipt",
                }
                and envelope["schema_version"] == KIND
                and envelope["task"] == task
                and _canonical(envelope) == raw,
                "rag_cas_envelope_identity_invalid")
        receipt = envelope.get("rag_receipt")
        require(type(receipt) is dict, "rag_cas_receipt_invalid")
        verified = bind_issue_rag(
            self.binding, receipt, independent_source_read,
        )
        require(envelope["source"] == {
            "path": verified.source_path, "sha256": verified.source_sha256,
            "blob_sha": verified.source_blob_sha, "line": verified.line,
            "url": verified.url,
        }, "rag_cas_citation_changed")
        return receipt, verified
