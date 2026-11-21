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


class ReqSysReadOnlyAuthority:
    """Authenticated GET-only adapter; never a complete PrePR Authority."""

    def __init__(self, *, client: httpx.Client,
                 token_reader: Callable[[], str]):
        require(isinstance(client, httpx.Client) and callable(token_reader),
                "reqsys_readonly_config_invalid")
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

    def _get(self, tail: str) -> dict:
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
                and not response.history and response.status_code == 200,
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

    def branch_sha(self, repository: str, ref: str) -> str:
        require(repository == REPOSITORY and type(ref) is str
                and BRANCH.fullmatch(ref) is not None
                and not ref.startswith("refs/")
                and ".." not in ref and "//" not in ref
                and not ref.endswith("/") and not ref.endswith(".lock")
                and all(not part.startswith(".") for part in ref.split("/")),
                "reqsys_readonly_branch_scope_invalid")
        obj = self._get("/branches/" + quote(ref, safe=""))
        sha = (obj.get("commit") or {}).get("sha") if type(obj.get("commit")) is dict else None
        require(obj.get("name") == ref and type(sha) is str
                and SHA.fullmatch(sha) is not None,
                "reqsys_readonly_sha_unverified")
        return sha

    def todo(self, todo_id: str) -> dict:
        raise PrePRBlocked("reqsys_readonly_todo_provider_missing")

    def authorization(self, request_id: str) -> dict:
        raise PrePRBlocked("reqsys_readonly_authorization_provider_missing")

    def verified_feature(self, request_id: str) -> dict:
        raise PrePRBlocked("reqsys_readonly_feature_provider_missing")
