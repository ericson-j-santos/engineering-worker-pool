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


def test_private_file_composition_uses_existing_reader(monkeypatch):
    """Policy integration only: the protected-reader implementation lives in EWP."""
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    calls = []
    def private_reader(path, *, max_bytes):
        calls.append((path, max_bytes))
        return b"x" * 24

    monkeypatch.setitem(sys.modules, "scripts.native_private_config",
                        SimpleNamespace(read_private=private_reader))
    client = httpx.Client(transport=httpx.MockTransport(lambda request:
        httpx.Response(200, json={"number": 715, "state": "open",
            "html_url": f"https://github.com/{REPOSITORY}/issues/715"})),
        trust_env=False)
    key_path = Path("/private/reqsys-readonly-token")
    with ReqSysReadOnlyAuthority.from_private_token_file(
        token_file=key_path, client=client,
    ) as a:
        assert a.issue(REPOSITORY, 715)["state"] == "open"
    assert calls == [(key_path, 4096)]
    assert not client.is_closed  # borrowed client lifecycle stays with caller
    client.close()


def test_private_file_composition_requires_absolute_path():
    from pathlib import Path
    with pytest.raises(PrePRBlocked, match="reqsys_readonly_private_path_invalid"):
        ReqSysReadOnlyAuthority.from_private_token_file(token_file=Path("relative"))


def test_private_file_reader_denies_access_before_network(monkeypatch):
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules, "scripts.native_private_config",
        SimpleNamespace(read_private=lambda *args, **kwargs: (_ for _ in ()).throw(
            PermissionError("private file denied"))))
    def no_network(request):
        pytest.fail("network must not be reached without a safe credential")
    client = httpx.Client(transport=httpx.MockTransport(no_network), trust_env=False)
    authority = ReqSysReadOnlyAuthority.from_private_token_file(
        token_file=Path("/private/absent"), client=client)
    with pytest.raises(PrePRBlocked, match="reqsys_readonly_credential_unavailable"):
        authority.issue(REPOSITORY, 715)
    client.close()


def test_factory_context_manager_closes_only_owned_client(monkeypatch):
    import sys
    from pathlib import Path
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules, "scripts.native_private_config",
                        SimpleNamespace(read_private=lambda *a, **k: b"x"*24))
    authority = ReqSysReadOnlyAuthority.from_private_token_file(
        token_file=Path("/private/read-only-token"))
    assert authority._owns_client is True
    with authority as a:
        assert a is authority
    assert authority._client.is_closed


def test_candidate_branch_absent_only_with_verified_github_404():
    def handler(request):
        if request.url.path.endswith("/branches/main"):
            return httpx.Response(200, json={"name": "main", "commit": {"sha": SHA}})
        return httpx.Response(404, json={"message": "Branch not found"})
    adapter = port(handler)
    assert adapter.branch_sha(REPOSITORY, "main") == SHA
    assert adapter.branch_sha(REPOSITORY, "feat/715-prepr-candidate") is None


@pytest.mark.parametrize("body", [{}, {"message": "Forbidden"}, []])
def test_candidate_404_ambiguous_body_is_denied(body):
    adapter = port(lambda req: httpx.Response(404, json=body))
    with pytest.raises(PrePRBlocked, match="reqsys_readonly_response_unverified"):
        adapter.branch_sha(REPOSITORY, "feat/715-prepr-candidate")


def test_main_missing_stays_blocked():
    adapter = port(lambda req: httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(PrePRBlocked, match="reqsys_readonly_response_unverified"):
        adapter.branch_sha(REPOSITORY, "main")


def test_candidate_unauthorized_is_not_reported_as_absent():
    adapter = port(lambda req: httpx.Response(401, json={"message": "Bad credentials"}))
    with pytest.raises(PrePRBlocked, match="reqsys_readonly_response_unverified"):
        adapter.branch_sha(REPOSITORY, "feat/715-prepr-candidate")
