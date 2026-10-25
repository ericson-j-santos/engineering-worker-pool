"""Official GitHub CAS policy and real Git bundle tests; GitHub API is mocked."""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from urllib.parse import unquote
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.github_cas_transport import (
    AlreadyExists, CAS_BUCKET, CAS_PREFIX, GitHubObjectCAS,
    governed_bundle_transport,
)
from app.github_draft_port import OfficialGitHubDraftPort
from app.issue_draft_pr import HandoffBlocked, REPO, admit
from app.native_one_shot import (
    AdapterResult, LocalWorkspace, NativeExecutorBlocked, TaskManifest,
)
from app.s3_git_bundle_artifact import S3GitBundleArtifactTransport
from app.structured_code_worker import render, sha256, validate
from scripts.structured_acceptance import INSTRUCTION, cases
from test_structured_code_worker import FIXED

BASE = "a" * 40


def git_blob(data: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(data)).encode() + b"\x00" + data,
    ).hexdigest()


class OfficialPortFixture(OfficialGitHubDraftPort):
    """Stateful API fixture; never evidence of live external GitHub writes."""

    def __init__(self, base=BASE):
        self.main = base
        self.head = None
        self.files = {}
        self.put_count = 0
        self.branch_count = 0
        self.comment_body = (
            f"/local-agent authorize bridge.property.v1 sha:{base}"
        )

    def get_branch(self, ref):
        assert ref == "main"
        return {"name": "main", "commit": {"sha": self.main}}

    def get_issue(self, number):
        return {
            "number": number, "state": "open",
            "html_url": f"https://github.com/{REPO}/issues/{number}",
            "repository_url": f"https://api.github.com/repos/{REPO}",
        }

    def get_comment(self, comment_id):
        return {
            "id": comment_id,
            "user": {"login": "ericson-j-santos", "type": "User"},
            "author_association": "OWNER",
            "issue_url": f"https://api.github.com/repos/{REPO}/issues/8",
            "body": self.comment_body,
        }

    def _request(self, method, tail, *, query=None, payload=None,
                 statuses=(200,), missing_ok=False):
        if method == "GET" and tail.startswith("/branches/"):
            return None if self.head is None else {
                "name": unquote(tail.removeprefix("/branches/")),
                "commit": {"sha": self.head},
            }
        if method == "POST" and tail == "/git/refs":
            assert self.head is None
            self.head = payload["sha"]
            self.branch_count += 1
            return {"ref": payload["ref"], "object": {"sha": self.head}}
        if method == "GET" and tail.startswith("/compare/"):
            return {
                "status": "ahead", "ahead_by": len(self.files),
                "behind_by": 0, "total_commits": len(self.files),
                "files": [
                    {"status": "added", "filename": path}
                    for path in self.files
                ],
            }
        if tail.startswith("/contents/"):
            path = tail.removeprefix("/contents/")
            if method == "GET":
                if path not in self.files:
                    return None
                data = self.files[path]
                return {
                    "type": "file", "path": path, "encoding": "base64",
                    "content": base64.b64encode(data).decode(),
                    "sha": git_blob(data), "size": len(data),
                }
            if method == "PUT":
                assert path not in self.files
                data = base64.b64decode(payload["content"])
                self.files[path] = data
                self.put_count += 1
                self.head = str(self.put_count).zfill(40)
                return {
                    "content": {"path": path, "sha": git_blob(data)},
                    "commit": {"sha": self.head},
                }
        raise AssertionError((method, tail, payload))


def fixture(base=BASE):
    api = OfficialPortFixture(base)
    now = datetime.now(timezone.utc)
    binding = admit(
        api.get_issue(8), api.get_comment(771),
        api.get_branch("main"), base_sha=base,
        observed_at=now.isoformat(), now=now,
    )
    return api, binding


