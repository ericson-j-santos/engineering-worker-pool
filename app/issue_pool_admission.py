"""Admit an owner-authorized GitHub issue to the *existing* fenced Worker Pool.

This module does not register workers, select arbitrary tasks, invoke Ollama,
create services, or publish a PR. The physical Builder and independent Validator
must claim the admitted task via the governed exact-fenced lease API.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from app.issue_draft_pr import (
    GitHubDraftPort, HandoffBlocked, IssueBinding, REPO, admit, require,
    trusted_task,
)
from app.structured_code_worker import validate


class FencedIssuePool(Protocol):
    def list_repositories(self) -> list[dict[str, Any]]: ...
    def enqueue_fenced_issue(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def _read_binding(github: GitHubDraftPort, issue_number: int, comment_id: int,
                  clock: datetime) -> IssueBinding:
    """Only reads from an authenticated, allowlisted GitHub transport."""
    branch = github.get_branch("main")
    require(type(branch) is dict and type(branch.get("commit")) is dict,
            "issue_main_unavailable")
    base_sha = branch["commit"].get("sha")
    require(type(base_sha) is str, "issue_main_invalid")
    issue = github.get_issue(issue_number)
    comment = github.get_comment(comment_id)
    return admit(
        issue, comment, branch, base_sha=base_sha,
        observed_at=clock.isoformat(), now=clock,
    )


def admit_to_existing_pool(*, github: GitHubDraftPort, pool: FencedIssuePool,
                           issue_number: int, comment_id: int,
                           environment: str, now: datetime | None = None
                           ) -> dict[str, Any]:
    """Issue -> genuine failing baseline -> DEV lane -> exact fenced task.

    The caller must provide authenticated GitHub and loopback worker transports;
    neither issue body nor command text controls paths, model, target or shell.
    This is **enqueue only**, not proof of physical execution or completion.
    """
    require(environment == "dev", "issue_dispatch_dev_only")
    require(type(issue_number) is int and issue_number > 0
            and type(comment_id) is int and comment_id > 0,
            "issue_dispatch_identity_invalid")
    clock = now or datetime.now(timezone.utc)
    require(clock.tzinfo is not None, "issue_dispatch_clock_invalid")

    binding = _read_binding(github, issue_number, comment_id, clock)
    source, _blob_sha = github.get_source(binding.base_sha)
    task = trusted_task(binding, source)
    try:
        result = validate(source.decode("utf-8"), task)
    except UnicodeError:
        raise HandoffBlocked("issue_source_encoding_invalid") from None
    require(result["total"] == 42 and result["passed"] < 42,
            "issue_no_failing_baseline")

    lanes = pool.list_repositories()
    require(type(lanes) is list and len(lanes) <= 128
            and all(type(item) is dict for item in lanes),
            "issue_lanes_invalid")
    target = [lane for lane in lanes if lane.get("repository") == REPO]
    require(len(target) == 1 and target[0].get("enabled") is True
            and type(target[0].get("max_in_flight")) is int
            and target[0]["max_in_flight"] == 1,
            "issue_fenced_lane_not_ready")

    # The final authenticated GitHub read follows baseline and lane checks.
    # No changes are allowed between admission and durable queue submission.
    current = _read_binding(github, issue_number, comment_id, clock)
    require(current == binding, "issue_admission_changed_before_enqueue")

    payload = {
        "repository": REPO,
        "issue_number": binding.number,
        "request_id": binding.request_id,
        "correlation_id": binding.correlation_id,
        "priority": 10,
        "base_sha": binding.base_sha,
        "target_branch": binding.branch,
        "max_attempts": 1,
        "fencing_required": True,
    }
    admitted = pool.enqueue_fenced_issue(payload)
    require(type(admitted) is dict and type(admitted.get("task")) is dict
            and type(admitted.get("task_id")) is str
            and admitted["task"].get("task_id") == admitted["task_id"]
            and type(admitted.get("created")) is bool,
            "issue_pool_readback_invalid")
    for key in ("repository", "issue_number", "request_id",
                "correlation_id", "base_sha"):
        require(admitted["task"].get(key) == payload[key],
                "issue_pool_readback_invalid")
    require(admitted["task"].get("branch") == binding.branch
            and admitted["task"].get("fencing_required") is True
            and admitted["task"].get("max_attempts") == 1,
            "issue_pool_readback_invalid")
    return {
        "result": "ISSUE_FENCED_ENQUEUED",
        "task_id": admitted["task_id"],
        "created": admitted["created"],
        "issue_number": binding.number,
        "owner_comment_id": binding.comment_id,
        "base_sha": binding.base_sha,
        "target_branch": binding.branch,
        "request_id": binding.request_id,
        "correlation_id": binding.correlation_id,
        "baseline_passed": result["passed"],
        "baseline_total": 42,
        "fencing_required": True,
        "builder_started": False,
        "validator_completed": False,
        "draft_pr_created": False,
    }
