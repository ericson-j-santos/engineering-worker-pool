"""Official GitHub draft port: HTTP contract tests without network/credentials."""
from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

from app.github_draft_port import OfficialGitHubDraftPort, PREFIX, API
from app.issue_draft_pr import HandoffBlocked, PATH, REPO

BRANCH = "agent/qwen7b-issue-8-" + "a" * 12
BASE = "a" * 40
HEAD = "c" * 40
TOKEN = "synthetic-token-not-secret-1234"
DATA = b"def _property(properties, name):\n    return ''\n"


def blob_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\x00" + data).hexdigest()


def response(status, obj):
    return httpx.Response(status, headers={"content-type": "application/json"},
                          json=obj)


def port_for(monkeypatch, handler):
    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False,
                          follow_redirects=False)
    port = OfficialGitHubDraftPort(token_file=__import__("pathlib").Path("/unused"), client=client)
    monkeypatch.setattr(port, "_token", lambda: TOKEN)
    return port


def test_real_http_contract_reads_verified_source_and_scope(monkeypatch):
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.url.params.get("ref")))
        assert request.url.host == "api.github.com"
        assert request.headers["authorization"] == "Bearer " + TOKEN
        if request.url.path.endswith("/branches/main"):
            return response(200, {"name": "main", "commit": {"sha": BASE}})
        if request.url.path.endswith("/contents/" + PATH):
            assert request.url.params["ref"] == BASE
            return response(200, {"type": "file", "path": PATH, "encoding": "base64",
                                  "content": base64.b64encode(DATA).decode(),
                                  "size": len(DATA), "sha": blob_sha(DATA)})
        if "/compare/" in request.url.path:
            return response(200, {"status": "ahead", "ahead_by": 1, "behind_by": 0,
                                  "total_commits": 1,
                                  "files": [{"status": "modified", "filename": PATH}]})
        raise AssertionError("unexpected endpoint")

    port = port_for(monkeypatch, handler)
    assert port.get_branch("main")["commit"]["sha"] == BASE
    assert port.get_source(BASE) == (DATA, blob_sha(DATA))
    assert port.changed_paths(BASE, HEAD) == (PATH,)
    assert len(seen) == 3


def test_git_api_read_and_write_shapes(monkeypatch):
    calls = []
    def handler(req):
        calls.append((req.method, req.url.path, req.url.params))
        assert req.url.host == "api.github.com"
        if req.method == "GET" and req.url.path.endswith("/issues/8"):
            return response(200, {"number": 8, "state": "open"})
        if req.method == "GET" and req.url.path.endswith("/issues/comments/7"):
            return response(200, {"id": 7, "body": "approved"})
        if req.method == "GET" and req.url.path.endswith("/branches/" + BRANCH):
            return response(404, {"message": "not found"})
        if req.method == "POST" and req.url.path.endswith("/git/refs"):
            payload = json.loads(req.content)
            assert payload == {"ref": "refs/heads/" + BRANCH, "sha": BASE}
            return response(201, {"ref": "refs/heads/" + BRANCH,
                                  "object": {"sha": BASE}})
        if req.method == "PUT" and req.url.path.endswith("/contents/" + PATH):
            payload = json.loads(req.content)
            assert payload["branch"] == BRANCH and payload["sha"] == BASE
            assert base64.b64decode(payload["content"]) == DATA
            return response(200, {"content": {"path": PATH, "sha": blob_sha(DATA)},
                                  "commit": {"sha": HEAD}})
        if req.method == "GET" and req.url.path.endswith("/pulls"):
            assert req.url.params["state"] == "all"
            return response(200, [])
        if req.method == "POST" and req.url.path.endswith("/pulls"):
            payload = json.loads(req.content)
            assert payload["draft"] is True and payload["base"] == "main"
            return response(201, {"state": "open", "draft": True})
        raise AssertionError("unexpected request " + str(req.url))

    api = port_for(monkeypatch, handler)
    assert api.get_issue(8)["number"] == 8
    assert api.get_comment(7)["id"] == 7
    assert api.get_branch(BRANCH) is None
    api.create_branch(BRANCH, BASE)
    api.replace_source(BRANCH, DATA, BASE)
    assert api.find_pr(BRANCH) is None
    api.create_pr(BRANCH, "fix: issue #8 via local Qwen7B", "reviewed\n")
    assert [method for method, *_ in calls] == [
        "GET", "GET", "GET", "POST", "PUT", "GET", "POST",
    ]


@pytest.mark.parametrize("status,payload,code", [
    (302, {"location": "https://evil.invalid"}, "github_status_unverified"),
    (401, {"message": "bad credentials"}, "github_status_unverified"),
    (403, {"message": "rate limit"}, "github_status_unverified"),
    (200, {"name": "wrong", "commit": {"sha": BASE}}, "github_branch_shape_invalid"),
])
def test_github_response_requires_exact_identity(monkeypatch, status, payload, code):
    api = port_for(monkeypatch, lambda _: response(status, payload))
    with pytest.raises(HandoffBlocked, match="^" + code + "$"):
        api.get_branch("main")


def test_source_blob_digest_verification(monkeypatch):
    forged = {"type": "file", "path": PATH, "encoding": "base64",
              "content": base64.b64encode(DATA).decode(), "size": len(DATA),
              "sha": "a" * 40}
    api = port_for(monkeypatch, lambda _: response(200, forged))
    with pytest.raises(HandoffBlocked, match="github_source_blob_unverified"):
        api.get_source(BASE)


def test_branch_scope_rejects_different_file(monkeypatch):
    api = port_for(monkeypatch, lambda _: response(200, {
        "status": "ahead", "ahead_by": 1, "behind_by": 0,
        "total_commits": 1,
        "files": [
            {"status": "modified", "filename": PATH},
            {"status": "modified", "filename": ".github/workflows/ci.yml"},
        ],
    }))
    with pytest.raises(HandoffBlocked, match="github_compare_unverified"):
        api.changed_paths(BASE, HEAD)


def test_branch_scope_rejects_wrong_ref_before_http(monkeypatch):
    api = port_for(monkeypatch, lambda _: pytest.fail("HTTP sent"))
    with pytest.raises(HandoffBlocked, match="github_branch_ref_invalid"):
        api.get_branch("refs/heads/main")
    with pytest.raises(HandoffBlocked, match="github_branch_ref_invalid"):
        api.get_source("../secrets")


def test_http_failure_is_uncertain_and_not_retried(monkeypatch):
    count = 0
    def handler(req):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("unverified")
    api = port_for(monkeypatch, handler)
    with pytest.raises(HandoffBlocked, match="github_result_uncertain"):
        api.create_branch(BRANCH, BASE)
    assert count == 1


def test_pr_identity_and_draft_from_github(monkeypatch):
    pr = {
        "number": 29, "state": "open", "draft": True, "body": "evidence",
        "head": {"ref": BRANCH, "sha": HEAD, "repo": {"full_name": REPO}},
        "base": {"ref": "main", "repo": {"full_name": REPO}},
    }
    api = port_for(monkeypatch, lambda _: response(200, [pr]))
    actual = api.find_pr(BRANCH)
    assert actual["draft"] is True and actual["head_sha"] == HEAD
    bad = dict(pr, head=dict(pr["head"], repo={"full_name": "other/owner"}))
    api = port_for(monkeypatch, lambda _: response(200, [bad]))
    with pytest.raises(HandoffBlocked, match="github_pr_identity_invalid"):
        api.find_pr(BRANCH)