def key_for(data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    return f"{CAS_PREFIX}/git-bundle-v1/sha256/{digest[:2]}/{digest}.artifact"


def put(client, body):
    digest = hashlib.sha256(body).hexdigest()
    return client.put_object(
        Bucket=CAS_BUCKET, Key=key_for(body),
        Body=body, ContentLength=len(body),
        Metadata={"sha256": digest}, IfNoneMatch="*",
    )


def test_github_cas_readback_and_replay_no_duplicate():
    api, binding = fixture()
    transport = governed_bundle_transport(api, binding)
    assert isinstance(transport, S3GitBundleArtifactTransport)
    transport.preflight()
    transport.preflight()
    body = b"artifact-envelope"
    put(transport.client, body)
    assert transport.client.get_object(
        Bucket=CAS_BUCKET, Key=key_for(body),
    )["Body"].read() == body
    with pytest.raises(AlreadyExists):
        put(transport.client, body)
    transport._put_cas(key_for(body), body, hashlib.sha256(body).hexdigest())
    assert api.branch_count == 1 and api.put_count == 2
    assert len(api.files) == 2


def test_issue_revoked_or_main_drift_block_before_write():
    for kind in ("revoked", "main"):
        api, binding = fixture()
        if kind == "revoked":
            api.comment_body = "/local-agent revoke"
        else:
            api.main = "b" * 40
        with pytest.raises(HandoffBlocked):
            put(GitHubObjectCAS(api, binding), b"content")
        assert api.put_count == api.branch_count == 0


def test_tampering_rejected_on_independent_readback():
    api, binding = fixture()
    client = GitHubObjectCAS(api, binding)
    put(client, b"trusted")
    api.files["evidence/cas/" + key_for(b"trusted")] = b"tampered"
    with pytest.raises(HandoffBlocked, match="cas_object_missing_or_modified"):
        client.get_object(Bucket=CAS_BUCKET, Key=key_for(b"trusted"))


@pytest.mark.parametrize("bad", [
    "../secret",
    "qwen7b-cas-v1/preflight-v1/sha256/ff/" + "f" * 64 + ".artifact",
    "qwen7b-cas-v1/git-bundle-v1/sha256/00/" + "f" * 64 + ".artifact",
])
def test_invalid_key_never_accesses_github(bad):
    api, binding = fixture()
    with pytest.raises(HandoffBlocked, match="cas_key_invalid"):
        GitHubObjectCAS(api, binding).get_object(
            Bucket=CAS_BUCKET, Key=bad,
        )
    assert api.put_count == api.branch_count == 0


def test_wrong_bucket_or_digest_fails_without_side_effect():
    api, binding = fixture()
    client = GitHubObjectCAS(api, binding)
    with pytest.raises(HandoffBlocked, match="cas_destination_invalid"):
        client.get_object(Bucket="wrong", Key=key_for(b"x"))
    with pytest.raises(HandoffBlocked, match="cas_write_invalid"):
        client.put_object(
            Bucket=CAS_BUCKET, Key=key_for(b"expected"),
            Body=b"tampered", ContentLength=8,
            Metadata={"sha256": hashlib.sha256(b"expected").hexdigest()},
            IfNoneMatch="*",
        )
    assert api.put_count == 0


def test_foreign_branch_files_block_new_publication():
    api, binding = fixture()
    client = GitHubObjectCAS(api, binding)
    put(client, b"one")
    api.files["docs/unrelated.txt"] = b"foreign"
    with pytest.raises(HandoffBlocked, match="cas_branch_scope_invalid"):
        put(client, b"two")
    assert api.put_count == 1


def git(root: Path, *args: str) -> str:
    outcome = subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        text=True, check=False,
    )
    assert outcome.returncode == 0, (args, outcome.stderr)
    return outcome.stdout.strip()


