"""Synthetic HTTP contract tests; no operational GitHub token or READY receipt."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.native_one_shot import NativeExecutorBlocked
from app.prepr_github_readback import (
    BRANCH, ISSUE, REPOSITORY, OfficialNoteriReadback,
)

BASE = "e10b3a00a9b3ec803ed4a4cc8f4f8c62b06c8928"
SOURCE = "1e60b4f3db1d5ad5d59dbed087f275870aa002ad"
TOKEN = "synthetic-example-not-a-token-567890"
PATHS = sorted([
    ".github/workflows/noteri-control-plane-probe.yml",
    "scripts/noteri_control_plane_watchdog_uac_launcher.py",
    "scripts/noteri_qualification_receipt.py",
    "scripts/noteri_watchdog_readonly_inventory.py",
    "tests/test_noteri_qualification_confirmation.py",
    "tests/test_noteri_qualification_receipt.py",
    "tests/test_noteri_watchdog_readonly_inventory.py",
])


def ok(body, status=200):
    return httpx.Response(status, headers={"content-type": "application/json"},
                          json=body)


def reader_for(monkeypatch, *, mutate=None):
    seen = []
    branch_reads = 0

    def handler(request):
        nonlocal branch_reads
        seen.append((request.method, request.url.path))
        assert request.method == "GET"
        assert request.url.host == "api.github.com"
        assert request.headers["authorization"] == "Bearer " + TOKEN
        path = request.url.path
        if path.endswith("/issues/42"):
            body = {
                "number": ISSUE, "state": "open",
                "html_url": "https://github.com/" + REPOSITORY + "/issues/42",
                "repository_url": "https://api.github.com/repos/" + REPOSITORY,
            }
            key = "issue"
        elif path.endswith("/branches/main"):
            body = {"name": "main", "commit": {"sha": BASE}}
            key = "main"
        elif path.endswith("/branches/" + BRANCH):
            branch_reads += 1
            body = {"name": BRANCH, "commit": {"sha": SOURCE}}
            key = "second_branch" if branch_reads == 2 else "branch"
        elif "/compare/" in path:
            body = {
                "status": "ahead", "ahead_by": 6, "behind_by": 0,
                "total_commits": 6, "base_commit": {"sha": BASE},
                "merge_base_commit": {"sha": BASE},
                "files": [{"filename": p, "status": "modified"} for p in PATHS],
            }
            key = "compare"
        else:
            raise AssertionError("Unexpected route: " + path)
        if mutate and key in mutate:
            patch = mutate[key]
            if patch is None:
                return ok({"message": "no access"}, status=403)
            body.update(patch)
        return ok(body)

    client = httpx.Client(transport=httpx.MockTransport(handler),
                          trust_env=False, follow_redirects=False)
    reader = OfficialNoteriReadback(token_file=Path("/nonexistent"),
                                   base_sha=BASE, source_sha=SOURCE,
                                   client=client)
    monkeypatch.setattr(reader, "_token", lambda: TOKEN)
    return reader, seen


def test_authentic_get_only_source_readback_and_replay(monkeypatch):
    reader, seen = reader_for(monkeypatch)
    first = reader(REPOSITORY, ISSUE)
    assert first == {
        "repository": REPOSITORY, "issue_number": ISSUE,
        "issue_state": "open", "main_sha": BASE,
        "branch_head_sha": SOURCE, "base_is_ancestor": True,
        "changed_paths": PATHS,
    }
    assert reader(REPOSITORY, ISSUE) == first
    assert len(seen) == 10
    assert all(method == "GET" for method, _ in seen)


def test_unallowlisted_repo_and_issue_never_read_remote(monkeypatch):
    reader, seen = reader_for(monkeypatch)
    for repo, number in [(REPOSITORY, 43),
                         ("ericson-j-santos/engineering-worker-pool", ISSUE)]:
        with pytest.raises(NativeExecutorBlocked,
                           match="prepr_github_target_not_allowlisted"):
            reader(repo, number)
    assert not seen


@pytest.mark.parametrize("mutation,code", [
    ({"issue": {"state": "closed"}}, "prepr_github_issue_invalid"),
    ({"issue": {"pull_request": {}}}, "prepr_github_issue_invalid"),
    ({"main": {"commit": {"sha": SOURCE}}}, "prepr_github_head_drift"),
    ({"branch": {"commit": {"sha": BASE}}}, "prepr_github_head_drift"),
    ({"compare": {"behind_by": 1}}, "prepr_github_ancestry_unproved"),
    ({"compare": {"merge_base_commit": {"sha": SOURCE}}},
     "prepr_github_ancestry_unproved"),
    ({"compare": {"status": "diverged"}},
     "prepr_github_ancestry_unproved"),
    ({"compare": {"files": []}}, "prepr_github_diff_invalid"),
    ({"compare": {"files": [{"filename": "a", "status": "renamed"}]}},
     "prepr_github_diff_invalid"),
    ({"second_branch": {"commit": {"sha": BASE}}},
     "prepr_github_head_drift"),
    ({"compare": None}, "prepr_github_response_unverified"),
])
def test_negative_http_evidence_fails_closed(monkeypatch, mutation, code):
    reader, _ = reader_for(monkeypatch, mutate=mutation)
    with pytest.raises(NativeExecutorBlocked, match=code):
        reader(REPOSITORY, ISSUE)


def test_missing_private_token_file_never_sends_http():
    called = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: called.append(r) or ok({})),
        trust_env=False, follow_redirects=False,
    )
    with OfficialNoteriReadback(token_file=Path("/nonexistent"),
                                base_sha=BASE, source_sha=SOURCE,
                                client=client) as reader:
        with pytest.raises(NativeExecutorBlocked,
                           match="prepr_github_token_unavailable"):
            reader(REPOSITORY, ISSUE)
    assert not called


@pytest.mark.parametrize("base,head", [
    ("invalid", SOURCE), (BASE, BASE), (BASE, "0" * 39),
])
def test_bad_sha_configuration_blocks_before_network(base, head):
    with pytest.raises(NativeExecutorBlocked,
                       match="prepr_github_sha_config_invalid"):
        OfficialNoteriReadback(token_file=Path("/nonexistent"),
                              base_sha=base, source_sha=head)
