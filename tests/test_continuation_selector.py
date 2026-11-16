from __future__ import annotations

import pytest

from app.continuation_selector import (
    ContinuationBinding, ContinuationBlocked, select_continuation,
)


BASE = "a" * 40
RULES = "b" * 40
PRODUCED = "c" * 40


def binding() -> ContinuationBinding:
    return ContinuationBinding(
        task_id="cwp-0123456789abcdef01234567",
        repository="ericson-j-santos/engineering-worker-pool",
        issue_number=62,
        request_id="worker-pool-62-next-action-v1",
        idempotency_key="d" * 64,
        todo_id="TODO-62",
        base_sha=BASE,
        rules_sha=RULES,
        correlation_id="worker-pool-62-continuation-v1",
    )


def task(state: str = "running") -> dict:
    item = {
        "task_id": binding().task_id,
        "repository": binding().repository,
        "issue_number": 62,
        "request_id": binding().request_id,
        "idempotency_key": binding().idempotency_key,
        "base_sha": BASE,
        "state": state,
        "fencing_required": True,
        "lease_generation": 3,
        "attempt_id": "attempt-3",
        "leased_by": "builder-1" if state == "running" else "validator-1",
        "builder_worker_id": "builder-1",
        "validator_worker_id": None if state == "running" else "validator-1",
        "produced_sha": None if state == "running" else PRODUCED,
    }
    return item


def checkpoint(stage: str = "builder_result",
               action: str = "submit_validation") -> dict:
    return {
        "task_id": binding().task_id,
        "lease_generation": 3,
        "attempt_id": "attempt-3",
        "checkpoint_id": "checkpoint-3",
        "stage": stage,
        "next_action": action,
        "artifact_ref": "cas://worker-pool/fixture",
        "artifact_sha256": "e" * 64,
        "effect_id": "artifact-publish-3",
        "effect_state": "confirmed",
        "effect_result_ref": "cas-readback://worker-pool/fixture",
    }


def test_selects_only_predefined_builder_handoff() -> None:
    selected = select_continuation(task(), checkpoint(), binding())
    assert selected.action == "submit_validation"
    assert selected.todo_id == "TODO-62"
    assert selected.rules_sha == RULES
    assert selected.lease_generation == 3


def test_selects_predefined_validator_completion() -> None:
    observed = checkpoint("validator_result", "complete")
    observed.update({"effect_id": None, "effect_state": "none",
                     "effect_result_ref": None})
    selected = select_continuation(task("validating"), observed, binding())
    assert selected.action == "complete"


@pytest.mark.parametrize("state", ["pending", "unknown"])
def test_rejects_unresolved_effect(state: str) -> None:
    observed = checkpoint()
    observed.update({"effect_state": state, "effect_result_ref": None})
    with pytest.raises(ContinuationBlocked, match="continuation_effect_unresolved"):
        select_continuation(task(), observed, binding())


@pytest.mark.parametrize("action", [
    "run powershell.exe", "workflow_dispatch", "submit_validation --force", "continue",
])
def test_rejects_free_text_or_unlisted_action(action: str) -> None:
    with pytest.raises(ContinuationBlocked, match="continuation_transition_not_allowed"):
        select_continuation(task(), checkpoint(action=action), binding())


def test_rejects_stale_or_concurrent_attempt() -> None:
    observed = checkpoint()
    observed["attempt_id"] = "attempt-2"
    with pytest.raises(ContinuationBlocked, match="continuation_checkpoint_fence_drift"):
        select_continuation(task(), observed, binding())


def test_rejects_non_boolean_fencing_value() -> None:
    observed = task()
    observed["fencing_required"] = 1.0
    with pytest.raises(ContinuationBlocked, match="continuation_active_fence_invalid"):
        select_continuation(observed, checkpoint(), binding())

