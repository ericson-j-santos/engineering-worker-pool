"""Adapter contract tests: simulated HTTP, NOT real authenticated service E2E."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from app.pre_pr_cas_gate import PrePRBlocked
from app.reqsys_readonly_authority import REPOSITORY, ReqSysReadOnlyAuthority

SHA = "a" * 40


def absolute_private_test_path(name: str) -> Path:
    """Synthetic fixture: absolute on both POSIX and Windows."""
    root = Path("C:/private") if os.name == "nt" else Path("/private")
    assert root.is_absolute()
    return root / name


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
    key_path = absolute_private_test_path("reqsys-readonly-token")
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
        token_file=absolute_private_test_path("absent"), client=client)
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
        token_file=absolute_private_test_path("read-only-token"))
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


# Notion API is mocked here; these tests DO NOT attest the real provider.
from copy import deepcopy
from app.reqsys_readonly_authority import (
    NOTION_API, NOTION_PAGE_ID, NOTION_DATA_SOURCE_ID, NOTION_VERSION,
    TODO_ID, TODO_KEY, TODO_EXTERNAL_ID, TODO_URL, NotionReqSysTodoReader,
)


def _notion_prop(value, field="rich_text"):
    if field == "number":
        return {"type": "number", "number": value}
    if field in ("select", "status"):
        return {"type": field, field: {"name": value}}
    if field == "url":
        return {"type": "url", "url": value}
    return {"type": field, field: [{"plain_text": value}]}


def _notion_page():
    return {
        "object": "page",
        "id": NOTION_PAGE_ID,
        "archived": False,
        "in_trash": False,
        "parent": {"type": "data_source_id",
                   "data_source_id": NOTION_DATA_SOURCE_ID},
        "properties": {
            "TODO ID": _notion_prop(74, "number"),
            "Chave de idempotência": _notion_prop(TODO_KEY),
            "Identificador externo": _notion_prop(TODO_EXTERNAL_ID),
            "Origem URL": _notion_prop(TODO_URL, "url"),
            "Fonte": _notion_prop("ReqSys", "select"),
            "Status": _notion_prop("BLOQUEADO", "status"),
            "Status E2E": _notion_prop("BLOQUEADO", "status"),
        },
    }


def _notion_reader(handler=None, token="n" * 24):
    def default(request):
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert str(request.url) == NOTION_API + "/v1/pages/" + NOTION_PAGE_ID
        assert request.headers["Authorization"] == "Bearer " + token
        assert request.headers["Notion-Version"] == NOTION_VERSION
        return httpx.Response(200, json=_notion_page())
    return NotionReqSysTodoReader(
        client=httpx.Client(transport=httpx.MockTransport(handler or default),
                            trust_env=False),
        token_reader=lambda: token,
    )


def test_notion_todo74_exact_authenticated_shape_and_blocked_semantics():
    reader = _notion_reader()
    assert reader.todo("TODO-74") == {
        "id": "TODO-74", "state": "BLOCKED",
        "repository": REPOSITORY, "issue_number": 715}
    github = port()
    github._notion_todo_reader = reader
    assert github.issue(REPOSITORY, 715)["state"] == "open"
    assert github.todo("TODO-74")["state"] == "BLOCKED"
    for getter in (lambda: github.authorization("reqsys715-prova-readback-20261121-v2"),
                   lambda: github.verified_feature("reqsys715-prova-readback-20261121-v2")):
        with pytest.raises(PrePRBlocked, match="provider_missing"):
            getter()


@pytest.mark.parametrize("invalid", ["todo-74", "TODO-074", "TODO-75", "TODO-74-", "", 74, None])
def test_notion_todo_scope_fail_closed_before_network(invalid):
    reader = _notion_reader(lambda request: pytest.fail("network not permitted"))
    with pytest.raises(PrePRBlocked, match="reqsys_notion_todo_scope_invalid"):
        reader.todo(invalid)


@pytest.mark.parametrize("wrong", [
    ("id", "00000000-0000-0000-0000-000000000000"),
    ("archived", True),
    ("in_trash", True),
    ("object", "database"),
])
def test_notion_page_identity_denies_tampering(wrong):
    field, value = wrong
    page = _notion_page()
    page[field] = value
    with pytest.raises(PrePRBlocked, match="reqsys_notion_page_unverified"):
        _notion_reader(lambda request: httpx.Response(200, json=page)).todo("TODO-74")


def test_notion_parent_identity_denies_foreign_data_source():
    page = _notion_page()
    page["parent"]["data_source_id"] = "a" * 36
    with pytest.raises(PrePRBlocked, match="reqsys_notion_parent_unverified"):
        _notion_reader(lambda request: httpx.Response(200, json=page)).todo("TODO-74")


@pytest.mark.parametrize("name,content", [
    ("TODO ID", _notion_prop(75, "number")),
    ("Chave de idempotência", _notion_prop("wrong")),
    ("Identificador externo", _notion_prop("github:other/repo#715")),
    ("Origem URL", _notion_prop("https://evil.example", "url")),
    ("Fonte", _notion_prop("GitHub", "select")),
    ("Status", _notion_prop("PENDENTE", "status")),
    ("Status E2E", _notion_prop("CONCLUIDO", "status")),
])
def test_notion_real_source_fields_rejected_if_different(name, content):
    page = _notion_page()
    page["properties"][name] = content
    with pytest.raises(PrePRBlocked, match="reqsys_notion_todo_unverified"):
        _notion_reader(lambda request: httpx.Response(200, json=page)).todo("TODO-74")


@pytest.mark.parametrize("status,headers,data", [
    (401, None, None),
    (404, None, None),
    (302, {"Location": "https://evil.example"}, None),
    (200, {"Content-Type": "text/html"}, b"bad"),
    (200, {"Content-Type": "application/json"}, b"{"),
])
def test_notion_bad_http_fails_closed(status, headers, data):
    def handler(request):
        if data is not None:
            return httpx.Response(status, headers=headers, content=data)
        return httpx.Response(status, headers=headers or {}, json={"message": "denied"})
    with pytest.raises(PrePRBlocked, match="reqsys_notion_response_"):
        _notion_reader(handler).todo("TODO-74")


def test_notion_no_token_must_not_touch_network():
    reader = _notion_reader(lambda request: pytest.fail("must not call network"), token="bad")
    with pytest.raises(PrePRBlocked, match="reqsys_notion_credential_unavailable"):
        reader.todo("TODO-74")


def test_notion_private_reader_fails_before_network(monkeypatch):
    import sys
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules, "scripts.native_private_config",
                        SimpleNamespace(read_private=lambda *a, **k: (_ for _ in ()).throw(
                            PermissionError("denied"))))
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: pytest.fail("network should not be reached")), trust_env=False)
    with NotionReqSysTodoReader.from_private_token_file(
        token_file=absolute_private_test_path("notion-token"), client=client,
    ) as reader:
        with pytest.raises(PrePRBlocked, match="reqsys_notion_credential_unavailable"):
            reader.todo("TODO-74")
    assert not client.is_closed
    client.close()


def test_notion_private_reader_rejects_relative_token_file():
    with pytest.raises(PrePRBlocked, match="reqsys_notion_private_path_invalid"):
        NotionReqSysTodoReader.from_private_token_file(token_file=Path("relative"))


def test_notion_todo_reader_rejects_untrusted_injection():
    with pytest.raises(PrePRBlocked, match="reqsys_notion_config_invalid"):
        ReqSysReadOnlyAuthority(
            client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)), trust_env=False),
            token_reader=lambda: "x"*24,
            notion_todo_reader=lambda todo_id: {"state": "AUTHORIZED"},
        )


# Regression against real TODO Global schema: Notion's auto_increment_id
# is a REST unique_id object, not a number; correlation is a typed rich_text.
def test_notion_actual_unique_id_todo74_is_accepted():
    page = _notion_page()
    page["properties"]["TODO ID"] = {
        "type": "unique_id", "unique_id": {"prefix": "TODO", "number": 74}}
    page["properties"]["Correlation ID"] = _notion_prop(
        "reqsys-prova-rapida-20261120")
    assert _notion_reader(lambda request: httpx.Response(
        200, json=page)).todo("TODO-74")["state"] == "BLOCKED"


def test_notion_correlation_id_mismatch_must_fail_closed():
    page = _notion_page()
    page["properties"]["Correlation ID"] = _notion_prop("wrong-correlation")
    with pytest.raises(PrePRBlocked, match="reqsys_notion_todo_unverified"):
        _notion_reader(lambda request: httpx.Response(
            200, json=page)).todo("TODO-74")


def test_notion_unique_id_prefix_mismatch_must_fail_closed():
    page = _notion_page()
    page["properties"]["TODO ID"] = {
        "type": "unique_id", "unique_id": {"prefix": "WRONG", "number": 74}}
    page["properties"]["Correlation ID"] = _notion_prop(
        "reqsys-prova-rapida-20261120")
    with pytest.raises(PrePRBlocked, match="reqsys_notion_todo_unverified"):
        _notion_reader(lambda request: httpx.Response(
            200, json=page)).todo("TODO-74")

