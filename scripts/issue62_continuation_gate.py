"""Governed Issue 62 continuation entrypoint.

Reads one authenticated Worker Pool task/checkpoint and routes only to a
preinstalled, attested handler. It never interprets queue text as a command.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from app.next_action_bridge import decide_next_action
from app.native_one_shot import NativeExecutorBlocked

SHA40 = re.compile(r"^[0-9a-f]{40}$")
TASK_ID = re.compile(r"^cwp-[0-9a-f]{24}$")
ALLOWED_HANDLERS = {"issue-qwen7b-native-v1"}
REQUIRED_COST_VAR = "EWP_COST_ZERO_CERTIFIED_SHA"


def blocked(code: str, **extra: Any) -> int:
    print(json.dumps({"result": "CONTINUATION_BLOCKED", "code": code, **extra},
                     sort_keys=True))
    return 2


def require(value: bool, code: str) -> None:
    if not value:
        raise NativeExecutorBlocked(code)


def api_json(base: str, token: str, method: str, path: str) -> dict[str, Any]:
    require(re.fullmatch(r"https?://[^/]{1,255}", base) is not None,
            "worker_pool_url_invalid")
    require(token and len(token) <= 4096, "worker_pool_token_missing")
    request = urllib.request.Request(
        base + path, method=method,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            require(response.status in {200, 201}, "worker_pool_http_status")
            value = json.loads(response.read(2_000_000))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
            json.JSONDecodeError):
        raise NativeExecutorBlocked("worker_pool_readback_unavailable") from None
    require(type(value) is dict, "worker_pool_payload_invalid")
    return value


def main() -> int:
    try:
        base = os.environ["EWP_WORKER_POOL_URL"].rstrip("/")
        token = os.environ["EWP_WORKER_POOL_TOKEN"]
        task_id = os.environ["EWP_TASK_ID"]
        repository = os.environ["EWP_EXPECTED_REPOSITORY"]
        issue = int(os.environ["EWP_EXPECTED_ISSUE"])
        request_id = os.environ["EWP_EXPECTED_REQUEST_ID"]
        base_sha = os.environ["EWP_EXPECTED_BASE_SHA"]
        expected_cost_sha = os.environ["EWP_EXPECTED_COST_ZERO_SHA"]

        require(TASK_ID.fullmatch(task_id) is not None, "task_id_invalid")
        require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                is not None and issue > 0, "task_binding_invalid")
        require(request_id and len(request_id) <= 128
                and SHA40.fullmatch(base_sha) is not None,
                "request_binding_invalid")
        require(expected_cost_sha == os.environ.get(REQUIRED_COST_VAR),
                "cost_zero_certificate_missing_or_mismatched")
        require(os.environ.get("EWP_CHECKED_OUT_SHA") == base_sha,
                "source_sha_mismatch")

        task = api_json(base, token, "GET", f"/v1/tasks/{task_id}")
        checkpoint_payload = api_json(
            base, token, "GET", f"/v1/tasks/{task_id}/checkpoints/latest")
        checkpoint = checkpoint_payload.get("checkpoint")
        require(checkpoint is None or type(checkpoint) is dict,
                "checkpoint_payload_invalid")

        role = decide_next_action(
            task, checkpoint, task_id=task_id,
            expected_repository=repository, expected_issue=issue,
            expected_request_id=request_id, expected_base_sha=base_sha,
        )
        if role in {"wait", "blocked", "done"}:
            print(json.dumps({"result": "CONTINUATION_NO_EXECUTION",
                               "state": role, "task_id": task_id},
                              sort_keys=True))
            return 0

        require(os.environ.get("EWP_HANDLER_ATTESTED") == "1",
                "handler_not_attested")
        require(os.environ.get("EWP_GATEWAY_ATTESTED") == "1",
                "gateway_not_attested")
        handler_id = os.environ.get("EWP_HANDLER_ID", "")
        require(handler_id in ALLOWED_HANDLERS, "handler_profile_not_allowed")
        require(os.environ.get("EWP_SESSION_LAUNCH_OK") == "1"
                and os.environ.get("EWP_STATE_VALIDATED") == "true",
                "session_not_attested")
        require(os.environ.get("EWP_HANDLER_ENTRYPOINT")
                == "scripts/native_live_driver.py",
                "handler_entrypoint_not_allowed")
        print(json.dumps({"result": "CONTINUATION_READY_FOR_HANDLER",
                           "state": role, "handler": handler_id,
                           "task_id": task_id}, sort_keys=True))
        return 0
    except KeyError as exc:
        return blocked("required_environment_missing", variable=str(exc))
    except (ValueError, NativeExecutorBlocked) as exc:
        return blocked(str(exc) or "continuation_blocked")
    except Exception:
        return blocked("continuation_unexpected_failure")


if __name__ == "__main__":
    raise SystemExit(main())