def test_real_git_bundle_roundtrip_across_checkouts(tmp_path):
    """Real Git objects + two checkouts. Model/GitHub provider are fixtures."""
    builder_root = tmp_path / "builder"
    validator_root = tmp_path / "validator"
    (builder_root / "app").mkdir(parents=True)
    original = (
        Path(__file__).resolve().parents[1] / "app" / "portfolio_bridge.py"
    ).read_bytes()
    (builder_root / "app" / "portfolio_bridge.py").write_bytes(original)
    git(builder_root, "init", "-q")
    git(builder_root, "config", "core.autocrlf", "false")
    git(builder_root, "config", "user.name", "CASFixture")
    git(builder_root, "config", "user.email", "fixture@example.invalid")
    git(builder_root, "add", "app/portfolio_bridge.py")
    git(builder_root, "commit", "-qm", "original real source")
    base = git(builder_root, "rev-parse", "HEAD")
    git(tmp_path, "clone", "--no-hardlinks", "-q",
        str(builder_root), str(validator_root))
    git(validator_root, "remote", "remove", "origin")
    git(validator_root, "checkout", "-q", "--detach", base)
    api, binding = fixture(base)
    transport = governed_bundle_transport(api, binding)
    task = {
        "task_id": "fixture", "path": "app/portfolio_bridge.py",
        "function": "_property", "parameters": ["properties", "name"],
        "instruction": INSTRUCTION, "base_sha": base,
        "before_sha256": sha256(original), "cases": cases(),
    }
    candidate = render(
        original.decode("utf-8"), task,
        json.dumps({"function": FIXED}),
    ).encode("utf-8")
    assert validate(candidate.decode("utf-8"), task)["passed"] == 42
    (builder_root / "app" / "portfolio_bridge.py").write_bytes(candidate)
    git(builder_root, "add", "app/portfolio_bridge.py")
    git(builder_root, "commit", "-qm", "fixture repair")
    produced = git(builder_root, "rev-parse", "HEAD")
    manifest = TaskManifest.parse({
        "schema_version": 1,
        "task_id": "cwp-" + "8" * 24,
        "repository": REPO,
        "base_sha": base, "workspace_sha": base,
        "adapter_id": "bridge.property.v1",
        "adapter_contract_sha256": "c" * 64,
        "before_sha256": {
            "app/portfolio_bridge.py": sha256(original),
        },
    })
    result = AdapterResult(
        produced, ("app/portfolio_bridge.py",),
        ("build",), sha256(candidate),
    )
    builder = LocalWorkspace(builder_root)
    validator = LocalWorkspace(validator_root)
    transport.preflight()
    artifact = transport.publish(
        task_id=manifest.task_id, manifest=manifest,
        lease_generation=7, attempt_id="attempt-7",
        effect_id="effect-7", workspace=builder, result=result,
    )
    transport.verify(
        task_id=manifest.task_id, manifest=manifest,
        workspace=builder, produced_sha=produced, artifact=artifact,
    )
    assert transport.materialize(
        task_id=manifest.task_id, manifest=manifest,
        workspace=validator, artifact=artifact,
    ) == result
    assert validator.head() == produced
    assert (
        validator_root / "app" / "portfolio_bridge.py"
    ).read_bytes() == candidate
    again = transport.publish(
        task_id=manifest.task_id, manifest=manifest,
        lease_generation=7, attempt_id="attempt-7",
        effect_id="effect-7", workspace=builder, result=result,
    )
    assert again == artifact and api.put_count == 2
    path = next(p for p in api.files if p.endswith(".artifact"))
    api.files[path] = b"tampered"
    with pytest.raises(NativeExecutorBlocked):
        transport.verify(
            task_id=manifest.task_id, manifest=manifest,
            workspace=builder, produced_sha=produced, artifact=artifact,
        )


def test_manifest_from_other_repo_is_rejected_before_publication():
    api, binding = fixture()
    transport = governed_bundle_transport(api, binding)
    manifest = TaskManifest.parse({
        "schema_version": 1, "task_id": "cwp-" + "9" * 24,
        "repository": "foreign/repository",
        "base_sha": BASE, "workspace_sha": BASE,
        "adapter_id": "fixture", "adapter_contract_sha256": "c" * 64,
        "before_sha256": {"app/portfolio_bridge.py": "d" * 64},
    })
    with pytest.raises(HandoffBlocked, match="cas_manifest_binding_invalid"):
        transport._bound(manifest)
    assert api.put_count == 0
