"""Content-addressed Git-bundle storage on a PUBLIC issue-bound GitHub branch.

This is a bounded S3-compatible object client for the already validated
S3GitBundleArtifactTransport. It does not execute models, grant new access,
create pull requests, merge, or deploy. Never store secrets or private data.
"""
from __future__ import annotations

import base64
import hashlib
import io
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from app.github_draft_port import OfficialGitHubDraftPort
from app.issue_draft_pr import (
    IssueBinding, HandoffBlocked, PATH, REPO, admit, require,
)
from app.native_one_shot import (
    AdapterResult, DurableArtifact, LocalWorkspace, TaskManifest,
)
from app.s3_git_bundle_artifact import (
    S3ArtifactConfig, S3GitBundleArtifactTransport,
)

CAS_BUCKET = "github-public-cas"
CAS_PREFIX = "qwen7b-cas-v1"
CAS_MAX_BYTES = 96 * 1024
KEY = re.compile(
    r"qwen7b-cas-v1/(?:preflight-v1|git-bundle-v1)/sha256/"
    r"([0-9a-f]{2})/([0-9a-f]{64})\.(?:probe|artifact)\Z"
)


class AlreadyExists(RuntimeError):
    """Equivalent to S3 If-None-Match: * conflict."""

    def __init__(self) -> None:
        super().__init__("cas_key_exists")
        self.response = {
            "Error": {"Code": "PreconditionFailed"},
            "ResponseMetadata": {"HTTPStatusCode": 412},
        }


class GitHubObjectCAS:
    """S3-shaped immutable storage on an issue-scoped GitHub branch."""

    def __init__(self, port: OfficialGitHubDraftPort, binding: IssueBinding):
        require(isinstance(port, OfficialGitHubDraftPort), "cas_port_invalid")
        require(type(binding) is IssueBinding and binding.number > 0
                and re.fullmatch(r"[0-9a-f]{40}", binding.base_sha) is not None
                and binding.branch ==
                f"agent/qwen7b-issue-{binding.number}-{binding.base_sha[:12]}",
                "cas_binding_invalid")
        self.port = port
        self.binding = binding
        self.branch = (
            f"agent/cas-qwen7b-issue-{binding.number}-{binding.base_sha[:12]}"
        )

    @staticmethod
    def _checked_key(bucket: str, key: str) -> tuple[str, str]:
        require(bucket == CAS_BUCKET and type(key) is str,
                "cas_destination_invalid")
        match = KEY.fullmatch(key)
        require(match is not None and match.group(1) == match.group(2)[:2]
                and (("/preflight-v1/" in key and key.endswith(".probe"))
                     or ("/git-bundle-v1/" in key
                         and key.endswith(".artifact"))),
                "cas_key_invalid")
        return "evidence/cas/" + key, match.group(2)

    def _authorized(self) -> None:
        now = datetime.now(timezone.utc)
        main = self.port.get_branch("main")
        require(main is not None, "cas_main_unavailable")
        current = admit(
            self.port.get_issue(self.binding.number),
            self.port.get_comment(self.binding.comment_id),
            main, base_sha=self.binding.base_sha,
            observed_at=now.isoformat(), now=now,
        )
        require(current == self.binding, "cas_issue_binding_changed")

    def _branch(self) -> dict | None:
        result = self.port._request(
            "GET", "/branches/" + quote(self.branch, safe=""),
            missing_ok=True,
        )
        if result is None:
            return None
        require(type(result) is dict and result.get("name") == self.branch
                and type(result.get("commit")) is dict
                and type(result["commit"].get("sha")) is str
                and re.fullmatch(r"[0-9a-f]{40}", result["commit"]["sha"])
                is not None, "cas_branch_unverified")
        return result

    def _ensure_branch(self) -> None:
        branch = self._branch()
        if branch is None:
            result = self.port._request(
                "POST", "/git/refs", statuses=(201,),
                payload={
                    "ref": "refs/heads/" + self.branch,
                    "sha": self.binding.base_sha,
                },
            )
            require(type(result) is dict
                    and result.get("ref") == "refs/heads/" + self.branch
                    and type(result.get("object")) is dict
                    and result["object"].get("sha") == self.binding.base_sha,
                    "cas_branch_create_unverified")
            branch = self._branch()
        require(branch is not None, "cas_branch_unavailable")
        sha = branch["commit"]["sha"]
        if sha == self.binding.base_sha:
            return
        compare = self.port._request(
            "GET", "/compare/" + self.binding.base_sha + "..." + sha,
        )
        require(type(compare) is dict
                and compare.get("status") == "ahead"
                and compare.get("behind_by") == 0
                and type(compare.get("ahead_by")) is int
                and 1 <= compare["ahead_by"] <= 2
                and type(compare.get("total_commits")) is int
                and compare["total_commits"] == compare["ahead_by"]
                and type(compare.get("files")) is list
                and len(compare["files"]) == compare["ahead_by"]
                and all(type(file) is dict
                        and file.get("status") == "added"
                        and type(file.get("filename")) is str
                        and file["filename"].startswith("evidence/cas/")
                        and KEY.fullmatch(
                            file["filename"].removeprefix("evidence/cas/")
                        ) is not None for file in compare["files"]),
                "cas_branch_scope_invalid")

    def _get(self, path: str) -> bytes | None:
        payload = self.port._request(
            "GET", "/contents/" + path,
            query={"ref": self.branch}, missing_ok=True,
        )
        if payload is None:
            return None
        require(type(payload) is dict and payload.get("type") == "file"
                and payload.get("path") == path
                and payload.get("encoding") == "base64"
                and type(payload.get("content")) is str
                and type(payload.get("size")) is int
                and 0 < payload["size"] <= CAS_MAX_BYTES
                and type(payload.get("sha")) is str,
                "cas_object_invalid")
        try:
            encoded = "".join(payload["content"].split())
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            raise HandoffBlocked("cas_object_encoding_invalid") from None
        require(len(raw) == payload["size"]
                and hashlib.sha1(
                    b"blob " + str(len(raw)).encode("ascii") + b"\x00" + raw
                ).hexdigest() == payload["sha"], "cas_blob_mismatch")
        return raw

    def put_object(self, **kwargs: Any) -> dict:
        path, digest = self._checked_key(
            kwargs.get("Bucket"), kwargs.get("Key"),
        )
        body = kwargs.get("Body")
        require(type(body) is bytes and 0 < len(body) <= CAS_MAX_BYTES
                and kwargs.get("ContentLength") == len(body)
                and kwargs.get("IfNoneMatch") == "*"
                and hashlib.sha256(body).hexdigest() == digest
                and type(kwargs.get("Metadata")) is dict
                and kwargs["Metadata"].get("sha256") == digest,
                "cas_write_invalid")
        self._authorized()
        self._ensure_branch()
        if self._get(path) is not None:
            raise AlreadyExists()
        result = self.port._request(
            "PUT", "/contents/" + path, statuses=(201,),
            payload={
                "message": "evidence: append immutable Qwen7B Git CAS object",
                "branch": self.branch,
                "content": base64.b64encode(body).decode("ascii"),
            },
        )
        expected_blob = hashlib.sha1(
            b"blob " + str(len(body)).encode("ascii") + b"\x00" + body
        ).hexdigest()
        require(type(result) is dict
                and type(result.get("content")) is dict
                and result["content"].get("path") == path
                and result["content"].get("sha") == expected_blob
                and type(result.get("commit")) is dict
                and type(result["commit"].get("sha")) is str
                and re.fullmatch(r"[0-9a-f]{40}", result["commit"]["sha"])
                is not None, "cas_write_readback_required")
        return {"ETag": expected_blob}

    def get_object(self, **kwargs: Any) -> dict:
        path, digest = self._checked_key(
            kwargs.get("Bucket"), kwargs.get("Key"),
        )
        require(self._branch() is not None, "cas_branch_unavailable")
        body = self._get(path)
        require(body is not None
                and hashlib.sha256(body).hexdigest() == digest,
                "cas_object_missing_or_modified")
        return {"ContentLength": len(body), "Body": io.BytesIO(body)}


