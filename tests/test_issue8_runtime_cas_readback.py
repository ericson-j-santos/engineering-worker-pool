"""Read-only CAS handoff unit controls; no live GitHub/physical model in tests."""
from __future__ import annotations

import base64
from copy import deepcopy
import json
import subprocess
from pathlib import Path

import pytest

from scripts import issue8_runtime_cas_readback as h


def fake_api(raw: bytes) -> tuple[dict[str, dict], str]:
    path = h.CAS_PATH
    manifest = {
        f"/repos/{h.WORKER}/git/ref/heads/main": {"object": {"sha": h.BASE}},
        f"/repos/{h.WORKER}/issues/8": {"number": 8, "state": "open"},
        f"/repos/{h.WORKER}/issues/comments/{h.OWNER_COMMENT}": {
            "id": h.OWNER_COMMENT,
            "user": {"login": "ericson-j-santos"},
            "author_association": "OWNER",
            "issue_url": f"https://api.github.com/repos/{h.WORKER}/issues/8",
            "body": f"/local-agent authorize bridge.property.v1 sha:{h.BASE}",
        },
        f"/repos/{h.RUNTIME}/git/ref/heads/main": {"object": {"sha": "a10e3d4855b00e8933921571c4c868333a7b60ba"}},
        f"/repos/{h.RUNTIME}/compare/{h.RUNTIME_BASE}...a10e3d4855b00e8933921571c4c868333a7b60ba": {
            "status": "ahead", "ahead_by": 1, "behind_by": 0,
            "merge_base_commit": {"sha": h.RUNTIME_BASE},
        },
        f"/repos/{h.RUNTIME}/git/ref/heads/{h.CAS_BRANCH}": {"object": {"sha": h.CAS_HEAD}},
        f"/repos/{h.RUNTIME}/compare/{h.RUNTIME_BASE}...{h.CAS_HEAD}": {
            "status": "ahead", "ahead_by": 1, "behind_by": 0,
            "total_commits": 1,
            "files": [{"filename": path, "status": "added"}],
        },
        f"/repos/{h.RUNTIME}/actions/runs/{h.RUN_ID}": {
            "id": h.RUN_ID, "head_sha": h.RUNTIME_HEAD,
            "status": "completed", "conclusion": "success",
        },
        f"/repos/{h.RUNTIME}/actions/runs/{h.RUN_ID}/jobs": {"jobs": [
            {"name": name, "status": "completed", "conclusion": "success",
             "runner_id": 2, "runner_name": "DESKTOP-PDQK954-runtime"}
            for name in (
                "Owner issue and SHA admission (no writes)",
                "Dell Qwen7B issue8 fenced Builder and independent checkout",
                "Publish one real model Git bundle in runtime CAS",
                "Independent hosted Validator reads durable CAS and executes real tests",
            )
        ]},
        f"/repos/{h.RUNTIME}/contents/{path}?ref={h.CAS_BRANCH}": {
            "type": "file", "path": path, "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii"),
            "size": len(raw), "sha": h.git_blob(raw),
        },
    }
    return manifest, f"/repos/{h.RUNTIME}/contents/{path}?ref={h.CAS_BRANCH}"


def fixture(monkeypatch):
    raw = b"public-git-bundle-example-not-real\n" * 17
    sha = h.digest(raw)
    monkeypatch.setattr(h, "CAS_SHA256", sha)
    monkeypatch.setattr(h, "CAS_PATH", f"evidence/issue8/sha256/{sha[:2]}/{sha}.bundle")
    return fake_api(raw), raw


def test_cross_repository_metadata_and_bytes_are_verified_without_any_write(monkeypatch):
    (records, key), expected = fixture(monkeypatch)
    calls = []

    def get(path):
        calls.append(path)
        return deepcopy(records[path])

    assert h.remote_readback(get) == expected
    assert len(calls) == len(records) == 10
    assert all(path.startswith("/repos/") for path in calls)
    assert h.remote_readback(get) == expected  # replay has no mutation/model call


