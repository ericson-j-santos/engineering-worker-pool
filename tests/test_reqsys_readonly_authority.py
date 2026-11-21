"""Adapter contract tests: simulated HTTP, NOT real authenticated service E2E."""
from __future__ import annotations

import json

import httpx
import pytest

from app.pre_pr_cas_gate import PrePRBlocked
from app.reqsys_readonly_authority import REPOSITORY, ReqSysReadOnlyAuthority

SHA = "a" * 40


def port(handler=None, token="x" * 24):
    def default(request):
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer " + token
        path = request.url.path
        if path.endswith("/issues/715"):
            obj = {"number": 715, "state": "open",
                   "html_url": f"https://github.com/{REPOSITORY}/issues/715"}
        else:
            obj = {"name": "main", "commit": {"sha": SHA}}
        return httpx.Response(200, json=obj)
    return ReqSysReadOnlyAuthority(
        client=httpx.Client(transport=httpx.MockTransport(handler or default), trust_env=False),
        token_reader=lambda: token)


def test_read_issue_and_main_sha():
    r = port()
    assert r.issue(REPOSITORY, 715) == {"repository": REPOSITORY,
        "number": 715, "state": "open", "is_pull_request": False}
    assert r.branch_sha(REPOSITORY, "main") == SHA


@pytest.mark.parametrize("repo,number", [
    ("ericson-j-santos/engineering-worker-pool", 715),
    (REPOSITORY, True), (REPOSITORY, 0),
])
def test_issue_scope_denied(repo, number):
    with pytest.raises(PrePRBlocked, match="reqsys_readonly_issue_scope_invalid"):
        port().issue(repo, number)


@pytest.mark.parametrize("ref", [
    "../main", "refs/heads/main", "//abc", ".github/test", "main.lock", "a?b"
])
def test_ref_scope_denied(ref):
    with pytest.raises(PrePRBlocked, match="reqsys_readonly_branch_scope_invalid"):
        port().branch_sha(REPOSITORY, ref)


def test_pr_cannot_impersonate_issue():
    def handler(req):
        return httpx.Response(200, json={"number": 715, "state": "open",
            "html_url": f"https://github.com/{REPOSITORY}/issues/715", "pull_request": {}})
    with pytest.raises(PrePRBlocked, match="reqsys_readonly_issue_unverified"):
        port(handler).issue(REPOSITORY, 715)


def test_foreign_url_cannot_impersonate_issue():
    def handler(req):
        return httpx.Response(200, json={"number": 715, "state": "open",
            "html_url": "https://github.com/other/repo/issues/715"})
    with pytest.raises(PrePRBlocked, match="reqsys_readonly_issue_unverified"):
        port(handler).issue(REPOSITORY, 715)


@pytest.mark.parametrize("obj", [
    {"name": "main", "commit": {"sha": "bad"}},
    {"name": "other", "commit": {"sha": SHA}},
    {"name": "main", "commit": []},
])
def test_invalid_branch_response_fails(obj):
    with pytest.raises(PrePRBlocked, match="reqsys_readonly_sha_unverified"):
        port(lambda req: httpx.Response(200, json=obj)).branch_sha(REPOSITORY, "main")


@pytest.mark.parametrize("status,headers,body", [
    (404, {"Content-Type": "application/json"}, b"{}"),
    (302, {"Location": "https://evil.example"}, b""),
    (200, {"Content-Type": "text/html"}, b"<html>bad</html>"),
    (200, {"Content-Type": "application/json"}, b"[1,2,3]"),
    (200, {"Content-Type": "application/json"}, b"{"),
])
def test_untrusted_http_rejected(status, headers, body):
    handler = lambda req: httpx.Response(status, headers=headers, content=body)
    with pytest.raises(PrePRBlocked, match="reqsys_readonly_"):
        port(handler).issue(REPOSITORY, 715)


def test_missing_token_fails_without_network():
    def handler(req):
        pytest.fail("must not access network without token")
    with pytest.raises(PrePRBlocked, match="reqsys_readonly_credential_unavailable"):
        port(handler, token="bad").issue(REPOSITORY, 715)


def test_untrusted_provider_sources_fail_closed():
    r = port()
    for fn in (lambda: r.todo("TODO-715"),
               lambda: r.authorization("reqsys-request-715"),
               lambda: r.verified_feature("reqsys-request-715")):
        with pytest.raises(PrePRBlocked):
            fn()


def test_no_write_method_exposed():
    r = port()
    assert not any(hasattr(r, name) for name in (
        "create_branch", "create_draft", "put_if_absent", "post", "patch", "merge"))
