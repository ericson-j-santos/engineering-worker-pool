"""CAS + fenced completion -> draft PR policy; all providers synthetic.

Real GitHub API, physical Qwen7B, cross-host CAS and Validator MUST be
verified separately. No fake receipt is accepted as physical proof.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import issue_draft_pr as gate
from app import issue_publish_coordinator as publisher
from test_issue_draft_pr import (
    BASE, NOW, DIGEST, admitted, checker, verified, FakeOfficialGitHub,
)
from app.structured_code_worker import render
from test_structured_code_worker import FIXED


TASK = "cwp-" + "9" * 24
PRODUCED = "c" * 40
BUNDLE = b"immutable-git-bundle-test-bytes"


@pytest.fixture
def completed():
    binding = admitted()
    before = (Path(__file__).resolve().parents[1] / gate.PATH).read_bytes()
    task = gate.trusted_task(binding, before)
    after = render(
        before.decode(), task,
        __import__("json").dumps({"function": FIXED}),
    ).encode()
    audit = verified(binding, before, after)
    github = FakeOfficialGitHub(binding, before)
    db = {
        "task_id": TASK, "repository": gate.REPO,
        "issue_number": binding.number, "request_id": binding.request_id,
        "base_sha": BASE, "branch": binding.branch,
        "fencing_required": True, "state": "completed",
        "leased_by": None, "produced_sha": PRODUCED,
        "builder_worker_id": "builder-x", "validator_worker_id": "validator-y",
        "attempt_count": 2, "lease_generation": 2,
    }
    checkpoint = {
        "task_id": TASK, "stage": "validator_result", "next_action": "complete",
        "lease_generation": 2, "artifact_kind": "git_bundle_s3_cas",
        "artifact_ref": "cas://test/9", "artifact_sha256": gate.sha256(BUNDLE),
    }
    class Pool:
        def __init__(self):
            self.task = db
            self.checkpoint = checkpoint
            self.reads = 0
        def get_task(self, task_id):
            assert task_id == TASK
            self.reads += 1
            return dict(self.task)
        def get_latest_checkpoint(self, task_id):
            assert task_id == TASK
            return dict(self.checkpoint) if self.checkpoint is not None else None
    pool = Pool()

    def cas_read(cp, *, task_id, produced_sha):
        assert cp["artifact_sha256"] == gate.sha256(BUNDLE)
        assert task_id == TASK and produced_sha == PRODUCED
        return {"artifact_bytes": BUNDLE, "source_bytes": after,
                "base_sha": BASE, "produced_sha": PRODUCED}

    def git_verify(source, *, base_sha, produced_sha):
        assert source == after and base_sha == BASE and produced_sha == PRODUCED
        return {"base_sha": base_sha, "produced_sha": produced_sha,
                "source_sha256": gate.sha256(source),
                "changed_paths": [gate.PATH],
                "checkout_isolated": True, "commit_verified": True}

    return {
        "binding": binding, "github": github, "pool": pool,
        "model_audit": audit, "materialize": cas_read,
        "git_verify": git_verify, "regression_checker": checker,
        "now": NOW,
    }


def run(data):
    return publisher.publish_completed_fenced_issue(
        **data, task_id=TASK,
    )


def test_confirmed_worker_cas_independent_validation_publishes_exactly_one_pr(completed):
    first = run(completed)
    assert first["result"] == "DRAFT_PR_VERIFIED"
    assert first["replayed"] is False
    assert first["worker_task_id"] == TASK
    assert first["fenced_generation"] == 2
    assert completed["github"].effects == [
        "create_branch", "replace_source", "create_pr",
    ]
    second = run(completed)
    assert second["replayed"] is True
    assert second["pr_number"] == first["pr_number"]
    assert len(completed["github"].effects) == 3
    assert completed["pool"].reads >= 4


@pytest.mark.parametrize("kind", [
    "queued", "unfenced", "no_validator", "same_worker",
    "bad_produced", "bad_generation", "incomplete_attempts",
    "task_mismatch",
])
def test_never_publish_without_fenced_completed_task(completed, kind):
    pool = completed["pool"]
    if kind == "queued":
        pool.task["state"] = "queued"
    elif kind == "unfenced":
        pool.task["fencing_required"] = False
    elif kind == "no_validator":
        pool.task["validator_worker_id"] = None
    elif kind == "same_worker":
        pool.task["validator_worker_id"] = pool.task["builder_worker_id"]
    elif kind == "bad_produced":
        pool.task["produced_sha"] = BASE
    elif kind == "bad_generation":
        pool.task["lease_generation"] = 1
    elif kind == "incomplete_attempts":
        pool.task["attempt_count"] = 1
    else:
        pool.task["request_id"] = "not-the-same-issue"
    with pytest.raises(gate.HandoffBlocked, match="issue_completed_pool_unproved"):
        run(completed)
    assert completed["github"].effects == []


@pytest.mark.parametrize("kind", [
    "missing", "wrong_stage", "invalid_sha", "stale_generation",
])
def test_validator_checkpoint_required_before_cas_and_github(completed, kind):
    pool = completed["pool"]
    if kind == "missing":
        pool.checkpoint = None
    elif kind == "wrong_stage":
        pool.checkpoint["stage"] = "builder_result"
    elif kind == "invalid_sha":
        pool.checkpoint["artifact_sha256"] = "not-digest"
    else:
        pool.checkpoint["lease_generation"] = 1
    with pytest.raises(gate.HandoffBlocked, match="issue_validator_checkpoint_unproved"):
        run(completed)
    assert completed["github"].effects == []


def test_cas_hash_mismatch_blocks_publication(completed):
    good = completed["materialize"]
    def bad(*args, **kwargs):
        result = good(*args, **kwargs)
        result["artifact_bytes"] = b"tampered"
        return result
    completed["materialize"] = bad
    with pytest.raises(gate.HandoffBlocked, match="issue_cas_materialization_unproved"):
        run(completed)
    assert completed["github"].effects == []


def test_git_checker_must_prove_exact_one_file(completed):
    good = completed["git_verify"]
    def bad(*args, **kwargs):
        result = good(*args, **kwargs)
        result["changed_paths"] = [gate.PATH, ".github/workflows/ci.yml"]
        return result
    completed["git_verify"] = bad
    with pytest.raises(gate.HandoffBlocked, match="issue_git_materialization_unproved"):
        run(completed)
    assert completed["github"].effects == []


def test_wrong_builder_sha_does_not_publish(completed):
    completed["model_audit"] = {
        **completed["model_audit"], "produced_sha": "f" * 40,
    }
    with pytest.raises(gate.HandoffBlocked, match="issue_model_audit_mismatch"):
        run(completed)
    assert completed["github"].effects == []


def test_failed_independent_regression_blocks_before_writes(completed):
    completed["regression_checker"] = lambda *_: {
        "independent_checkout": False,
    }
    with pytest.raises(gate.HandoffBlocked, match="independent_regression_unproved"):
        run(completed)
    assert completed["github"].effects == []


def test_pool_change_after_publication_is_detected(completed):
    original = completed["pool"].get_task
    reads = 0
    def changed(task_id):
        nonlocal reads
        reads += 1
        item = original(task_id)
        if reads == 2:
            item["lease_generation"] += 1
        return item
    completed["pool"].get_task = changed
    with pytest.raises(gate.HandoffBlocked, match="issue_pool_changed_after_publication"):
        run(completed)
    assert completed["github"].effects == [
        "create_branch", "replace_source", "create_pr",
    ]
