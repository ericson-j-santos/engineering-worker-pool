"""Official, read-only GitHub evidence for the canonical pre-PR stage.

This connector is deliberately scoped to the first, public Noteri consumer.
It does not publish branches, issue comments, artifacts, PRs, status checks,
or grants. It never interprets issue text as an instruction or evidence.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote

import httpx

from app.native_one_shot import NativeExecutorBlocked
from app.secure_file import SecureFileError, open_private_read, validate_private_fd

API = "https://api.github.com"
REPOSITORY = "ericson-j-santos/noteri-runtime"
ISSUE = 42
BRANCH = "fix/noteri-probe-rules-sha-20261106"
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_FILES = 100
TOKEN = re.compile(r"[!-~]{16,4096}\Z")


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise NativeExecutorBlocked(code)


class OfficialNoteriReadback:
    """Authenticated GET-only transport. The source SHA is never read from issue text."""

    def __init__(self, *, token_file: Path, base_sha: str, source_sha: str,
                 client: httpx.Client | None = None) -> None:
        _need(type(base_sha) is str and SHA40.fullmatch(base_sha) is not None
              and type(source_sha) is str and SHA40.fullmatch(source_sha) is not None
              and base_sha != source_sha, "prepr_github_sha_config_invalid")
        self.base_sha, self.source_sha = base_sha, source_sha
        self.token_file = Path(token_file)
        self._owned_client = client is None
        self.client = client if client is not None else httpx.Client(
            trust_env=False, follow_redirects=False, timeout=12,
        )

    def close(self) -> None:
        if self._owned_client:
            self.client.close()

    def __enter__(self) -> "OfficialNoteriReadback":
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
            raise NativeExecutorBlocked("prepr_github_token_unavailable") from None
        _need(16 <= len(raw) <= 4096, "prepr_github_token_invalid")
        try:
            token = raw.decode("ascii").strip()
        except UnicodeError:
            raise NativeExecutorBlocked("prepr_github_token_invalid") from None
        _need(TOKEN.fullmatch(token) is not None,
              "prepr_github_token_invalid")
        return token

    def _get(self, endpoint: str) -> dict:
        _need(type(endpoint) is str and endpoint.startswith("/repos/" + REPOSITORY + "/")
              and "//" not in endpoint and "\\" not in endpoint
              and "?" not in endpoint and "#" not in endpoint,
              "prepr_github_route_invalid")
        url = API + endpoint
        try:
            request = self.client.build_request(
                "GET", url, headers={
                    "Authorization": "Bearer " + self._token(),
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                }, timeout=12,
            )
            response = self.client.send(request, follow_redirects=False)
        except (httpx.HTTPError, OSError):
            raise NativeExecutorBlocked("prepr_github_unavailable") from None
        _need(response.url == httpx.URL(url) and not response.history
              and response.status_code == 200
              and len(response.content) <= MAX_RESPONSE_BYTES
              and response.headers.get("content-type", "").split(";")[0].strip()
              == "application/json", "prepr_github_response_unverified")
        try:
            payload = response.json()
        except ValueError:
            raise NativeExecutorBlocked("prepr_github_json_invalid") from None
        _need(type(payload) is dict, "prepr_github_json_invalid")
        return payload

    def __call__(self, repository: str, issue_number: int) -> dict:
        _need(repository == REPOSITORY and type(issue_number) is int
              and issue_number == ISSUE, "prepr_github_target_not_allowlisted")
        prefix = "/repos/" + REPOSITORY
        issue = self._get(prefix + "/issues/" + str(ISSUE))
        _need(issue.get("number") == ISSUE and issue.get("state") == "open"
              and "pull_request" not in issue
              and issue.get("html_url") == "https://github.com/" + REPOSITORY
              + "/issues/" + str(ISSUE)
              and issue.get("repository_url") == API + prefix,
              "prepr_github_issue_invalid")
        main = self._get(prefix + "/branches/main")
        branch = self._get(prefix + "/branches/" + quote(BRANCH, safe=""))
        _need(main.get("name") == "main" and branch.get("name") == BRANCH
              and type(main.get("commit")) is dict
              and type(branch.get("commit")) is dict
              and main["commit"].get("sha") == self.base_sha
              and branch["commit"].get("sha") == self.source_sha,
              "prepr_github_head_drift")
        compare = self._get(
            prefix + "/compare/" + self.base_sha + "..." + self.source_sha,
        )
        _need(compare.get("status") == "ahead"
              and type(compare.get("ahead_by")) is int
              and compare["ahead_by"] >= 1
              and compare.get("behind_by") == 0
              and type(compare.get("total_commits")) is int
              and compare["total_commits"] == compare["ahead_by"]
              and type(compare.get("base_commit")) is dict
              and compare["base_commit"].get("sha") == self.base_sha
              and type(compare.get("merge_base_commit")) is dict
              and compare["merge_base_commit"].get("sha") == self.base_sha,
              "prepr_github_ancestry_unproved")
        files = compare.get("files")
        _need(type(files) is list and 1 <= len(files) < MAX_FILES
              and all(type(f) is dict
                      and type(f.get("filename")) is str
                      and f.get("status") in ("added", "modified", "removed")
                      for f in files), "prepr_github_diff_invalid")
        paths = [f["filename"] for f in files]
        _need(len(set(paths)) == len(paths), "prepr_github_diff_invalid")
        # A second HEAD read detects drift while the compare was being read.
        after = self._get(prefix + "/branches/" + quote(BRANCH, safe=""))
        _need(after.get("name") == BRANCH
              and type(after.get("commit")) is dict
              and after["commit"].get("sha") == self.source_sha,
              "prepr_github_head_drift")
        return {
            "repository": REPOSITORY, "issue_number": ISSUE,
            "issue_state": "open", "main_sha": self.base_sha,
            "branch_head_sha": self.source_sha,
            "base_is_ancestor": True, "changed_paths": sorted(paths),
        }
