"""Read-only, fail-closed continuation admission from GitHub Actions.

A workflow can inspect the next action, but it cannot authorize code execution
with environment booleans. A native session, Gateway, exact fenced claim and
independent result reconciliation are required before handler execution; this
script deliberately does not run a handler or claim a task.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from app.native_one_shot import NativeExecutorBlocked
from app.native_pool_http import LocalWorkerApi
from app.next_action_bridge import decide_next_action

SHA40 = re.compile(r"^[0-9a-f]{40}$")
TASK_ID = re.compile(r"^cwp-[0-9a-f]{24}$")
REPOSITORY = "ericson-j-santos/engineering-worker-pool"


def blocked(code: str, **extra: Any) -> int:
    print(json.dumps({"result": "CONTINUATION_BLOCKED", "code": code,
                      "executed": False, **extra}, sort_keys=True))
    return 2


def require(condition: bool, code: str) -> None:
    if not condition:
        raise NativeExecutorBlocked(code)


def _connect() -> LocalWorkerApi:
    """Use the existing private-file, fixed-loopback Worker Pool transport.

    Never accept a task/dispatch-provided URL, a bearer token from the event,
    or a nonlocal network origin. LocalWorkerApi checks the credential file
    permissions and explicitly disables proxy and redirect handling.
    """
    raw_port = os.environ.get("EWP_WORKER_POOL_PORT", "")
    token_file = os.environ.get("EWP_WORKER_POOL_TOKEN_FILE", "")
    require(re.fullmatch(r"[0-9]{1,5}", raw_port) is not None
            and 1 <= int(raw_port) <= 65535, "worker_pool_port_invalid")
    require(bool(token_file) and Path(token_file).is_absolute(),
            "worker_pool_private_token_file_missing")
    return LocalWorkerApi(port=int(raw_port), token_file=Path(token_file))


def main() -> int:
    try:
        # A repository_dispatch client_payload is a request for inspection,
        # not an authorization to run arbitrary code on a self-hosted runner.
        require(os.environ.get("GITHUB_ACTIONS") == "true"
                and os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
                and os.environ.get("GITHUB_EVENT_NAME") in {
                    "workflow_dispatch", "repository_dispatch",
                }, "continuation_github_origin_unverified")
        code_sha = os.environ.get("GITHUB_SHA", "")
        require(SHA40.fullmatch(code_sha) is not None
                and code_sha == os.environ.get("EWP_CHECKED_OUT_SHA"),
                "continuation_code_sha_unverified")

        task_id = os.environ["EWP_TASK_ID"]
        repository = os.environ["EWP_EXPECTED_REPOSITORY"]
        raw_issue = os.environ["EWP_EXPECTED_ISSUE"]
        request_id = os.environ["EWP_EXPECTED_REQUEST_ID"]
        base_sha = os.environ["EWP_EXPECTED_BASE_SHA"]
        require(TASK_ID.fullmatch(task_id) is not None, "task_id_invalid")
        require(repository == REPOSITORY, "repository_not_allowlisted")
        require(re.fullmatch(r"[1-9][0-9]*", raw_issue) is not None,
                "issue_number_invalid")
        require(bool(request_id) and len(request_id) <= 128
                and SHA40.fullmatch(base_sha) is not None,
                "request_binding_invalid")

        # SHA equality is just a binding check, NOT financial certification.
        # No paid service, self-hosted handler or model is invoked here.
        certificate = os.environ.get("EWP_COST_ZERO_CERTIFIED_SHA", "")
        require(SHA40.fullmatch(certificate) is not None
                and certificate == base_sha
                and certificate == os.environ.get("EWP_EXPECTED_COST_ZERO_SHA"),
                "cost_zero_binding_missing_or_mismatched")

        with _connect() as api:
            task = api.get_task(task_id)
            checkpoint = api.get_latest_checkpoint(task_id)
        role = decide_next_action(
            task, checkpoint, task_id=task_id,
            expected_repository=repository, expected_issue=int(raw_issue),
            expected_request_id=request_id, expected_base_sha=base_sha,
        )
        if role in {"wait", "blocked", "done"}:
            print(json.dumps({"result": "CONTINUATION_NO_EXECUTION",
                              "state": role, "task_id": task_id,
                              "executed": False}, sort_keys=True))
            return 0

        # False attestation regression: environment flags like
        # EWP_GATEWAY_ATTESTED=1 or EWP_SESSION_LAUNCH_OK=1 are NOT receipts.
        # Do not claim a lease or invoke a handler until a separate, genuine
        # Session Launcher/Gateway native entrypoint is wired and E2E tested.
        return blocked("native_session_gateway_and_handler_not_attested",
                       next_action=role, task_id=task_id)
    except KeyError:
        return blocked("required_environment_missing")
    except (ValueError, NativeExecutorBlocked) as exc:
        return blocked(str(exc) or "continuation_blocked")
    except Exception:
        return blocked("continuation_unexpected_failure")


if __name__ == "__main__":
    raise SystemExit(main())
