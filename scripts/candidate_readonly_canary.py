#!/usr/bin/env python3
"""Strict read-only preflight for an ALREADY ISOLATED Worker Pool 1.4 DEV canary.

This tool DOES NOT create/start a canary, attest its image or authorize cutover.
Launch/isolation/rollback are independently governed and must be proven first.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
CORRELATION = re.compile(r"[a-zA-Z0-9._:-]{1,128}\Z")
EXPECTED_API_VERSION = "1.4.0"
EXPECTED_CONTRACT_VERSION = "v1"
TASKS = {
    "cwp-cdb85c31e3cc4e972f3094ab": 2546,
    "cwp-7d84018f3319356181ab9b47": 2547,
}
REPOSITORY = "ericson-j-santos/reqsys-v2-enterprise-real"
RequestFn = Callable[[str, str | None], tuple[int, dict[str, Any]]]


class CanaryBlocked(RuntimeError):
    """Only explicit, sanitized reason codes may leave this process."""


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CanaryBlocked("redirect_blocked")


def validate_url(value: str) -> str:
    """Do not accidentally test or mutate the live legacy service on 8097."""
    if not isinstance(value, str):
        raise CanaryBlocked("candidate_url_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise CanaryBlocked("candidate_url_invalid") from None
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or not port or port == 8097 or port < 1024
            or parsed.netloc != f"127.0.0.1:{port}"
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise CanaryBlocked("candidate_url_not_isolated_loopback")
    return f"http://127.0.0.1:{port}"


def current_sha() -> str:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT,
            text=True, capture_output=True, timeout=10, check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise CanaryBlocked("checkout_unavailable") from None
    return done.stdout.strip().lower()


def read_token(token_file: Path) -> str:
    """Token remains private; never return path/content in evidence/errors."""
    try:
        if token_file.is_symlink() or not token_file.is_file():
            raise CanaryBlocked("token_file_not_regular")
        st = token_file.stat()
        if os.name != "nt" and stat.S_IMODE(st.st_mode) & 0o077:
            raise CanaryBlocked("token_file_permissions")
        if not 8 <= st.st_size <= 8192:
            raise CanaryBlocked("token_file_size")
        value = token_file.read_text(encoding="utf-8").strip()
    except CanaryBlocked:
        raise
    except (OSError, UnicodeError):
        raise CanaryBlocked("token_file_unavailable") from None
    if len(value) < 8 or "\r" in value or "\n" in value:
        raise CanaryBlocked("token_invalid")
    return value


def http_request(url: str, token: str | None) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    req = Request(url, headers=headers, method="GET")
    opener = build_opener(ProxyHandler({}), _RejectRedirects())
    try:
        with opener.open(req, timeout=7) as response:
            code = response.status
            raw = response.read(131073)
    except HTTPError as exc:
        code, raw = exc.code, b""
    except CanaryBlocked:
        raise
    except (URLError, OSError, TimeoutError):
        raise CanaryBlocked("candidate_unreachable") from None
    if 300 <= code < 400:
        raise CanaryBlocked("redirect_blocked")
    if code != 200:
        return code, {}
    if len(raw) > 131072:
        raise CanaryBlocked("response_too_large")
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeError):
        raise CanaryBlocked("response_invalid_json") from None
    if not isinstance(parsed, dict):
        raise CanaryBlocked("response_invalid_shape")
    return code, parsed


def _get(request_fn: RequestFn, url: str, token: str | None, *, status: int) -> dict[str, Any]:
    received_status, body = request_fn(url, token)
    if received_status != status:
        raise CanaryBlocked("candidate_http_status_mismatch")
    return body


def run_probe(
    *, pool_url: str, token_file: Path, expected_sha: str, correlation_id: str,
    request_fn: RequestFn = http_request, head_fn: Callable[[], str] = current_sha,
) -> dict[str, Any]:
    if not isinstance(expected_sha, str) or not SHA40.fullmatch(expected_sha):
        raise CanaryBlocked("expected_sha_invalid")
    if not isinstance(correlation_id, str) or not CORRELATION.fullmatch(correlation_id):
        raise CanaryBlocked("correlation_invalid")
    url = validate_url(pool_url)
    if head_fn().strip().lower() != expected_sha:
        raise CanaryBlocked("checkout_sha_mismatch")
    token = read_token(token_file)

    # No POST, PUT or GET on legacy snapshot/workers; all reads are GET-only.
    health = _get(request_fn, url + "/health", None, status=200)
    if (health.get("status") != "healthy"
            or health.get("contract_version") != EXPECTED_CONTRACT_VERSION
            or health.get("auth_configured") is not True):
        raise CanaryBlocked("candidate_health_invalid")
    spec = _get(request_fn, url + "/openapi.json", None, status=200)
    if (not isinstance(spec.get("info"), dict)
            or spec["info"].get("version") != EXPECTED_API_VERSION
            or "/v1/tasks/{task_id}/diagnostics" not in (spec.get("paths") or {})):
        raise CanaryBlocked("candidate_api_version_or_route_invalid")
    _get(request_fn, url + "/v1/contract", None, status=401)
    contract = _get(request_fn, url + "/v1/contract", token, status=200)
    if (contract.get("contract_name") != "engineering-worker-pool"
            or contract.get("contract_version") != EXPECTED_CONTRACT_VERSION):
        raise CanaryBlocked("candidate_contract_invalid")

    result_tasks: list[dict[str, Any]] = []
    for task_id, issue in TASKS.items():
        route = url + "/v1/tasks/" + task_id
        before = _get(request_fn, route, token, status=200)
        diag = _get(request_fn, route + "/diagnostics", token, status=200)
        after = _get(request_fn, route, token, status=200)
        task = diag.get("task")
        lane = diag.get("lane")
        if (not isinstance(task, dict) or not isinstance(lane, dict)
                or diag.get("read_mode") != "sqlite_mode_ro"
                or diag.get("claim_performed") is not False
                or diag.get("reconciliation_requested") is not False
                or task.get("task_id") != task_id or task.get("issue_number") != issue
                or task.get("repository") != REPOSITORY
                or not lane.get("configured") or lane.get("enabled") is not True
                or before.get("task_id") != task_id or after.get("task_id") != task_id
                or before.get("state") != "queued" or after.get("state") != "queued"
                or task.get("state") != "queued"
                or before.get("leased_by") is not None or after.get("leased_by") is not None
                or before.get("attempt_count") != 0 or after.get("attempt_count") != 0
                or not isinstance(diag.get("workers"), list)):
            raise CanaryBlocked("candidate_task_or_isolation_precheck_invalid")
        # Do not disclose worker IDs, token, raw task, lease, or request data.
        result_tasks.append({
            "issue_number": issue, "task_id": task_id,
            "state": "queued", "attempt_count": 0,
            "worker_count": len(diag["workers"]),
            "precheck_eligible_workers": diag.get("precheck_eligible_workers"),
        })

    # 404 on a guaranteed unknown but syntactically valid ID; no mutation.
    _get(request_fn, url + "/v1/tasks/cwp-000000000000000000000000/diagnostics", token, status=404)
    return {
        "schema_version": "1.0.0", "result": "CANDIDATE_READONLY_PREFLIGHT_OK",
        "environment": "dev", "probe_code_sha": expected_sha,
        "runtime_source_sha_attested": False,
        "api_version": EXPECTED_API_VERSION, "contract_version": EXPECTED_CONTRACT_VERSION,
        "correlation_id": correlation_id, "port": int(url.rsplit(":", 1)[-1]),
        "tasks": result_tasks, "get_only": True,
        "runtime_mutated_by_probe": False, "secrets_exposed": False,
        "production_touched": False, "candidate_image_digest_attested": False,
        "isolated_candidate_canary_passed": False, "rollback_rehearsal_passed": False,
        "cutover_ready": False,
    }


def write_evidence(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    cli = argparse.ArgumentParser(description="Read-only DEV probe for an existing isolated Worker Pool 1.4 canary")
    cli.add_argument("--pool-url", required=True)
    cli.add_argument("--token-file", type=Path, required=True)
    cli.add_argument("--expected-sha", required=True)
    cli.add_argument("--correlation-id", required=True)
    args = cli.parse_args()
    try:
        result = run_probe(pool_url=args.pool_url, token_file=args.token_file,
                           expected_sha=args.expected_sha.lower(), correlation_id=args.correlation_id)
        code = 0
    except CanaryBlocked as exc:
        result = {"schema_version": "1.0.0", "result": "CANDIDATE_PREFLIGHT_BLOCKED",
                  "reason": str(exc), "cutover_ready": False, "production_touched": False,
                  "runtime_mutated_by_probe": False}
        code = 2
    write_evidence(ROOT / "artifacts/candidate-readonly-canary/evidence.json", result)
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