@pytest.mark.parametrize("fault", [
    "owner", "main", "runtime_main", "cas_ref", "cas_compare", "run_status",
    "run_sha", "validator_skipped", "builder_wrong_runner", "bundle_bytes",
    "file_path", "file_sha", "issue_closed", "history_rewritten",
])
def test_invalid_external_evidence_fails_closed(monkeypatch, fault):
    (records, key), raw = fixture(monkeypatch)
    routes = list(records)
    if fault == "owner":
        records[routes[2]]["body"] = "/local-agent authorize bridge.property.v1 sha:" + "0" * 40
    elif fault == "main":
        records[routes[0]]["object"]["sha"] = "0" * 40
    elif fault == "runtime_main":
        records[routes[3]]["object"]["sha"] = "0" * 40
    elif fault == "cas_ref":
        records[routes[5]]["object"]["sha"] = "0" * 40
    elif fault == "cas_compare":
        records[routes[6]]["files"].append({"filename": "unrelated.txt", "status": "added"})
    elif fault == "history_rewritten":
        records[routes[4]]["merge_base_commit"]["sha"] = "0" * 40
    elif fault == "run_status":
        records[routes[7]]["conclusion"] = "failure"
    elif fault == "run_sha":
        records[routes[7]]["head_sha"] = "0" * 40
    elif fault == "validator_skipped":
        records[routes[8]]["jobs"][3]["conclusion"] = "skipped"
    elif fault == "builder_wrong_runner":
        records[routes[8]]["jobs"][1]["runner_name"] = "other-host"
    elif fault == "bundle_bytes":
        records[key]["content"] = base64.b64encode(raw + b"X").decode("ascii")
        records[key]["size"] += 1
        records[key]["sha"] = h.git_blob(raw + b"X")
    elif fault == "file_path":
        records[key]["path"] = "elsewhere.bundle"
    elif fault == "file_sha":
        records[key]["sha"] = "0" * 40
    elif fault == "issue_closed":
        records[routes[1]]["state"] = "closed"
    def guarded_get(path):
        if path not in records:
            raise h.HandoffBlocked("issue8_test_lineage_unavailable")
        return deepcopy(records[path])
    with pytest.raises(h.HandoffBlocked):
        h.remote_readback(guarded_get)


def test_official_transport_rejects_arbitrary_uris_and_mutating_methods():
    transport = h.ReadOnlyGitHub("")
    try:
        with pytest.raises(h.HandoffBlocked, match="issue8_cas_route_invalid"):
            transport("/repos/ericson-j-santos/engineering-worker-pool/git/refs")
        with pytest.raises(h.HandoffBlocked, match="issue8_cas_route_invalid"):
            transport("/repos/ericson-j-santos/engineering-worker-pool/issues/8?extra=1")
        with pytest.raises(h.HandoffBlocked, match="issue8_cas_route_invalid"):
            transport(f"/repos/{h.RUNTIME}/compare/{h.RUNTIME_BASE}...not-a-sha")
    finally:
        transport.close()


def test_git_bundle_can_be_verified_in_an_independent_read_only_checkout(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    (original / "file.txt").write_text("before\n")

    def cmd(directory, *parts):
        p = subprocess.run(["git", *parts], cwd=directory,
            capture_output=True, text=True, check=True)
        return p.stdout.strip()

    cmd(original, "init", "-q")
    cmd(original, "config", "user.name", "Fixture")
    cmd(original, "config", "user.email", "fixture@example.invalid")
    cmd(original, "add", "file.txt")
    cmd(original, "commit", "-qm", "baseline")
    base = cmd(original, "rev-parse", "HEAD")
    generated = tmp_path / "builder"
    cmd(tmp_path, "clone", "-q", "--no-hardlinks", str(original), str(generated))
    (generated / "file.txt").write_text("after\n")
    cmd(generated, "add", "file.txt")
    cmd(generated, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "repaired")
    produced = cmd(generated, "rev-parse", "HEAD")
    artifact = tmp_path / "repair.bundle"
    cmd(generated, "bundle", "create", str(artifact), "HEAD", "^" + base)
    validator = tmp_path / "validator"
    h.safe_git(tmp_path, "clone", "--no-hardlinks", str(original), str(validator))
    h.safe_git(validator, "remote", "remove", "origin")
    h.safe_git(validator, "bundle", "verify", str(artifact))
    h.safe_git(validator, "fetch", "--no-tags", str(artifact), "HEAD")
    h.safe_git(validator, "checkout", "--detach", produced)
    assert h.safe_git(validator, "rev-list", "--parents", "-n", "1", produced) == produced + " " + base
    assert (validator / "file.txt").read_text() == "after\n"
    assert (original / "file.txt").read_text() == "before\n"
