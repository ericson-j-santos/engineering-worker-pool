#!/usr/bin/env python3
"""Smoke DEV standalone do Engineering Worker Pool.

Sem dependência do ReqSys:
- aceita apenas endpoint HTTP loopback;
- lê bearer token somente de arquivo local protegido;
- valida o SHA exato do checkout;
- cria/valida lane sintética desabilitada;
- comprova replay idempotente e leitura independente;
- nunca executa deploy nem toca produção.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SHA40 = re.compile(r"^[0-9a-f]{40}$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
SMOKE_REPOSITORY = "ericson-j-santos/engineering-worker-pool-smoke"
SMOKE_ISSUE = 1

class SmokeError(RuntimeError):
    pass

RequestFn = Callable[[str, str, str, dict[str, Any] | None], tuple[int, dict[str, Any]]]
HeadFn = Callable[[], str]

def current_sha() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
        timeout=15,
    )
    return completed.stdout.strip().lower()

def validate_pool_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
        raise SmokeError("worker_pool_url_not_loopback")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SmokeError("worker_pool_url_invalid")
    if parsed.path not in {"", "/"}:
        raise SmokeError("worker_pool_url_invalid")
    return raw

def resolve_token_file(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    configured = (
        os.getenv("CODEX_WORKER_POOL_API_TOKEN_FILE", "").strip()
        or os.getenv("CODEX_WORKER_POOL_API_TOKEN_FILE_HOST", "").strip()
    )
    if not configured:
        raise SmokeError("worker_pool_token_file_not_configured")
    return Path(configured)

def read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise SmokeError("worker_pool_token_file_missing") from exc
    except PermissionError as exc:
        raise SmokeError("worker_pool_token_permission_denied") from exc
    except OSError as exc:
        raise SmokeError("worker_pool_token_unavailable") from exc
    if not token:
        raise SmokeError("worker_pool_token_empty")
    return token

def http_request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None,
) -> tuple[int, dict[str, Any]]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
            decoded = json.loads(body) if body else {}
            if not isinstance(decoded, dict):
                raise SmokeError("worker_pool_invalid_json_shape")
            return int(response.status), decoded
    except HTTPError as exc:
        raise SmokeError(f"worker_pool_http_{exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SmokeError("worker_pool_unreachable") from exc
    except json.JSONDecodeError as exc:
        raise SmokeError("worker_pool_invalid_json") from exc

def _task_id(payload: dict[str, Any]) -> str:
    task = payload.get("task")
    if not isinstance(task, dict) or not str(task.get("task_id") or ""):
        raise SmokeError("worker_pool_task_response_invalid")
    return str(task["task_id"])

def run_smoke(
    *,
    expected_sha: str,
    correlation_id: str,
    pool_url: str,
    token_file: Path,
    request_fn: RequestFn = http_request,
    head_fn: HeadFn = current_sha,
) -> dict[str, Any]:
    expected_sha = expected_sha.strip().lower()
    if not SHA40.fullmatch(expected_sha):
        raise SmokeError("expected_sha_invalid")
    if head_fn().strip().lower() != expected_sha:
        raise SmokeError("checkout_sha_mismatch")
    if not correlation_id.strip():
        raise SmokeError("correlation_id_invalid")

    pool_url = validate_pool_url(pool_url)
    token = read_token(token_file)

    health_code, health = request_fn("GET", f"{pool_url}/health", token, None)
    if health_code != 200 or health.get("status") != "healthy":
        raise SmokeError("worker_pool_not_ready")

    lane_payload = {
        "repository": SMOKE_REPOSITORY,
        "enabled": False,
        "max_in_flight": 1,
        "correlation_id": f"{correlation_id}-lane"[:128],
    }
    lane_code, lane = request_fn(
        "POST", f"{pool_url}/v1/repositories", token, lane_payload
    )
    if lane_code != 200:
        raise SmokeError("worker_pool_smoke_lane_config_failed")
    if lane.get("repository") != SMOKE_REPOSITORY or lane.get("enabled") is not False:
        raise SmokeError("worker_pool_smoke_lane_invalid")

    task_payload = {
        "repository": SMOKE_REPOSITORY,
        "issue_number": SMOKE_ISSUE,
        "request_id": f"smoke-{expected_sha[:12]}",
        "correlation_id": f"{correlation_id}-task"[:128],
        "priority": 10,
        "base_sha": expected_sha,
        "max_attempts": 3,
    }

    first_code, first = request_fn(
        "POST", f"{pool_url}/v1/tasks", token, task_payload
    )
    if first_code not in {200, 201}:
        raise SmokeError("worker_pool_smoke_enqueue_failed")
    task_id = _task_id(first)

    replay_code, replay = request_fn(
        "POST", f"{pool_url}/v1/tasks", token, task_payload
    )
    if replay_code != 200 or replay.get("created") is not False:
        raise SmokeError("worker_pool_smoke_replay_not_idempotent")
    if _task_id(replay) != task_id:
        raise SmokeError("worker_pool_smoke_replay_task_mismatch")

    read_code, readback = request_fn(
        "GET", f"{pool_url}/v1/tasks/{task_id}", token, None
    )
    if read_code != 200 or readback.get("task_id") != task_id:
        raise SmokeError("worker_pool_smoke_readback_failed")
    if readback.get("state") != "queued" or readback.get("leased_by") is not None:
        raise SmokeError("worker_pool_smoke_task_became_executable")

    snap_code, snapshot = request_fn(
        "GET", f"{pool_url}/v1/snapshot", token, None
    )
    if snap_code != 200:
        raise SmokeError("worker_pool_smoke_snapshot_failed")
    lane_readback = next(
        (
            row
            for row in snapshot.get("repositories") or []
            if isinstance(row, dict) and row.get("repository") == SMOKE_REPOSITORY
        ),
        None,
    )
    if not isinstance(lane_readback, dict) or lane_readback.get("enabled") is not False:
        raise SmokeError("worker_pool_smoke_lane_readback_failed")

    task_readback = next(
        (
            row
            for row in snapshot.get("tasks") or []
            if isinstance(row, dict) and row.get("task_id") == task_id
        ),
        None,
    )
    if not isinstance(task_readback, dict):
        raise SmokeError("worker_pool_smoke_task_missing_from_snapshot")
    if task_readback.get("state") != "queued" or task_readback.get("leased_by") is not None:
        raise SmokeError("worker_pool_smoke_snapshot_task_invalid")

    return {
        "schema_version": "1.0.0",
        "result": "WORKER_POOL_SMOKE_PASSED",
        "correlation_id": correlation_id,
        "expected_sha": expected_sha,
        "repository": SMOKE_REPOSITORY,
        "lane_enabled": False,
        "task_id": task_id,
        "task_state": "queued",
        "leased_by": None,
        "first_created": first.get("created"),
        "replay_created": False,
        "independent_readback": True,
        "secrets_exposed": False,
        "production_touched": False,
        "deploy_executed": False,
    }

def write_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke DEV standalone do Worker Pool")
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument(
        "--pool-url",
        default=os.getenv("CODEX_WORKER_POOL_URL", "http://127.0.0.1:8097"),
    )
    parser.add_argument("--token-file", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/worker-pool-smoke/evidence.json"),
    )
    args = parser.parse_args()

    try:
        result = run_smoke(
            expected_sha=args.expected_sha,
            correlation_id=args.correlation_id,
            pool_url=args.pool_url,
            token_file=resolve_token_file(args.token_file),
        )
        write_evidence(args.output, result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (SmokeError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        blocked = {
            "schema_version": "1.0.0",
            "result": "WORKER_POOL_SMOKE_BLOCKED",
            "reason": str(exc)[:240],
            "production_touched": False,
            "deploy_executed": False,
        }
        write_evidence(args.output, blocked)
        print(json.dumps(blocked, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
