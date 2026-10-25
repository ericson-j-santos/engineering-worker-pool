"""Publish an issue draft only after fenced Builder+Validator and CAS readback.

This is a *policy coordinator*, not a background runner or Git transport.
The caller must inject trusted independent CAS extraction, Git verification and
pytest verification implementations. Fabricated callback results are not an E2E.
"""
from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Callable, Mapping, Protocol

from app.issue_draft_pr import (
    GitHubDraftPort, HandoffBlocked, IssueBinding, PATH, REPO,
    SHA, HEX64, publish_draft, require, sha256,
)


class CompletedPool(Protocol):
    def get_task(self, task_id: str) -> dict[str, Any]: ...
    def get_latest_checkpoint(self, task_id: str) -> dict[str, Any] | None: ...


class CasMaterializer(Protocol):
    def __call__(self, checkpoint: Mapping[str, Any], *,
                 task_id: str, produced_sha: str) -> Mapping[str, Any]: ...


class IndependentGitVerifier(Protocol):
    def __call__(self, source: bytes, *,
                 base_sha: str, produced_sha: str) -> Mapping[str, Any]: ...


def _completed(pool: CompletedPool, binding: IssueBinding,
               task_id: str) -> dict[str, Any]:
    """Canonical read; neither a worker receipt nor a green log is enough."""
    require(type(task_id) is str and bool(re.fullmatch(
        r"cwp-[0-9a-f]{24}", task_id)), "issue_task_id_invalid")
    observed = pool.get_task(task_id)
    require(type(observed) is dict
            and observed.get("task_id") == task_id
            and observed.get("repository") == REPO
            and observed.get("issue_number") == binding.number
            and observed.get("request_id") == binding.request_id
            and observed.get("base_sha") == binding.base_sha
            and observed.get("branch") == binding.branch
            and observed.get("fencing_required") is True
            and observed.get("state") == "completed"
            and observed.get("leased_by") is None
            and type(observed.get("produced_sha")) is str
            and SHA.fullmatch(observed["produced_sha"]) is not None
            and observed["produced_sha"] != binding.base_sha
            and type(observed.get("builder_worker_id")) is str
            and type(observed.get("validator_worker_id")) is str
            and observed["builder_worker_id"] != observed["validator_worker_id"]
            and type(observed.get("attempt_count")) is int
            and observed["attempt_count"] >= 2
            and type(observed.get("lease_generation")) is int
            and observed["lease_generation"] >= 2,
            "issue_completed_pool_unproved")
    return observed


def publish_completed_fenced_issue(
    *, github: GitHubDraftPort, pool: CompletedPool, binding: IssueBinding,
    task_id: str, model_audit: Mapping[str, Any],
    materialize: CasMaterializer, git_verify: IndependentGitVerifier,
    regression_checker: Callable[[bytes, str], Mapping[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    """Verified persisted CAS -> Builder SHA -> independent Validator -> PR draft.

    All three callbacks must be trusted *external implementations*:
    materialize reads immutable bytes from the configured CAS provider and
    independently extracts the product source; git_verify verifies a separately
    materialized checkout/commit/diff; regression_checker executes the real
    suite there. This coordinator only rejects inconsistent receipts and
    delegates GitHub mutations to publish_draft after all guards pass.
    """
    task = _completed(pool, binding, task_id)
    checkpoint = pool.get_latest_checkpoint(task_id)
    require(type(checkpoint) is dict
            and checkpoint.get("task_id") == task_id
            and checkpoint.get("stage") == "validator_result"
            and checkpoint.get("next_action") == "complete"
            and checkpoint.get("lease_generation") == task["lease_generation"]
            and type(checkpoint.get("artifact_ref")) is str
            and 1 <= len(checkpoint["artifact_ref"]) <= 2048
            and type(checkpoint.get("artifact_sha256")) is str
            and HEX64.fullmatch(checkpoint["artifact_sha256"]) is not None
            and type(checkpoint.get("artifact_kind")) is str
            and 1 <= len(checkpoint["artifact_kind"]) <= 128,
            "issue_validator_checkpoint_unproved")
    cas = materialize(checkpoint, task_id=task_id,
                      produced_sha=task["produced_sha"])
    require(type(cas) is dict and type(cas.get("artifact_bytes")) is bytes
            and 1 <= len(cas["artifact_bytes"]) <= 10_000_000
            and sha256(cas["artifact_bytes"]) == checkpoint["artifact_sha256"]
            and type(cas.get("source_bytes")) is bytes
            and 1 <= len(cas["source_bytes"]) <= 262144
            and cas.get("produced_sha") == task["produced_sha"]
            and cas.get("base_sha") == binding.base_sha,
            "issue_cas_materialization_unproved")
    after = cas["source_bytes"]
    verified_git = git_verify(
        after, base_sha=binding.base_sha,
        produced_sha=task["produced_sha"],
    )
    require(type(verified_git) is dict
            and verified_git.get("base_sha") == binding.base_sha
            and verified_git.get("produced_sha") == task["produced_sha"]
            and verified_git.get("source_sha256") == sha256(after)
            and verified_git.get("changed_paths") == [PATH]
            and verified_git.get("checkout_isolated") is True
            and verified_git.get("commit_verified") is True,
            "issue_git_materialization_unproved")

    before, _blob = github.get_source(binding.base_sha)
    require(type(model_audit) is dict
            and model_audit.get("produced_sha") == task["produced_sha"]
            and model_audit.get("base_sha") == binding.base_sha,
            "issue_model_audit_mismatch")
    # publish_draft performs final re-admission and re-runs the real
    # acceptance/independent regression exactly once before any GitHub write.
    # Do not duplicate that expensive verification in this coordinator.
    result = publish_draft(
        github, binding, before, after, dict(model_audit),
        now=now, observed_at=now.isoformat(),
        regression_checker=regression_checker,
    )
    final_task = _completed(pool, binding, task_id)
    require(final_task.get("produced_sha") == task["produced_sha"]
            and final_task.get("lease_generation") == task["lease_generation"],
            "issue_pool_changed_after_publication")
    return {**result, "worker_task_id": task_id,
            "validator_worker_id": task["validator_worker_id"],
            "artifact_sha256": checkpoint["artifact_sha256"],
            "fenced_generation": task["lease_generation"],
            "independent_readback": True}
