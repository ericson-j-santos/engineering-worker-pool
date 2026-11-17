"""Consume canonical Worker Pool next_action without treating it as a command.

This library is called by a trusted, already-attested native entrypoint. The
Worker Pool remains the only task/lease authority. The injected handlers must
use NativeOneShotExecutor (including its exact fenced claim and Gateway).
No issue text, model output, or document may select a handler or a command.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from app.native_one_shot import NativeExecutorBlocked

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_TASK = re.compile(r"cwp-[0-9a-f]{24}\Z")


class CanonicalReads(Protocol):
    def get_task(self, task_id: str) -> dict[str, Any]: ...
    def get_latest_checkpoint(self, task_id: str) -> dict[str, Any] | None: ...


def _need(value: bool, reason: str) -> None:
    if not value:
        raise NativeExecutorBlocked(reason)


def _read(api: CanonicalReads, task_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    task = api.get_task(task_id)
    checkpoint = api.get_latest_checkpoint(task_id)
    _need(type(task) is dict and (checkpoint is None or type(checkpoint) is dict),
          "continuation_readback_invalid")
    return task, checkpoint


def _checkpoint_ok(cp: dict[str, Any], task_id: str, generation: int,
                   stage: str, next_action: str, effect: str) -> bool:
    return (
        cp.get("task_id") == task_id
        and cp.get("stage") == stage
        and cp.get("next_action") == next_action
        and cp.get("effect_state") == effect
        and type(cp.get("lease_generation")) is int
        and 1 <= cp["lease_generation"] <= generation
        and type(cp.get("sequence")) is int
        and cp["sequence"] >= 1
    )


def decide_next_action(
    task: dict[str, Any], checkpoint: dict[str, Any] | None, *,
    task_id: str, expected_repository: str, expected_issue: int,
    expected_request_id: str, expected_base_sha: str,
) -> str:
    """Return one of builder/validator/wait/done/blocked, never a command."""
    _need(
        type(task_id) is str and bool(_TASK.fullmatch(task_id))
        and type(expected_repository) is str
        and bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", expected_repository))
        and type(expected_issue) is int and expected_issue > 0
        and type(expected_request_id) is str and 1 <= len(expected_request_id) <= 128
        and type(expected_base_sha) is str and bool(_SHA.fullmatch(expected_base_sha)),
        "continuation_binding_invalid",
    )
    _need(
        type(task) is dict and task.get("task_id") == task_id
        and task.get("repository") == expected_repository
        and task.get("issue_number") == expected_issue
        and task.get("request_id") == expected_request_id
        and task.get("base_sha") == expected_base_sha
        and task.get("fencing_required") is True,
        "continuation_authority_drift",
    )
    generation = task.get("lease_generation")
    state = task.get("state")
    _need(type(generation) is int and generation >= 0, "continuation_generation_invalid")
    _need(state in {"queued", "leased", "running", "validating", "completed",
                    "blocked", "failed"}, "continuation_state_invalid")
    if checkpoint is not None:
        _need(type(checkpoint) is dict and checkpoint.get("task_id") == task_id,
              "continuation_checkpoint_invalid")
        # An unresolved effect can never be retried automatically.
        _need(checkpoint.get("effect_state") not in {"pending", "unknown"},
              "continuation_effect_unresolved")

    if state in {"blocked", "failed"}:
        return "blocked"
    if state in {"leased", "running"} or task.get("leased_by") is not None:
        return "wait"
    if state == "queued":
        if checkpoint is None:
            return "builder"
        _need(
            _checkpoint_ok(checkpoint, task_id, generation,
                           "builder_result", "submit_validation", "confirmed"),
            "continuation_unexpected_checkpoint",
        )
        return "builder"  # recovery is decided by the exact-fenced claim
    if state == "validating":
        _need(
            checkpoint is not None
            and _checkpoint_ok(checkpoint, task_id, generation,
                               "builder_result", "submit_validation", "confirmed")
            and type(checkpoint.get("artifact_sha256")) is str
            and bool(re.fullmatch(r"[0-9a-f]{64}", checkpoint["artifact_sha256"]))
            and type(task.get("produced_sha")) is str
            and bool(_SHA.fullmatch(task["produced_sha"])),
            "continuation_validator_evidence_missing",
        )
        return "validator"
    _need(
        checkpoint is not None
        and _checkpoint_ok(checkpoint, task_id, generation,
                           "validator_result", "complete", "none")
        and type(task.get("produced_sha")) is str
        and bool(_SHA.fullmatch(task["produced_sha"])),
        "continuation_completion_unproved",
    )
    return "done"


def _verify_native_receipt(receipt: Any, *, role: str, task_id: str,
                           task: dict[str, Any]) -> None:
    """Reject 2xx, None and unrelated handler output before claiming progress.

    This checks the installed NativeOneShotExecutor v2 receipt against canonical
    Worker Pool readback. It does NOT attest actual LLM model/digest, physical
    host identity or the underlying provenance of an injected handler.
    """
    expected = ("NATIVE_ONE_SHOT_BUILT" if role == "builder"
                else "NATIVE_ONE_SHOT_VALIDATED")
    worker = task.get(f"{role}_worker_id")
    _need(
        type(receipt) is dict
        and receipt.get("schema_version") == 2
        and receipt.get("result") == expected
        and receipt.get("task_id") == task_id
        and receipt.get("role") == role
        and type(worker) is str and len(worker) > 0
        and receipt.get("worker_id") == worker
        and receipt.get("gateway_verified") is True
        and receipt.get("replayed") is False
        and receipt.get("fencing_required") is True
        and type(receipt.get("lease_generation")) is int
        and receipt["lease_generation"] > 0
        and receipt["lease_generation"] == task.get("lease_generation")
        and type(receipt.get("attempt_id")) is str
        and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
                              receipt["attempt_id"]))
        and type(receipt.get("session_id")) is str
        and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
                              receipt["session_id"]))
        and type(receipt.get("evidence_sha256")) is str
        and bool(re.fullmatch(r"[0-9a-f]{64}", receipt["evidence_sha256"]))
        and receipt.get("produced_sha") == task.get("produced_sha")
        and type(receipt.get("produced_sha")) is str
        and bool(_SHA.fullmatch(receipt["produced_sha"])),
        "continuation_handler_receipt_unverified",
    )


def continue_once(
    api: CanonicalReads, *, task_id: str, expected_repository: str,
    expected_issue: int, expected_request_id: str, expected_base_sha: str,
    handlers: Mapping[str, Callable[[], Any]],
) -> dict[str, Any]:
    """Route at most one stage through trusted, installed handlers.

    This is NOT a new scheduler or an authorization source. The handler
    independently attests session/Gateway, claims an exact fenced lease and
    performs effect readback. This function independently checks the result.
    """
    _need(type(handlers) is dict and set(handlers) == {"builder", "validator"}
          and all(callable(fn) for fn in handlers.values()),
          "continuation_handlers_invalid")
    kwargs = dict(task_id=task_id, expected_repository=expected_repository,
                  expected_issue=expected_issue,
                  expected_request_id=expected_request_id,
                  expected_base_sha=expected_base_sha)
    before = _read(api, task_id)
    role = decide_next_action(*before, **kwargs)
    if role in {"wait", "blocked", "done"}:
        return {"state": role, "task_id": task_id, "executed": False}

    # A second authenticated read rules out a stale plan before a handler
    # gets any authority. Its own exact claim still enforces concurrency.
    _need(_read(api, task_id) == before, "continuation_snapshot_changed")
    receipt = handlers[role]()
    after = _read(api, task_id)
    _verify_native_receipt(receipt, role=role, task_id=task_id, task=after[0])
    next_role = decide_next_action(*after, **kwargs)
    _need(
        (role == "builder" and next_role == "validator")
        or (role == "validator" and next_role == "done"),
        "continuation_effect_not_confirmed",
    )
    return {"state": next_role, "task_id": task_id,
            "executed": True, "executed_role": role}
