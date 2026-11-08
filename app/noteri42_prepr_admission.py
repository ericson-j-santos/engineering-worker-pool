"""Readiness-gated admission of the single authorized Noteri #42 pre-PR task.

This reuses the current fenced Worker Pool lane; it is not a scheduler.
No defaults create lanes, provision tokens, start hosts, execute Ollama,
produce a CAS object, set READY_FOR_PR, or publish PRs. All evidence readers
must be independently bound by trusted runtime configuration. The endpoint
and queue URL are NOT selected by issue text or the model.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from app.native_one_shot import NativeExecutorBlocked
from app.noteri42_native_qwen import SOURCE_SHA

REPO = "ericson-j-santos/noteri-runtime"
ISSUE = 42
BASE_SHA = "e10b3a00a9b3ec803ed4a4cc8f4f8c62b06c8928"
BRANCH = "fix/noteri-probe-rules-sha-20261106"
RULES_SHA = "86abe65b22781c2f608a7d7286ce3a28cd48aaaf"
TODO_ID = "TODO-54"
CORRELATION_ID = "noteri-issue42-prepr-cas-route-20261107"
REQUEST_ID = "issue-" + hashlib.sha256(
    ("noteri42/prepr/v1/" + SOURCE_SHA).encode("ascii")
).hexdigest()
EXPECTED_FILES = tuple(sorted((
    ".github/workflows/noteri-control-plane-probe.yml",
    "scripts/noteri_control_plane_watchdog_uac_launcher.py",
    "scripts/noteri_qualification_receipt.py",
    "scripts/noteri_watchdog_readonly_inventory.py",
    "tests/test_noteri_qualification_confirmation.py",
    "tests/test_noteri_qualification_receipt.py",
    "tests/test_noteri_watchdog_readonly_inventory.py",
)))


def _need(ok: bool, reason: str) -> None:
    if not ok:
        raise NativeExecutorBlocked(reason)


class ExistingFencedPool(Protocol):
    def list_repositories(self) -> list[dict[str, Any]]: ...

    def enqueue_fenced_noteri42(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def _source(value: Any) -> None:
    _need(type(value) is dict and value.get("repository") == REPO
          and type(value.get("issue_number")) is int
          and value["issue_number"] == ISSUE
          and value.get("issue_state") == "open"
          and value.get("main_sha") == BASE_SHA
          and value.get("branch_head_sha") == SOURCE_SHA
          and value.get("base_is_ancestor") is True
          and value.get("changed_paths") == list(EXPECTED_FILES),
          "noteri42_source_authority_unverified")


def _todo(value: Any) -> None:
    _need(type(value) is dict
          and value.get("todo_id") == TODO_ID
          and value.get("external_id") == REPO + "#42"
          and value.get("correlation_id") == CORRELATION_ID
          and value.get("status") == "AUTORIZADO_PARA_EXECUCAO"
          and value.get("source_sha") == SOURCE_SHA
          and value.get("canonical") is True,
          "noteri42_todo_not_authorized")


def _approval(value: Any) -> None:
    _need(type(value) is dict
          and value.get("repository") == REPO
          and value.get("issue_number") == ISSUE
          and value.get("source_sha") == SOURCE_SHA
          and value.get("approved") is True
          and value.get("authority") == "authenticated_owner_approval",
          "noteri42_owner_approval_unverified")


def _cost(value: Any) -> None:
    _need(type(value) is dict
          and value.get("incremental_brl") == "0.00"
          and value.get("overage_hard_blocked") is True
          and value.get("runtime_covered") is True,
          "noteri42_cost_unverified")


def _host(value: Any) -> None:
    _need(type(value) is dict
          and value.get("source_sha") == SOURCE_SHA
          and value.get("rules_sha") == RULES_SHA
          and value.get("host_id") in ("Noteri", "DESKTOP-PDQK954", "DESKTOP-RP23OGS")
          and value.get("physical_identity_verified") is True
          and value.get("profile") == "NORMAL"
          and value.get("session_launcher") == "SESSION_LAUNCH_OK"
          and value.get("state_validated") is True
          and value.get("gateway_verified") is True
          and value.get("exclusive_worktree") is True
          and value.get("exclusive_lease") is True
          and value.get("native_deps_ready") is True
          and value.get("model_name") == "qwen2.5-coder:7b"
          and type(value.get("model_digest")) is str
          and re.fullmatch(r"[0-9a-f]{64}", value["model_digest"]) is not None
          and value.get("model_local_only") is True
          and value.get("ready_for_work") is True,
          "noteri42_host_not_ready")


def _lane(pool: ExistingFencedPool) -> None:
    lanes = pool.list_repositories()
    _need(type(lanes) is list and len(lanes) <= 128
          and all(type(v) is dict for v in lanes),
          "noteri42_lane_unverified")
    matched = [v for v in lanes if v.get("repository") == REPO]
    _need(len(matched) == 1 and matched[0].get("enabled") is True
          and type(matched[0].get("max_in_flight")) is int
          and matched[0]["max_in_flight"] == 1,
          "noteri42_lane_not_ready")


def admit_exact(
    *, source_reader: Callable[[str, int], Mapping[str, Any]],
    todo_reader: Callable[[str], Mapping[str, Any]],
    approval_reader: Callable[[str, int], Mapping[str, Any]],
    cost_reader: Callable[[], Mapping[str, Any]],
    host_reader: Callable[[], Mapping[str, Any]],
    pool: ExistingFencedPool, environment: str,
) -> dict[str, Any]:
    """Require full fresh authority before one idempotent, fenced enqueue.

    Re-read external authority immediately before the sole POST. In absence
    of live registered reader implementations this fails closed. An ambiguous
    POST never retries here; inspect canonical task by request identity.
    """
    _need(environment == "dev", "noteri42_dev_only")
    readers = (source_reader, todo_reader, approval_reader,
               cost_reader, host_reader)
    _need(all(callable(v) for v in readers)
          and callable(getattr(pool, "list_repositories", None))
          and callable(getattr(pool, "enqueue_fenced_noteri42", None)),
          "noteri42_authority_reader_missing")

    def confirm() -> None:
        _source(source_reader(REPO, ISSUE))
        _todo(todo_reader(TODO_ID))
        _approval(approval_reader(REPO, ISSUE))
        _cost(cost_reader())
        _host(host_reader())

    confirm()
    _lane(pool)
    confirm()
    payload = {
        "repository": REPO,
        "issue_number": ISSUE,
        "request_id": REQUEST_ID,
        "correlation_id": CORRELATION_ID,
        "priority": 10,
        "base_sha": SOURCE_SHA,
        "target_branch": BRANCH,
        "max_attempts": 1,
        "fencing_required": True,
    }
    response = pool.enqueue_fenced_noteri42(payload)
    _need(type(response) is dict
          and type(response.get("created")) is bool
          and type(response.get("task_id")) is str
          and re.fullmatch(r"cwp-[0-9a-f]{24}", response["task_id"])
          and type(response.get("task")) is dict,
          "noteri42_enqueue_readback_missing")
    task = response["task"]
    for key in ("repository", "issue_number", "request_id",
                "correlation_id", "priority", "base_sha"):
        _need(task.get(key) == payload[key], "noteri42_enqueue_readback_mismatch")
    _need(task.get("branch") == BRANCH
          and task.get("fencing_required") is True
          and task.get("max_attempts") == 1
          and task.get("state") in ("queued", "leased", "running",
                                    "validating", "completed", "blocked", "failed"),
          "noteri42_enqueue_readback_mismatch")
    return {
        "result": "NOTERI42_FENCED_ENQUEUED",
        "task_id": response["task_id"],
        "created": response["created"],
        "repository": REPO,
        "source_sha": SOURCE_SHA,
        "todo_id": TODO_ID,
        "ready_for_pr": False,
        "model_executed": False,
        "cas_materialized": False,
    }