class IssueBoundGitBundleTransport(S3GitBundleArtifactTransport):
    """Only one real issue/base and one reviewed function may use this CAS."""

    def __init__(self, config: S3ArtifactConfig,
                 client: GitHubObjectCAS, binding: IssueBinding):
        super().__init__(config, client)
        require(type(client) is GitHubObjectCAS
                and client.binding == binding, "cas_binding_invalid")
        self.binding = binding

    def _bound(self, manifest: TaskManifest, *,
               validator: bool = False) -> None:
        require(type(manifest) is TaskManifest
                and manifest.repository == REPO
                and manifest.base_sha == self.binding.base_sha
                and (validator or
                     manifest.workspace_sha == self.binding.base_sha)
                and set(manifest.before_sha256) == {PATH}
                and len(manifest.before_sha256[PATH]) == 64,
                "cas_manifest_binding_invalid")

    def publish(self, *, task_id: str, lease_generation: int,
                attempt_id: str, effect_id: str,
                manifest: TaskManifest, workspace: LocalWorkspace,
                result: AdapterResult) -> DurableArtifact:
        self._bound(manifest)
        require(type(result) is AdapterResult
                and result.produced_sha != self.binding.base_sha
                and result.changed_paths == (PATH,),
                "cas_patch_scope_invalid")
        return super().publish(
            task_id=task_id, lease_generation=lease_generation,
            attempt_id=attempt_id, effect_id=effect_id,
            manifest=manifest, workspace=workspace, result=result,
        )

    def verify(self, *, task_id: str, manifest: TaskManifest,
               workspace: LocalWorkspace, produced_sha: str,
               artifact: DurableArtifact) -> None:
        self._bound(manifest, validator=True)
        require(produced_sha != self.binding.base_sha,
                "cas_patch_scope_invalid")
        return super().verify(
            task_id=task_id, manifest=manifest, workspace=workspace,
            produced_sha=produced_sha, artifact=artifact,
        )

    def materialize(self, *, task_id: str, manifest: TaskManifest,
                    workspace: LocalWorkspace,
                    artifact: DurableArtifact) -> AdapterResult:
        self._bound(manifest)
        result = super().materialize(
            task_id=task_id, manifest=manifest,
            workspace=workspace, artifact=artifact,
        )
        require(result.produced_sha != self.binding.base_sha
                and result.changed_paths == (PATH,)
                and workspace.head() == result.produced_sha,
                "cas_materialized_scope_invalid")
        return result


def governed_bundle_transport(
    port: OfficialGitHubDraftPort, binding: IssueBinding,
) -> IssueBoundGitBundleTransport:
    """Build the Git-bundle CAS; actual Builder/Validator run separately."""
    client = GitHubObjectCAS(port, binding)
    return IssueBoundGitBundleTransport(
        S3ArtifactConfig(
            bucket=CAS_BUCKET, region="github", key_prefix=CAS_PREFIX,
            max_artifact_bytes=CAS_MAX_BYTES,
        ),
        client, binding,
    )
