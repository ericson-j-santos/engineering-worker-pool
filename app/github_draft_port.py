"""Narrow authenticated GitHub REST port for issue-bound Qwen draft PRs.

Not an executor. No model, shell, checkout, workflow dispatch, merge or deploy.
A separate trusted caller must provide issue authorization and independent E2E.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.issue_draft_pr import HandoffBlocked, OWNER, PATH, REPO, SHA, require
from app.secure_file import SecureFileError, open_private_read, validate_private_fd

API = "https://api.github.com"
PREFIX = f"/repos/{REPO}"
AGENT = re.compile(r"agent/qwen7b-issue-[1-9][0-9]*-[0-9a-f]{12}\Z")
TOKEN = re.compile(r"[!-~]{16,4096}\Z")
MAX_JSON = 262144
MAX_SOURCE = 262144


class OfficialGitHubDraftPort:
    """Fixed-repository, fixed-path official GitHub adapter, fail-closed."""

    def __init__(self, *, token_file: Path, client: httpx.Client | None = None):
        self.token_file = Path(token_file)
        self._own_client = client is None
        self._client = client or httpx.Client(
            timeout=12, trust_env=False, follow_redirects=False,
        )

    def close(self) -> None:
        if self._own_client:
            self._client.close()

    def __enter__(self) -> OfficialGitHubDraftPort:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _token(self) -> str:
        try:
            fd = open_private_read(self.token_file)
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(4097)
                validate_private_fd(stream.fileno())
        except (SecureFileError, OSError):
            raise HandoffBlocked("github_token_file_unavailable") from None
        require(16 <= len(raw) <= 4096, "github_token_invalid")
        try:
            value = raw.decode("ascii").strip()
        except UnicodeError:
            raise HandoffBlocked("github_token_invalid") from None
        require(TOKEN.fullmatch(value) is not None, "github_token_invalid")
        return value

    @staticmethod
    def _ref(ref: str, *, allow_sha: bool = False) -> str:
        require(type(ref) is str
                and (ref == "main" or AGENT.fullmatch(ref) is not None
                     or (allow_sha and SHA.fullmatch(ref) is not None)),
                "github_branch_ref_invalid")
        return ref

    def _request(self, method: str, tail: str, *,
                 query: dict[str, str] | None = None,
                 payload: dict[str, Any] | None = None,
                 statuses: tuple[int, ...] = (200,),
                 missing_ok: bool = False) -> Any:
        require(method in {"GET", "POST", "PUT"} and tail.startswith("/")
                and "//" not in tail and "\\" not in tail
                and "?" not in tail and "#" not in tail
                and "\n" not in tail and "\r" not in tail,
                "github_route_invalid")
        url = API + PREFIX + tail
        try:
            request = self._client.build_request(
                method, url, params=query, json=payload,
                headers={
                    "Authorization": "Bearer " + self._token(),
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=12,
            )
            result = self._client.send(request, follow_redirects=False)
        except (httpx.HTTPError, OSError):
            # Write may have reached GitHub. No immediate blind retry.
            raise HandoffBlocked("github_result_uncertain") from None
        require(result.url.scheme == "https" and result.url.host == "api.github.com"
                and result.url.raw_path.split(b'?')[0] == (PREFIX + tail).encode('ascii')
                and not result.history,
                "github_response_origin_invalid")
        if missing_ok and result.status_code == 404:
            return None
        require(result.status_code in statuses, "github_status_unverified")
        require(len(result.content) <= MAX_JSON
                and result.headers.get("content-type", "").split(";")[0].strip()
                == "application/json", "github_json_invalid")
        try:
            data = result.json()
        except ValueError:
            raise HandoffBlocked("github_json_invalid") from None
        require(type(data) in {dict, list}, "github_json_invalid")
        return data

    def get_issue(self, number: int) -> dict:
        require(type(number) is int and number > 0, "github_issue_id_invalid")
        result = self._request("GET", f"/issues/{number}")
        require(type(result) is dict, "github_issue_shape_invalid")
        return result

    def get_comment(self, comment_id: int) -> dict:
        require(type(comment_id) is int and comment_id > 0,
                "github_comment_id_invalid")
        result = self._request("GET", f"/issues/comments/{comment_id}")
        require(type(result) is dict, "github_comment_shape_invalid")
        return result

    def get_branch(self, branch: str) -> dict | None:
        ref = self._ref(branch)
        result = self._request(
            "GET", "/branches/" + quote(ref, safe=""), missing_ok=True,
        )
        if result is None:
            return None
        require(type(result) is dict and result.get("name") == ref
                and type(result.get("commit")) is dict
                and type(result["commit"].get("sha")) is str
                and SHA.fullmatch(result["commit"]["sha"]) is not None,
                "github_branch_shape_invalid")
        return result

    def get_source(self, ref: str) -> tuple[bytes, str]:
        self._ref(ref, allow_sha=True)
        result = self._request(
            "GET", "/contents/" + PATH, query={"ref": ref},
        )
        require(type(result) is dict and result.get("type") == "file"
                and result.get("path") == PATH and result.get("encoding") == "base64"
                and type(result.get("content")) is str
                and type(result.get("sha")) is str
                and SHA.fullmatch(result["sha"]) is not None
                and type(result.get("size")) is int
                and 0 < result["size"] <= MAX_SOURCE,
                "github_source_invalid")
        try:
            raw = base64.b64decode(result["content"], validate=False)
            # GitHub JSON base64 content may include newlines.
            compact = "".join(result["content"].split())
            require(base64.b64encode(raw).decode("ascii") == compact,
                    "github_source_base64_invalid")
        except (ValueError, TypeError, base64.binascii.Error):
            raise HandoffBlocked("github_source_base64_invalid") from None
        require(len(raw) == result["size"] and len(raw) <= MAX_SOURCE
                and hashlib.sha1(
                    b"blob " + str(len(raw)).encode("ascii") + b"\x00" + raw,
                ).hexdigest() == result["sha"],
                "github_source_blob_unverified")
        return raw, result["sha"]

    def create_branch(self, name: str, sha: str) -> None:
        require(AGENT.fullmatch(name) is not None
                and type(sha) is str and SHA.fullmatch(sha) is not None,
                "github_branch_create_invalid")
        result = self._request(
            "POST", "/git/refs",
            payload={"ref": "refs/heads/" + name, "sha": sha}, statuses=(201,),
        )
        require(type(result) is dict and result.get("ref") == "refs/heads/" + name
                and type(result.get("object")) is dict
                and result["object"].get("sha") == sha,
                "github_branch_create_unverified")

    def replace_source(self, name: str, content: bytes, blob_sha: str) -> None:
        require(AGENT.fullmatch(name) is not None
                and type(content) is bytes and 0 < len(content) <= MAX_SOURCE
                and type(blob_sha) is str and SHA.fullmatch(blob_sha) is not None,
                "github_write_invalid")
        # Caller proved base SHA and branch identity. PUT is conditional on
        # exact content blob; a changing branch requires post-write scope readback.
        result = self._request(
            "PUT", "/contents/" + PATH, statuses=(200,),
            payload={
                "message": "fix: local Qwen7B validated issue-bound change",
                "branch": name, "sha": blob_sha,
                "content": base64.b64encode(content).decode("ascii"),
            },
        )
        blob = hashlib.sha1(
            b"blob " + str(len(content)).encode("ascii") + b"\x00" + content,
        ).hexdigest()
        require(type(result) is dict and type(result.get("content")) is dict
                and result["content"].get("path") == PATH
                and result["content"].get("sha") == blob
                and type(result.get("commit")) is dict
                and type(result["commit"].get("sha")) is str
                and SHA.fullmatch(result["commit"]["sha"]) is not None,
                "github_write_readback_required")

    def changed_paths(self, base_sha: str, head_sha: str) -> tuple[str, ...]:
        require(type(base_sha) is str and SHA.fullmatch(base_sha) is not None
                and type(head_sha) is str and SHA.fullmatch(head_sha) is not None,
                "github_compare_sha_invalid")
        result = self._request(
            "GET", "/compare/" + base_sha + "..." + head_sha,
        )
        require(type(result) is dict and result.get("status") == "ahead"
                and result.get("ahead_by") == 1 and result.get("behind_by") == 0
                and result.get("total_commits") == 1
                and type(result.get("files")) is list
                and len(result["files"]) == 1
                and type(result["files"][0]) is dict
                and result["files"][0].get("status") == "modified",
                "github_compare_unverified")
        return (result["files"][0].get("filename"),)

    def find_pr(self, head: str) -> dict | None:
        require(AGENT.fullmatch(head) is not None, "github_pr_head_invalid")
        result = self._request("GET", "/pulls", query={
            "state": "all", "head": OWNER + ":" + head,
            "base": "main", "per_page": "100",
        })
        require(type(result) is list and len(result) <= 1,
                "github_pr_lookup_ambiguous")
        if not result:
            return None
        pr = result[0]
        require(type(pr) is dict and type(pr.get("number")) is int
                and type(pr.get("head")) is dict
                and pr["head"].get("ref") == head
                and pr["head"].get("repo", {}).get("full_name") == REPO
                and type(pr.get("base")) is dict
                and pr["base"].get("ref") == "main"
                and pr["base"].get("repo", {}).get("full_name") == REPO,
                "github_pr_identity_invalid")
        return {
            "number": pr["number"], "state": pr.get("state"),
            "draft": pr.get("draft"), "head_sha": pr["head"].get("sha"),
            "base_ref": pr["base"].get("ref"), "body": pr.get("body"),
        }

    def create_pr(self, head: str, title: str, body: str) -> None:
        require(AGENT.fullmatch(head) is not None
                and type(title) is str and title.startswith("fix: issue #")
                and type(body) is str and len(body) < 4096,
                "github_create_pr_invalid")
        result = self._request("POST", "/pulls", payload={
            "title": title, "head": head, "base": "main",
            "body": body, "draft": True, "maintainer_can_modify": False,
        }, statuses=(201,))
        require(type(result) is dict and result.get("state") == "open"
                and result.get("draft") is True,
                "github_pr_create_unverified")
