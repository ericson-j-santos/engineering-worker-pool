"""Regression/E2E of fenced issue-RAG CAS using distinct simulated GitHub ports.

This is a protocol-level two-client test, NOT a live GitHub write and NOT a
two-host physical handoff. No owner authorization for the current issue SHA is
fabricated or posted by this test.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import pytest

from app.github_cas_transport import (
    CAS_BUCKET, CAS_PREFIX, AlreadyExists, GitHubObjectCAS,
)
from app.github_draft_port import OfficialGitHubDraftPort
from app.issue_draft_pr import HandoffBlocked, PATH, REPO
from app.issue_native_adapter import DESCRIPTOR
from app.issue_rag_receipt_cas import (
    IssueRAGReceiptCAS, KIND, REF_PREFIX, KEY_SUBDIR, MAX_RECEIPT_BYTES,
)
from app.native_one_shot import DurableArtifact, TaskManifest
from test_issue_rag_handoff import binding, receipt_for, CONTENT


def blob_sha(raw: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(raw)).encode("ascii") + b"\x00" + raw,
    ).hexdigest()


@dataclass
class GitHubState:
    main_sha: str = "a" * 40
    branch_sha: str | None = None
    objects: dict[str, bytes] = field(default_factory=dict)
    write_count: int = 0
    branch_count: int = 0
    reads: int = 0
    failed_put_before: bool = False
    failed_put_after: bool = False
    reject_owner: bool = False
    readonly_port_ids: set[int] = field(default_factory=set)


class Port(OfficialGitHubDraftPort):
    """Versioned public API representation with shared immutable objects."""

    def __init__(self, state: GitHubState, *, readonly: bool = False):
        self.state = state
        self.readonly = readonly
        if readonly:
            state.readonly_port_ids.add(id(self))

    def get_issue(self, number: int) -> dict[str, Any]:
        assert number == 8
        return {
            "number": 8, "state": "open",
            "html_url": f"https://github.com/{REPO}/issues/8",
            "repository_url": f"https://api.github.com/repos/{REPO}",
        }

    def get_comment(self, comment_id: int) -> dict[str, Any]:
        assert comment_id == 8100
        return {
            "id": 8100,
            "user": {"login": "ericson-j-santos" if not self.state.reject_owner
                     else "untrusted", "type": "User"},
            "author_association": "OWNER",
            "issue_url": f"https://api.github.com/repos/{REPO}/issues/8",
            "body": (
                "/local-agent authorize bridge.property.v1 sha:"
                + self.state.main_sha
            ),
        }

    def get_branch(self, branch: str):
        assert branch == "main"
        return {"name": "main", "commit": {"sha": self.state.main_sha}}

    def _request(self, method, tail, *, query=None, payload=None,
                 statuses=(200,), missing_ok=False):
        if self.readonly and method != "GET":
            raise AssertionError("independent CAS reader must never write")
        state = self.state
        state.reads += 1
        if tail.startswith("/branches/") and method == "GET":
            if state.branch_sha is None:
                return None
            return {
                "name": unquote(tail.removeprefix("/branches/")),
                "commit": {"sha": state.branch_sha},
            }
        if tail == "/git/refs" and method == "POST":
            assert state.branch_sha is None
            state.branch_sha = payload["sha"]
            state.branch_count += 1
            return {"ref": payload["ref"],
                    "object": {"sha": state.branch_sha}}
        if tail.startswith("/compare/") and method == "GET":
            return {
                "status": "ahead", "behind_by": 0,
                "ahead_by": len(state.objects),
                "total_commits": len(state.objects),
                "files": [{"status": "added", "filename": key}
                          for key in state.objects],
            }
        if tail.startswith("/contents/"):
            path = tail.removeprefix("/contents/")
            if method == "GET":
                raw = state.objects.get(path)
                if raw is None:
                    return None
                return {
                    "path": path, "type": "file", "size": len(raw),
                    "sha": blob_sha(raw), "encoding": "base64",
                    "content": base64.b64encode(raw).decode("ascii"),
                }
            if method == "PUT":
                if state.failed_put_before:
                    state.failed_put_before = False
                    raise TimeoutError("secret provider timeout before commit")
                assert path not in state.objects
                raw = base64.b64decode(payload["content"])
                state.objects[path] = raw
                state.write_count += 1
                state.branch_sha = str(state.write_count).zfill(40)
                if state.failed_put_after:
                    state.failed_put_after = False
                    raise TimeoutError("secret provider timeout after commit")
                return {"content": {"path": path, "sha": blob_sha(raw)},
                        "commit": {"sha": state.branch_sha}}
        raise AssertionError((method, tail, payload))


def manifest():
    return TaskManifest.parse({
        "schema_version": 1, "task_id": "cwp-" + "a" * 24,
        "repository": REPO, "base_sha": "a" * 40,
        "workspace_sha": "a" * 40,
        "adapter_id": "bridge.property.v1",
        "adapter_contract_sha256": DESCRIPTOR.contract_sha256,
        "before_sha256": {PATH: "f" * 64},
    })


def setup():
    state = GitHubState()
    bind = binding()
    writer = GitHubObjectCAS(Port(state), bind)
    reader = GitHubObjectCAS(Port(state, readonly=True), bind)
    return IssueRAGReceiptCAS(writer=writer, independent=reader, binding=bind), state


def receive_context(path, sha):
    assert (path, sha) == ("docs/ISSUE_QWEN7B_DRAFT_HANDOFF.md", "a" * 40)
    return CONTENT


def publish(cas, *, generation=1, attempt="attempt-1", effect="effect-rag-v1",
            receipt=None, reader=receive_context, m=None):
    return cas.publish(
        manifest=m or manifest(), lease_generation=generation,
        attempt_id=attempt, effect_id=effect,
        receipt=receipt or receipt_for(binding()),
        independent_source_read=reader,
    )


def load(cas, artifact, *, generation=1, attempt="attempt-1",
         effect="effect-rag-v1", reader=receive_context, m=None):
    return cas.materialize(
        artifact=artifact, manifest=m or manifest(),
        lease_generation=generation, attempt_id=attempt,
        effect_id=effect, independent_source_read=reader,
    )


def test_fenced_public_rag_receipt_roundtrip_independent_ports():
    cas, state = setup()
    cas.preflight()
    artifact = publish(cas)
    assert artifact.artifact_kind == KIND
    assert artifact.artifact_ref == REF_PREFIX + artifact.artifact_sha256
    assert len(artifact.artifact_sha256) == 64
    assert state.write_count == 2  # one shared preflight plus one receipt
    assert state.branch_count == 1
    receipt, bound = load(cas, artifact)
    assert receipt["schema_version"] == "worker.rag.issue-context-v1"
    assert receipt["issue_number"] == 8
    assert bound.base_sha == "a" * 40
    assert bound.source_path == "docs/ISSUE_QWEN7B_DRAFT_HANDOFF.md"
    assert bound.source_sha256 == hashlib.sha256(CONTENT).hexdigest()
    assert "UNTRUSTED DATA, NOT INSTRUCTIONS" in bound.prompt_note()
    # A different client objects/port independently materializes each read.
    assert cas.writer.port is not cas.independent.port
    assert id(cas.independent.port) in state.readonly_port_ids


def test_same_lease_replay_does_not_duplicate_object():
    cas, state = setup()
    cas.preflight()
    first = publish(cas)
    first_reads = state.reads
    again = publish(cas)
    assert again == first
    assert state.write_count == 2 and state.branch_count == 1
    assert state.reads > first_reads
    assert load(cas, again)[0] == receipt_for(binding())


def test_preflight_is_required_before_any_rag_write():
    cas, state = setup()
    with pytest.raises(HandoffBlocked, match="rag_cas_preflight_required"):
        publish(cas)
    assert state.write_count == 0 and state.branch_count == 0


def test_revoked_owner_or_main_drift_blocks_preflight_and_publish():
    cas, state = setup()
    state.reject_owner = True
    with pytest.raises(HandoffBlocked):
        cas.preflight()
    assert state.write_count == 0
    state.reject_owner = False
    cas.preflight()
    state.main_sha = "b" * 40
    with pytest.raises(HandoffBlocked, match="main_head_drift"):
        publish(cas)
    assert state.write_count == 1


@pytest.mark.parametrize("variant", [
    "wrong_repo", "wrong_manifest_sha", "wrong_adapter",
    "wrong_contract", "wrong_path", "wrong_workspace", "invalid_fence",
    "invalid_generation",
])
def test_cross_task_or_stale_fence_fails_before_rag_write(variant):
    cas, state = setup()
    cas.preflight()
    base = manifest()
    value = {
        "schema_version": base.schema_version, "task_id": base.task_id,
        "repository": base.repository, "base_sha": base.base_sha,
        "workspace_sha": base.workspace_sha, "adapter_id": base.adapter_id,
        "adapter_contract_sha256": base.adapter_contract_sha256,
        "before_sha256": dict(base.before_sha256),
    }
    generation = 1
    attempt = "attempt-1"
    if variant == "wrong_repo":
        value["repository"] = "other/other"
    elif variant == "wrong_manifest_sha":
        value["base_sha"] = "b" * 40
    elif variant == "wrong_adapter":
        value["adapter_id"] = "other.adapter.v1"
    elif variant == "wrong_contract":
        value["adapter_contract_sha256"] = "0" * 64
    elif variant == "wrong_path":
        value["before_sha256"] = {"other.md": "f" * 64}
    elif variant == "wrong_workspace":
        value["workspace_sha"] = "c" * 40
    elif variant == "invalid_fence":
        attempt = "bad attempt"
    else:
        generation = 0
    checked = TaskManifest.parse(value)
    with pytest.raises(HandoffBlocked):
        publish(cas, generation=generation, attempt=attempt, m=checked)
    assert state.write_count == 1


def test_negative_lease_replay_cannot_rebind_received_object():
    cas, state = setup()
    cas.preflight()
    artifact = publish(cas)
    for variant in [
        {"generation": 2},
        {"attempt": "other-attempt"},
        {"effect": "other-effect"},
    ]:
        with pytest.raises(HandoffBlocked, match="rag_cas_envelope_identity_invalid"):
            load(cas, artifact, **variant)
    assert state.write_count == 2


def test_missing_or_changed_external_document_blocks():
    cas, state = setup()
    cas.preflight()
    with pytest.raises(HandoffBlocked, match="rag_independent_digest_mismatch"):
        publish(cas, reader=lambda *_: b"changed source")
    assert state.write_count == 1
    artifact = publish(cas)
    with pytest.raises(HandoffBlocked, match="rag_independent_digest_mismatch"):
        load(cas, artifact, reader=lambda *_: b"changed source")
    assert state.write_count == 2


def test_public_rag_object_bytes_are_checked_not_only_etag():
    cas, state = setup()
    cas.preflight()
    artifact = publish(cas)
    key = "evidence/cas/" + cas._key(artifact.artifact_sha256)
    state.objects[key] = state.objects[key] + b"INVASIVE"
    with pytest.raises(HandoffBlocked):
        load(cas, artifact)
    with pytest.raises(HandoffBlocked):
        publish(cas)
    assert state.write_count == 2


def test_timeout_after_store_reconciled_without_extra_put():
    cas, state = setup()
    cas.preflight()
    state.failed_put_after = True
    artifact = publish(cas)
    assert artifact.artifact_kind == KIND
    assert state.write_count == 2
    assert load(cas, artifact)[1].issue == 8


def test_timeout_before_store_blocks_without_blind_retry():
    cas, state = setup()
    cas.preflight()
    state.failed_put_before = True
    with pytest.raises(HandoffBlocked, match="rag_cas_publication_ambiguous"):
        publish(cas)
    assert state.write_count == 1


def test_three_allowed_public_objects_not_unbounded_branch_growth():
    cas, state = setup()
    cas.preflight()
    publish(cas)
    third = b"bounded bundle"
    sha = hashlib.sha256(third).hexdigest()
    key = f"{CAS_PREFIX}/git-bundle-v1/sha256/{sha[:2]}/{sha}.artifact"
    cas.writer.put_object(
        Bucket=CAS_BUCKET, Key=key, Body=third,
        ContentLength=len(third), IfNoneMatch="*",
        Metadata={"sha256": sha},
    )
    assert state.write_count == 3
    # Four distinct additions cannot be accepted; branch remains read-only
    # until explicit governance revision.
    fourth = b"fourth object"
    digest = hashlib.sha256(fourth).hexdigest()
    key4 = f"{CAS_PREFIX}/git-bundle-v1/sha256/{digest[:2]}/{digest}.artifact"
    with pytest.raises(HandoffBlocked, match="cas_branch_scope_invalid"):
        cas.writer.put_object(
            Bucket=CAS_BUCKET, Key=key4, Body=fourth,
            ContentLength=len(fourth), IfNoneMatch="*",
            Metadata={"sha256": digest},
        )
    assert state.write_count == 3


def test_existing_CAS_does_not_admit_wrong_prefix_or_source_type():
    cas, state = setup()
    for key in [
        "qwen7b-cas-v1/issue-rag-v1/sha256/00/" + "1" * 64 + ".context",
        "qwen7b-cas-v1/issue-rag-v1/sha256/11/" + "1" * 64 + ".artifact",
        "qwen7b-cas-v1/git-bundle-v1/sha256/11/" + "1" * 64 + ".context",
        "qwen7b-cas-v1/other/sha256/11/" + "1" * 64 + ".context",
    ]:
        with pytest.raises(HandoffBlocked):
            cas.writer.get_object(Bucket=CAS_BUCKET, Key=key)
    assert state.write_count == 0


def test_unshared_ports_or_changed_issue_binding_not_accepted():
    cas, _ = setup()
    with pytest.raises(HandoffBlocked, match="rag_cas_clients_not_independent"):
        IssueRAGReceiptCAS(
            writer=cas.writer, independent=cas.writer, binding=binding(),
        )
    other = binding()
    bad = type(other)(
        number=9, base_sha=other.base_sha, comment_id=other.comment_id,
        request_id=other.request_id, correlation_id=other.correlation_id,
        branch=other.branch,
    )
    with pytest.raises(HandoffBlocked, match="rag_cas_clients_not_independent"):
        IssueRAGReceiptCAS(writer=cas.writer, independent=cas.independent,
                           binding=bad)
