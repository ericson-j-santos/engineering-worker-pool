"""Fail-closed selection of the next governed Worker Pool stage.

This module only turns an authenticated task/checkpoint readback into a bounded
stage identifier.  It performs no dispatch, claim, GitHub write, or host action.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
TASK_ID = re.compile(r"cwp-[0-9a-f]{24}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
SHA40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")


class ContinuationBlocked(RuntimeError):
    """The readback cannot safely select a continuation stage."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ContinuationBlocked(reason)


@dataclass(frozen=True)
class ContinuationBinding:
    """Trusted identity supplied by the control plane, never by a model."""

    task_id: str
    repository: str
    issue_number: int
    request_id: str
    idempotency_key: str
    todo_id: str
    base_sha: str
    rules_sha: str
    correlation_id: str

    def validate(self) -> None:
        _require(bool(TASK_ID.fullmatch(self.task_id)), "continuation_binding_invalid")
        _require(bool(REPOSITORY.fullmatch(self.repository)), "continuation_binding_invalid")
        _require(type(self.issue_number) is int and self.issue_number >= 1,
                 "continuation_binding_invalid")
        _require(all(type(value) is str and bool(SAFE_ID.fullmatch(value)) for value in (
            self.request_id, self.todo_id, self.correlation_id,
        )), "continuation_binding_invalid")
        _require(bool(HEX64.fullmatch(self.idempotency_key)),
                 "continuation_binding_invalid")
        _require(bool(SHA40.fullmatch(self.base_sha))
                 and bool(SHA40.fullmatch(self.rules_sha)),
                 "continuation_binding_invalid")


@dataclass(frozen=True)
class SelectedContinuation:
    action: str
    task_id: str
    repository: str
    issue_number: int
    request_id: str
    idempotency_key: str
    todo_id: str
    base_sha: str
    rules_sha: str
    correlation_id: str
    lease_generation: int
    attempt_id: str
    checkpoint_id: str


_TRANSITIONS = {
    ("running", "builder_result", "submit_validation"): "submit_validation",
    ("validating", "validator_result", "complete"): "complete",
}


def select_continuation(
    task: Mapping[str, Any], checkpoint: Mapping[str, Any],
    binding: ContinuationBinding,
) -> SelectedContinuation:
    """Select one predefined stage from authenticated canonical readback.

    The caller remains responsible for authenticating both reads and for
    dispatch authorization.  Returned data is inert and cannot encode a shell
    command or arbitrary workflow input.
    """
    binding.validate()
    _require(type(task) is dict and type(checkpoint) is dict,
             "continuation_readback_invalid")
    expected_task = {
        "task_id": binding.task_id,
        "repository": binding.repository,
        "issue_number": binding.issue_number,
        "request_id": binding.request_id,
        "idempotency_key": binding.idempotency_key,
        "base_sha": binding.base_sha,
    }
    _require(all(task.get(key) == value for key, value in expected_task.items()),
             "continuation_task_identity_drift")
    fencing = task.get("fencing_required")
    _require(((type(fencing) is bool and fencing is True)
              or (type(fencing) is int and fencing == 1))
             and type(task.get("lease_generation")) is int
             and not isinstance(task.get("lease_generation"), bool)
             and task["lease_generation"] >= 1
             and type(task.get("attempt_id")) is str
             and bool(SAFE_ID.fullmatch(task["attempt_id"]))
             and type(task.get("leased_by")) is str
             and bool(SAFE_ID.fullmatch(task["leased_by"])),
             "continuation_active_fence_invalid")
    _require(checkpoint.get("task_id") == binding.task_id
             and type(checkpoint.get("lease_generation")) is int
             and checkpoint.get("lease_generation") == task["lease_generation"]
             and checkpoint.get("attempt_id") == task["attempt_id"]
             and type(checkpoint.get("checkpoint_id")) is str
             and bool(SAFE_ID.fullmatch(checkpoint["checkpoint_id"])),
             "continuation_checkpoint_fence_drift")

    effect_state = checkpoint.get("effect_state")
    _require(effect_state in {"none", "confirmed", "not_applied"},
             "continuation_effect_unresolved")
    if effect_state == "none":
        _require(checkpoint.get("effect_id") is None
                 and checkpoint.get("effect_result_ref") is None,
                 "continuation_effect_invalid")
    else:
        _require(type(checkpoint.get("effect_id")) is str
                 and bool(SAFE_ID.fullmatch(checkpoint["effect_id"]))
                 and type(checkpoint.get("effect_result_ref")) is str
                 and 1 <= len(checkpoint["effect_result_ref"]) <= 2048,
                 "continuation_effect_invalid")

    transition = (
        task.get("state"), checkpoint.get("stage"), checkpoint.get("next_action"),
    )
    action = _TRANSITIONS.get(transition)
    _require(action is not None, "continuation_transition_not_allowed")
    if action == "submit_validation":
        _require(task.get("builder_worker_id") == task["leased_by"],
                 "continuation_builder_lease_invalid")
        _require(effect_state == "confirmed"
                 and type(checkpoint.get("artifact_ref")) is str
                 and 1 <= len(checkpoint["artifact_ref"]) <= 2048
                 and type(checkpoint.get("artifact_sha256")) is str
                 and bool(HEX64.fullmatch(checkpoint["artifact_sha256"])),
                 "continuation_builder_artifact_unproved")
    else:
        _require(task.get("validator_worker_id") == task["leased_by"]
                 and type(task.get("builder_worker_id")) is str
                 and task["builder_worker_id"] != task["validator_worker_id"],
                 "continuation_validator_independence_unproved")
        _require(type(task.get("produced_sha")) is str
                 and bool(SHA40.fullmatch(task["produced_sha"])),
                 "continuation_validator_result_unproved")

    return SelectedContinuation(
        action=action, task_id=binding.task_id, repository=binding.repository,
        issue_number=binding.issue_number, request_id=binding.request_id,
        idempotency_key=binding.idempotency_key, todo_id=binding.todo_id,
        base_sha=binding.base_sha, rules_sha=binding.rules_sha,
        correlation_id=binding.correlation_id,
        lease_generation=task["lease_generation"], attempt_id=task["attempt_id"],
        checkpoint_id=checkpoint["checkpoint_id"],
    )
