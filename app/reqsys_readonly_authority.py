"""Narrow authenticated GitHub read port for ReqSys pre-PR qualification.

Reads source of truth for Issue and branch SHA, and *nothing else*.
Classification, TODO, authorization and CAS require independent trusted
providers; this adapter MUST NOT assert or mint those decisions.

A trusted composition root supplies an httpx.Client and a secure token reader.
No token, authentication, filesystem mutation, CI dispatch, or PR creation
is implemented by this module.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import httpx

from app.pre_pr_cas_gate import PrePRBlocked, require

REPOSITORY = "ericson-j-santos/reqsys-v2-enterprise-real"
API = "https://api.github.com"
PREFIX = "/repos/" + REPOSITORY
SHA = re.compile(r"[0-9a-f]{40}\Z")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{2,159}\Z")
MAX_BODY = 65536


# Pinned owner-approved Notion TODO; this is STATE evidence, not authorization.
NOTION_API = "https://api.notion.com"
NOTION_PAGE_ID = "41f4c12b-ae7c-8144-a9c8-ccd3785976b6"
NOTION_DATA_SOURCE_ID = "663a8805-d9a6-48dc-92a0-308d7a9e87ac"
NOTION_VERSION = "2026-03-11"
TODO_ID = "TODO-74"
TODO_KEY = "reqsys:ux:prova-rapida:20261120:v1"
TODO_CORRELATION = "reqsys-prova-rapida-20261120"
TODO_EXTERNAL_ID = "github:" + REPOSITORY + "#715:prova-rapida"
TODO_URL = f"https://github.com/{REPOSITORY}/issues/715"


def _notion_property(props: dict, name: str) -> str | int | None:
    """Extract only official Notion property types, never untyped text."""
    item = props.get(name)
    if type(item) is not dict:
        return None
    kind = item.get("type")
    if kind == "unique_id":
        value = item.get("unique_id")
        if (type(value) is dict and value.get("prefix") == "TODO"
                and type(value.get("number")) is int
                and 1 <= value["number"] <= 9999999999):
            return "TODO-" + str(value["number"])
        return None
    if kind == "number" and type(item.get("number")) is int:
        return item["number"]
    if kind in ("rich_text", "title"):
        parts = item.get(kind)
        if type(parts) is not list or len(parts) > 20:
            return None
        if not all(type(v) is dict and type(v.get("plain_text")) is str
                   for v in parts):
            return None
        return "".join(v["plain_text"] for v in parts)
    if kind in ("select", "status"):
        value = item.get(kind)
        if type(value) is dict and type(value.get("name")) is str:
            return value["name"]
    if kind == "url" and type(item.get("url")) is str:
        return item["url"]
    return None


class NotionReqSysTodoReader:
    """Exact-page GET-only state reader; NEVER a decision/approval issuer."""

    def __init__(self, *, client: httpx.Client,
                 token_reader: Callable[[], str]):
        require(isinstance(client, httpx.Client) and callable(token_reader),
                "reqsys_notion_config_invalid")
        self._client = client
        self._token_reader = token_reader
        self._owns_client = False

    @classmethod
    def from_private_token_file(
        cls, *, token_file: Path, client: httpx.Client | None = None,
    ) -> NotionReqSysTodoReader:
        require(isinstance(token_file, Path) and token_file.is_absolute(),
                "reqsys_notion_private_path_invalid")
        from scripts.native_private_config import read_private

        def token_reader() -> str:
            try:
                return read_private(token_file, max_bytes=4096).decode("ascii").strip()
            except Exception:
                raise PrePRBlocked("reqsys_notion_credential_unavailable") from None

        owns_client = client is None
        if client is None:
            client = httpx.Client(trust_env=False, follow_redirects=False,
                                  timeout=12)
        result = cls(client=client, token_reader=token_reader)
        result._owns_client = owns_client
        return result

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> NotionReqSysTodoReader:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def todo(self, todo_id: str) -> dict:
        require(type(todo_id) is str and todo_id == TODO_ID,
                "reqsys_notion_todo_scope_invalid")
        try:
            token = self._token_reader()
        except Exception:
            raise PrePRBlocked("reqsys_notion_credential_unavailable") from None
        require(type(token) is str and 16 <= len(token) <= 4096
                and all(33 <= ord(c) <= 126 for c in token),
                "reqsys_notion_credential_unavailable")
        path = f"/v1/pages/{NOTION_PAGE_ID}"
        try:
            request = self._client.build_request(
                "GET", NOTION_API + path,
                headers={"Authorization": "Bearer " + token,
                         "Notion-Version": NOTION_VERSION,
                         "Accept": "application/json"}, timeout=12,
            )
            response = self._client.send(request, follow_redirects=False)
        except (httpx.HTTPError, OSError):
            raise PrePRBlocked("reqsys_notion_network_unavailable") from None
        require(response.url.scheme == "https"
                and response.url.host == "api.notion.com"
                and response.url.raw_path.split(b"?")[0] == path.encode("ascii")
                and not response.history and response.status_code == 200,
                "reqsys_notion_response_unverified")
        require(len(response.content) <= MAX_BODY
                and response.headers.get("content-type", "").split(";")[0].strip()
                == "application/json",
                "reqsys_notion_response_invalid")
        try:
            page = response.json()
        except ValueError:
            raise PrePRBlocked("reqsys_notion_response_invalid") from None
        require(type(page) is dict and page.get("object") == "page",
                "reqsys_notion_page_type_unverified")
        require(page.get("id") == NOTION_PAGE_ID,
                "reqsys_notion_page_id_unverified")
        # Notion-Version 2026-03-11 removes the deprecated archived alias.
        # If it is present, it must not conflict with the mandatory in_trash=false.
        require("archived" not in page or page.get("archived") is False,
                "reqsys_notion_archived_unverified")
        require(page.get("in_trash") is False,
                "reqsys_notion_trash_unverified")
        parent = page.get("parent")
        require(type(parent) is dict
                and parent.get("type") == "data_source_id"
                and parent.get("data_source_id") == NOTION_DATA_SOURCE_ID,
                "reqsys_notion_parent_unverified")
        props = page.get("properties")
        require(type(props) is dict
                and _notion_property(props, "TODO ID") == TODO_ID
                and _notion_property(props, "Chave de idempotência") == TODO_KEY
                and _notion_property(props, "Correlation ID") == TODO_CORRELATION
                and _notion_property(props, "Identificador externo") == TODO_EXTERNAL_ID
                and _notion_property(props, "Origem URL") == TODO_URL
                and _notion_property(props, "Fonte") == "ReqSys"
                and _notion_property(props, "Status") == "BLOQUEADO"
                and _notion_property(props, "Status E2E") == "BLOQUEADO",
                "reqsys_notion_todo_unverified")
        # BLOCKED is an observation; only the independent controller can
        # authorize or classify a feature, even if Notion returns this row.
        return {"id": TODO_ID, "state": "BLOCKED", "repository": REPOSITORY,
                "issue_number": 715}


class ReqSysReadOnlyAuthority:
    """Authenticated GET-only adapter; never a complete PrePR Authority."""

    def __init__(self, *, client: httpx.Client,
                 token_reader: Callable[[], str],
                 notion_todo_reader: NotionReqSysTodoReader | None = None):
        require(isinstance(client, httpx.Client) and callable(token_reader),
                "reqsys_readonly_config_invalid")
        require(notion_todo_reader is None or
                isinstance(notion_todo_reader, NotionReqSysTodoReader),
                "reqsys_notion_config_invalid")
        self._notion_todo_reader = notion_todo_reader
        self._client = client
        self._token_reader = token_reader
        self._owns_client = False

    @classmethod
    def from_private_token_file(
        cls, *, token_file: Path, client: httpx.Client | None = None,
    ) -> ReqSysReadOnlyAuthority:
        """Compose with the EXISTING protected-file reader, not a new secret store.

        The trusted caller must separately attest OIDC identity, repository,
        permissions, cost and gateway session. This factory proves NONE of those.
        An absent/unsafe token fails closed; it is never logged or returned.
        """
        require(isinstance(token_file, Path) and token_file.is_absolute(),
                "reqsys_readonly_private_path_invalid")
        from scripts.native_private_config import read_private

        def token_reader() -> str:
            try:
                return read_private(token_file, max_bytes=4096).decode("ascii").strip()
            except Exception:
                raise PrePRBlocked("reqsys_readonly_credential_unavailable") from None

        owns_client = client is None
        if client is None:
            client = httpx.Client(trust_env=False, follow_redirects=False,
                                  timeout=12)
        instance = cls(client=client, token_reader=token_reader)
        instance._owns_client = owns_client
        return instance

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> ReqSysReadOnlyAuthority:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _get(self, tail: str, *, missing_ok: bool = False) -> dict | None:
        require(type(tail) is str and tail.startswith("/")
                and "//" not in tail and "\\" not in tail
                and "?" not in tail and "#" not in tail
                and "\r" not in tail and "\n" not in tail,
                "reqsys_readonly_route_invalid")
        try:
            token = self._token_reader()
        except Exception:
            raise PrePRBlocked("reqsys_readonly_credential_unavailable") from None
        require(type(token) is str and 16 <= len(token) <= 4096
                and all(33 <= ord(c) <= 126 for c in token),
                "reqsys_readonly_credential_unavailable")
        path = PREFIX + tail
        try:
            request = self._client.build_request(
                "GET", API + path,
                headers={"Authorization": "Bearer " + token,
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"},
                timeout=12,
            )
            response = self._client.send(request, follow_redirects=False)
        except (httpx.HTTPError, OSError):
            raise PrePRBlocked("reqsys_readonly_network_unavailable") from None
        require(response.url.scheme == "https"
                and response.url.host == "api.github.com"
                and response.url.raw_path.split(b"?")[0] == path.encode("ascii")
                and not response.history,
                "reqsys_readonly_response_unverified")
        # Only the branch GET may report an authenticated absence.
        if missing_ok and response.status_code == 404:
            require(tail.startswith("/branches/")
                    and len(response.content) <= MAX_BODY
                    and response.headers.get("content-type", "").split(";")[0].strip()
                    == "application/json",
                    "reqsys_readonly_response_unverified")
            try:
                absent = response.json()
            except ValueError:
                raise PrePRBlocked("reqsys_readonly_response_unverified") from None
            require(type(absent) is dict
                    and absent.get("message") in ("Not Found", "Branch not found"),
                    "reqsys_readonly_response_unverified")
            return None
        require(response.status_code == 200,
                "reqsys_readonly_response_unverified")
        require(len(response.content) <= MAX_BODY
                and response.headers.get("content-type", "").split(";")[0].strip()
                == "application/json",
                "reqsys_readonly_response_invalid")
        try:
            obj = response.json()
        except ValueError:
            raise PrePRBlocked("reqsys_readonly_response_invalid") from None
        require(type(obj) is dict,
                "reqsys_readonly_response_invalid")
        return obj

    def issue(self, repository: str, number: int) -> dict:
        require(repository == REPOSITORY and type(number) is int
                and 0 < number <= 999999,
                "reqsys_readonly_issue_scope_invalid")
        obj = self._get(f"/issues/{number}")
        require(obj.get("number") == number
                and obj.get("state") in ("open", "closed")
                and obj.get("html_url") ==
                f"https://github.com/{REPOSITORY}/issues/{number}"
                and "pull_request" not in obj,
                "reqsys_readonly_issue_unverified")
        return {"repository": REPOSITORY, "number": number,
                "state": obj["state"], "is_pull_request": False}

    def branch_sha(self, repository: str, ref: str) -> str | None:
        require(repository == REPOSITORY and type(ref) is str
                and BRANCH.fullmatch(ref) is not None
                and not ref.startswith("refs/")
                and ".." not in ref and "//" not in ref
                and not ref.endswith("/") and not ref.endswith(".lock")
                and all(not part.startswith(".") for part in ref.split("/")),
                "reqsys_readonly_branch_scope_invalid")
        obj = self._get("/branches/" + quote(ref, safe=""), missing_ok=(ref != "main"))
        if obj is None:
            return None
        sha = (obj.get("commit") or {}).get("sha") if type(obj.get("commit")) is dict else None
        require(obj.get("name") == ref and type(sha) is str
                and SHA.fullmatch(sha) is not None,
                "reqsys_readonly_sha_unverified")
        return sha

    def todo(self, todo_id: str) -> dict:
        if self._notion_todo_reader is None:
            raise PrePRBlocked("reqsys_readonly_todo_provider_missing")
        return self._notion_todo_reader.todo(todo_id)

    def authorization(self, request_id: str) -> dict:
        raise PrePRBlocked("reqsys_readonly_authorization_provider_missing")

    def verified_feature(self, request_id: str) -> dict:
        raise PrePRBlocked("reqsys_readonly_feature_provider_missing")