@pytest.mark.parametrize("invalid_generation", [True, 1.0, "1", None])
def test_rejects_checkpoint_generation_with_wrong_type(invalid_generation: object) -> None:
    observed_task = task()
    observed_task["lease_generation"] = 1
    observed_checkpoint = checkpoint()
    observed_checkpoint["lease_generation"] = invalid_generation
    with pytest.raises(ContinuationBlocked, match="continuation_checkpoint_fence_drift"):
        select_continuation(observed_task, observed_checkpoint, binding())


def test_accepts_valid_integer_checkpoint_generation_one() -> None:
    observed_task = task()
    observed_task["lease_generation"] = 1
    observed_checkpoint = checkpoint()
    observed_checkpoint["lease_generation"] = 1
    assert select_continuation(
        observed_task, observed_checkpoint, binding(),
    ).lease_generation == 1


@pytest.mark.parametrize("field,value", [
    ("idempotency_key", "f" * 64),
    ("base_sha", "f" * 40),
    ("issue_number", 63),
])
def test_rejects_identity_drift(field: str, value: object) -> None:
    observed = task()
    observed[field] = value
    with pytest.raises(ContinuationBlocked, match="continuation_task_identity_drift"):
        select_continuation(observed, checkpoint(), binding())


@pytest.mark.parametrize("state", ["queued", "blocked", "completed", "failed"])
def test_rejects_replay_from_non_active_or_terminal_state(state: str) -> None:
    with pytest.raises(ContinuationBlocked, match="continuation_transition_not_allowed"):
        select_continuation(task(state), checkpoint(), binding())


def test_rejects_builder_handoff_without_confirmed_artifact() -> None:
    observed = checkpoint()
    observed["artifact_sha256"] = None
    with pytest.raises(ContinuationBlocked, match="continuation_builder_artifact_unproved"):
        select_continuation(task(), observed, binding())


def test_rejects_builder_checkpoint_owned_by_another_worker() -> None:
    observed = task()
    observed["leased_by"] = "builder-2"
    with pytest.raises(ContinuationBlocked, match="continuation_builder_lease_invalid"):
        select_continuation(observed, checkpoint(), binding())


def test_rejects_validator_that_is_not_independent() -> None:
    observed = task("validating")
    observed["builder_worker_id"] = observed["validator_worker_id"]
    check = checkpoint("validator_result", "complete")
    check.update({"effect_id": None, "effect_state": "none",
                  "effect_result_ref": None})
    with pytest.raises(
        ContinuationBlocked, match="continuation_validator_independence_unproved",
    ):
        select_continuation(observed, check, binding())


@pytest.mark.parametrize("invalid_effect", [[], {}])
def test_rejects_malformed_effect_state_without_crashing(invalid_effect: object) -> None:
    observed = checkpoint()
    observed["effect_state"] = invalid_effect
    with pytest.raises(ContinuationBlocked, match="continuation_effect_unresolved"):
        select_continuation(task(), observed, binding())


@pytest.mark.parametrize("field", ["state", "stage", "next_action"])
def test_rejects_unhashable_transition_value_without_crashing(field: str) -> None:
    observed_task = task()
    observed_checkpoint = checkpoint()
    if field == "state":
        observed_task["state"] = []
    else:
        observed_checkpoint[field] = []
    with pytest.raises(ContinuationBlocked, match="continuation_transition_not_allowed"):
        select_continuation(observed_task, observed_checkpoint, binding())


@pytest.mark.parametrize("invalid_builder", ["", " ", None])
def test_rejects_invalid_builder_identity_during_validation(invalid_builder: object) -> None:
    observed_task = task("validating")
    observed_task["builder_worker_id"] = invalid_builder
    observed_checkpoint = checkpoint("validator_result", "complete")
    observed_checkpoint.update({
        "effect_id": None, "effect_state": "none", "effect_result_ref": None,
    })
    with pytest.raises(ContinuationBlocked, match="continuation_validator_independence_unproved"):
        select_continuation(observed_task, observed_checkpoint, binding())
