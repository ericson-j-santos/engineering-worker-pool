"""Bridge autorizado TODO Global (Notion) -> GitHub -> Engineering Worker Pool.

Não consulta hosts físicos nem executa trabalho remoto; apenas cria uma task no
Worker Pool DEV quando explicitamente solicitado com --execute. O padrão é
somente diagnóstico/dry-run. Credenciais são lidas de arquivos fora do Git.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.portfolio_todo import AdmissionRejected, prepare_portfolio_task

NOTION_VERSION = "2025-09-03"
GITHUB_API = "https://api.github.com"
NOTION_API = "https://api.notion.com"
EXTERNAL_ID = re.compile(r"^github:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)$")
MAX_RESPONSE_BYTES = 262144


class BridgeRejected(ValueError):
    """Bloqueio sem risco de expor resposta bruta de provedor ou credencial."""


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise BridgeRejected(reason)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JsonTransport:
    """Transporta somente JSON entre origens fixas, sem seguir redirects."""

    def __init__(self) -> None:
        self.opener = build_opener(NoRedirect())

    def request(
        self, service: str, method: str, url: str, token: str,
        data: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        parsed = urlparse(url)
        _require(
            (service == "notion" and parsed.scheme == "https" and parsed.netloc == "api.notion.com")
            or (service == "github" and parsed.scheme == "https" and parsed.netloc == "api.github.com")
            or (service == "worker" and parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port is not None),
            "endpoint_not_allowed",
        )
        _require(method in {"GET", "POST"}, "method_not_allowed")
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
        if service == "notion":
            headers["Notion-Version"] = NOTION_VERSION
        if service == "github":
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            headers["Accept"] = "application/vnd.github+json"
        if data is not None:
            headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = Request(url, headers=headers, data=body, method=method)
        try:
            with self.opener.open(req, timeout=12) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                _require(len(raw) <= MAX_RESPONSE_BYTES, "oversized_response")
                return response.status, json.loads(raw)
        except HTTPError as exc:
            raise BridgeRejected(f"{service}_http_{exc.code}") from None
        except (URLError, OSError, ValueError) as exc:
            raise BridgeRejected(f"{service}_unavailable_or_invalid_json") from None


def _property(properties: dict[str, Any], name: str) -> str:
    data = properties.get(name) or {}
    if not isinstance(data, dict):
        return ""
    if data.get("type") == "select":
        return str((data.get("select") or {}).get("name") or "").strip()
    if data.get("type") == "url":
        return str(data.get("url") or "").strip()
    if data.get("type") == "rich_text":
        return "".join(
            str(v.get("plain_text") or (v.get("text") or {}).get("content") or "")
            for v in data.get("rich_text", []) if isinstance(v, dict)
        ).strip()
    return ""


FIELDS = ("Fonte", "Status", "Tipo", "Prioridade", "Origem URL",
          "Identificador externo", "Chave de idempotência", "Correlation ID")


def _validated_notion_page(page: Any, data_source_id: str) -> dict[str, str]:
    _require(isinstance(page, dict) and page.get("object") == "page", "notion_page_invalid")
    parent = page.get("parent") or {}
    _require(
        isinstance(parent, dict) and parent.get("type") == "data_source_id"
        and isinstance(parent.get("data_source_id"), str)
        and parent["data_source_id"].replace("-", "").lower()
        == data_source_id.replace("-", "").lower(),
        "notion_source_mismatch",
    )
    properties = page.get("properties")
    _require(isinstance(properties, dict), "notion_properties_invalid")
    return {k: _property(properties, k) for k in FIELDS}


def _get(transport, service: str, url: str, token: str) -> Any:
    status, data = transport.request(service, "GET", url, token)
    _require(status == 200, f"{service}_get_failed")
    return data


def _read_notion(transport, page_id: str, data_source_id: str, token: str) -> dict[str, str]:
    page = _get(transport, "notion", f"{NOTION_API}/v1/pages/{page_id}", token)
    _require(isinstance(page, dict), "notion_page_invalid")
    try:
        returned_id = str(uuid.UUID(page.get("id", "")))
    except (ValueError, TypeError, AttributeError):
        raise BridgeRejected("notion_page_id_invalid") from None
    _require(returned_id == page_id, "notion_page_id_mismatch")
    _require(page.get("archived") is False and page.get("in_trash") is False,
             "notion_page_not_active")
    return _validated_notion_page(page, data_source_id)


def _github_snapshot(transport, external_id: str, token: str, observed_at: datetime) -> dict[str, Any]:
    match = EXTERNAL_ID.fullmatch(external_id)
    _require(match is not None, "github_external_id_invalid")
    repo, number_text = match.groups()
    owner, name = repo.split("/")
    root = f"{GITHUB_API}/repos/{quote(owner)}/{quote(name)}"
    metadata = _get(transport, "github", root, token)
    _require(isinstance(metadata, dict) and metadata.get("full_name") == repo, "github_repo_mismatch")
    branch = str(metadata.get("default_branch") or "").strip()
    _require(branch and len(branch) <= 200, "github_default_branch_missing")
    issue_number = int(number_text)
    issue = _get(transport, "github", f"{root}/issues/{issue_number}", token)
    _require(
        isinstance(issue, dict) and type(issue.get("number")) is int
        and issue["number"] == issue_number
        and issue.get("state") == "open" and "pull_request" not in issue
        and issue.get("html_url") == f"https://github.com/{repo}/issues/{issue_number}",
        "github_issue_not_open_or_mismatch",
    )
    ref_url = f"{root}/git/ref/heads/{quote(branch, safe='/')}"
    ref = _get(transport, "github", ref_url, token)
    _require(isinstance(ref, dict) and ref.get("ref") == "refs/heads/" + branch,
             "github_ref_identity_mismatch")
    ref_object = ref.get("object")
    _require(isinstance(ref_object, dict), "github_ref_missing")
    sha = ref_object.get("sha")
    _require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) is not None,
             "github_ref_missing")
    _require(ref_object.get("type") == "commit", "github_ref_not_commit")
    return {
        "repository": repo, "issue_number": issue_number,
        "issue_url": issue["html_url"], "state": "open", "is_pull_request": False,
        "default_branch": branch, "verified_ref": branch,
        "verified_head_sha": sha, "base_sha": sha,
        "observed_at": observed_at.isoformat(),
        "_ref_url": ref_url,
    }


def _allowed_worker_url(base_url: str) -> str:
    u = urlparse(base_url.rstrip("/"))
    _require(
        u.scheme == "http" and u.hostname in {"127.0.0.1", "localhost"}
        and u.port is not None and not (u.username or u.password or u.query or u.fragment)
        and u.path in {"", "/"},
        "worker_must_be_loopback",
    )
    return base_url.rstrip("/")


def run_page(
    *, page_id: str, data_source_id: str, transport,
    notion_token: str, github_token: str, worker_token: str = "",
    worker_url: str = "", execute: bool = False,
    environment: str = "", now: datetime | None = None,
) -> dict[str, Any]:
    """Checa origem independente e só persiste em Worker Pool sob execute DEV."""
    try:
        page_id = str(uuid.UUID(page_id))
        data_source_id = str(uuid.UUID(data_source_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise BridgeRejected("notion_id_invalid") from None
    _require(bool(notion_token and github_token), "source_tokens_missing")
    clock = now or datetime.now(timezone.utc)
    _require(clock.tzinfo is not None, "clock_timezone_missing")
    todo = _read_notion(transport, page_id, data_source_id, notion_token)
    _require(todo["Status"] == "PENDENTE", "todo_not_pending")
    _require(todo["Fonte"] == "GitHub", "todo_source_invalid")
    snapshot = _github_snapshot(transport, todo["Identificador externo"], github_token, clock)
    payload = prepare_portfolio_task(todo, snapshot, now=clock)
    state = {"state": "ready_for_dispatch", "repository": payload["repository"],
             "issue_number": payload["issue_number"], "base_sha": payload["base_sha"],
             "request_id": payload["request_id"], "dispatched": False}
    if not execute:
        return state
    _require(environment == "dev", "execute_dev_only")
    _require(bool(worker_token and worker_url), "worker_credentials_missing")
    url = _allowed_worker_url(worker_url)
    lanes = _get(transport, "worker", f"{url}/v1/repositories", worker_token)
    _require(isinstance(lanes, list), "worker_lanes_invalid")
    lane = next((x for x in lanes
                 if isinstance(x, dict) and x.get("repository") == payload["repository"]), None)
    _require(
        lane is not None and lane.get("enabled") is True and lane.get("max_in_flight") == 1,
        "worker_lane_not_admitted",
    )
    # Revalidate the page, open issue and default branch before the only write.
    # Independent APIs are not a distributed transaction; the executor must
    # re-admit current state before performing work, including on replay.
    reread = _read_notion(transport, page_id, data_source_id, notion_token)
    _require(reread == todo, "todo_changed_before_dispatch")
    latest = _github_snapshot(
        transport, todo["Identificador externo"], github_token, clock,
    )
    _require(latest["default_branch"] == snapshot["default_branch"],
             "github_default_branch_changed_before_dispatch")
    _require(latest["base_sha"] == payload["base_sha"],
             "github_head_changed_before_dispatch")
    status, response = transport.request("worker", "POST", f"{url}/v1/tasks", worker_token, payload)
    _require(status in (200, 201) and isinstance(response, dict), "worker_enqueue_not_confirmed")
    task = response.get("task") or {}
    task_id = task.get("task_id")
    _require(isinstance(task_id, str) and 0 < len(task_id) < 256, "worker_task_id_missing")
    _require((status == 201) == (response.get("created") is True), "worker_created_state_invalid")
    readback = _get(transport, "worker", f"{url}/v1/tasks/{quote(task_id)}", worker_token)
    for key in ("repository", "issue_number", "base_sha", "request_id", "correlation_id"):
        _require(readback.get(key) == payload[key], "worker_readback_mismatch")
    _require(readback.get("task_id") == task_id and "lease_token" not in readback,
             "worker_readback_invalid")
    return {**state, "state": "enqueued_verified", "dispatched": True,
            "task_id": task_id, "created": response["created"]}


def _token_from_file(path: str) -> str:
    _require(bool(path), "token_file_path_missing")
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        raise BridgeRejected("token_file_unavailable") from None
    _require(1 <= len(token) <= 4096, "token_file_invalid")
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Admissão DEV governada do TODO Global")
    parser.add_argument("--page-id", required=True)
    parser.add_argument("--data-source-id", required=True)
    parser.add_argument("--execute", action="store_true", help="Persiste task em Worker Pool DEV")
    options = parser.parse_args(argv)
    try:
        result = run_page(
            page_id=options.page_id,
            data_source_id=options.data_source_id,
            transport=JsonTransport(),
            notion_token=_token_from_file(os.getenv("PORTFOLIO_NOTION_TOKEN_FILE", "")),
            github_token=_token_from_file(os.getenv("PORTFOLIO_GITHUB_TOKEN_FILE", "")),
            worker_token=_token_from_file(os.getenv("PORTFOLIO_WORKER_TOKEN_FILE", "")) if options.execute else "",
            worker_url=os.getenv("PORTFOLIO_WORKER_URL", ""),
            execute=options.execute, environment=os.getenv("PORTFOLIO_ENV", ""),
        )
    except (AdmissionRejected, BridgeRejected) as exc:
        print(json.dumps({"state": "blocked", "reason": str(exc)}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
