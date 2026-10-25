"""Issue-bound Qwen7B handoff to a draft PR, with no host or API defaults.

Only a trusted GitHub adapter may supply live issue/branch/comment responses and
perform writes. The model cannot choose repository, paths, commands or PR base.
The separately governed runner must supply an independent regression verifier.
This module does NOT start Ollama, register workers, merge or dispatch work.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol

from app.structured_code_worker import extracted, render, sha256, validate
from scripts.structured_acceptance import INSTRUCTION, cases

REPO = "ericson-j-santos/engineering-worker-pool"
OWNER = "ericson-j-santos"
PROFILE = "bridge.property.v1"
PATH = "app/portfolio_bridge.py"
FUNCTION = "_property"
MODEL = "qwen2.5-coder:7b"
MODEL_DIGEST_PREFIX = "dae161e27b0e"
SHA = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MARKER = "local-qwen7b-issue-draft-v1"


class HandoffBlocked(RuntimeError):
    """Fixed, sanitized rejection reason; never embed provider input."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise HandoffBlocked(reason)


def observed_date(raw: str) -> datetime:
    require(type(raw) is str, "observation_timestamp_invalid")
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise HandoffBlocked("observation_timestamp_invalid") from None
    require(result.tzinfo is not None, "observation_timezone_required")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class IssueBinding:
    number: int
    base_sha: str
    comment_id: int
    request_id: str
    correlation_id: str
    branch: str


def admit(issue: Mapping[str, Any], comment: Mapping[str, Any],
          branch: Mapping[str, Any], *, base_sha: str,
          observed_at: str, now: datetime) -> IssueBinding:
    """Admit ONLY owner-approved, fresh GitHub snapshots for a fixed repair.

    Caller must fetch all three resources from authenticated GitHub API; no
    user-authored JSON or untrusted workflow event may be passed as a snapshot.
    """
    require(type(issue) is dict and type(comment) is dict and type(branch) is dict,
            "untrusted_snapshot_shape")
    require(type(now) is datetime and now.tzinfo is not None, "now_timezone_required")
    require(type(base_sha) is str and SHA.fullmatch(base_sha) is not None,
            "base_sha_invalid")
    timestamp = observed_date(observed_at)
    age = now.astimezone(timezone.utc) - timestamp
    require(timedelta(0) <= age <= timedelta(minutes=5),
            "github_observation_stale_or_future")
    number = issue.get("number")
    require(type(number) is int and number > 0 and issue.get("state") == "open"
            and not issue.get("pull_request")
            and issue.get("html_url") == f"https://github.com/{REPO}/issues/{number}"
            and issue.get("repository_url") == f"https://api.github.com/repos/{REPO}",
            "issue_identity_invalid")
    require(branch.get("name") == "main"
            and type(branch.get("commit")) is dict
            and branch["commit"].get("sha") == base_sha,
            "main_head_drift")
    comment_id = comment.get("id")
    user = comment.get("user")
    require(type(comment_id) is int and comment_id > 0
            and type(user) is dict and user.get("login") == OWNER
            and user.get("type") == "User"
            and comment.get("author_association") == "OWNER"
            and comment.get("issue_url") == f"https://api.github.com/repos/{REPO}/issues/{number}"
            and comment.get("body") ==
            f"/local-agent authorize {PROFILE} sha:{base_sha}",
            "owner_authorization_missing")
    identity = f"{REPO}#{number}:{PROFILE}:{base_sha}"
    request_id = "issue-" + hashlib.sha256(identity.encode()).hexdigest()
    correlation = f"qwen7b-issue-{number}-{base_sha[:12]}"
    return IssueBinding(
        number, base_sha, comment_id, request_id, correlation,
        f"agent/qwen7b-issue-{number}-{base_sha[:12]}",
    )


def trusted_task(binding: IssueBinding, before: bytes) -> dict[str, Any]:
    """Use versioned local acceptance cases, never instructions from an issue."""
    require(type(before) is bytes and 1 <= len(before) <= 262144,
            "source_bytes_invalid")
    return {
        "task_id": binding.correlation_id,
        "path": PATH, "function": FUNCTION, "parameters": ["properties", "name"],
        "instruction": INSTRUCTION, "base_sha": binding.base_sha,
        "before_sha256": sha256(before), "cases": cases(),
    }


def verify_patch(binding: IssueBinding, before: bytes, after: bytes,
                 *, model: str, model_digest: str, model_calls: int,
                 produced_sha: str, regression_checker: Callable[[bytes, str], Mapping[str, Any]]
                 ) -> dict[str, Any]:
    """Validate the exact model artifact and invoke a separate checkout verifier.

    The trusted caller MUST implement regression_checker with real subprocess
    pytest in an independently materialized, governed checkout; receipt fields
    alone are not an attestation of physical independence.
    """
    require(model == MODEL and type(model_digest) is str
            and HEX64.fullmatch(model_digest) is not None
            and model_digest.startswith(MODEL_DIGEST_PREFIX)
            and type(model_calls) is int and model_calls in (1, 2)
            and type(produced_sha) is str and SHA.fullmatch(produced_sha) is not None
            and produced_sha != binding.base_sha, "model_identity_unproved")
    require(type(after) is bytes and len(after) <= 262144 and before != after,
            "candidate_bytes_invalid")
    task = trusted_task(binding, before)
    try:
        original = before.decode("utf-8")
        candidate = after.decode("utf-8")
    except UnicodeError:
        raise HandoffBlocked("source_encoding_invalid") from None
    # A model may replace ONLY the reviewed function, never imports/tests.
    function = extracted(candidate, task)
    reconstructed = render(
        original, task, json.dumps({"function": function}, ensure_ascii=False),
    )
    require(candidate == reconstructed, "candidate_scope_changed")
    baseline = validate(original, task)
    report = validate(candidate, task)
    require(baseline["passed"] < baseline["total"] == 42
            and report == {"passed": 42, "total": 42, "errors": 0, "failures": []},
            "bounded_acceptance_failed")
    observed = regression_checker(after, produced_sha)
    require(type(observed) is dict
            and observed.get("independent_checkout") is True
            and observed.get("source_sha256") == sha256(after)
            and observed.get("produced_sha") == produced_sha
            and type(observed.get("passed")) is int and observed["passed"] >= 52
            and observed.get("failed") == 0 and observed.get("errors") == 0
            and observed.get("collected") >= observed["passed"],
            "independent_regression_unproved")
    return {
        "model": model, "model_digest": model_digest, "model_calls": model_calls,
        "base_sha": binding.base_sha, "produced_sha": produced_sha,
        "source_sha256": sha256(after), "baseline_passed": baseline["passed"],
        "accepted_cases": 42, "regressions_passed": observed["passed"],
        "request_id": binding.request_id,
    }


class GitHubDraftPort(Protocol):
    """Trusted, authenticated official GitHub API adapter (not issue content)."""
    def get_branch(self, branch: str) -> Mapping[str, Any] | None: ...
    def get_source(self, ref: str) -> tuple[bytes, str]: ...
    def changed_paths(self, base_sha: str, head_sha: str) -> tuple[str, ...]: ...
    def create_branch(self, name: str, sha: str) -> None: ...
    def replace_source(self, name: str, content: bytes, blob_sha: str) -> None: ...
    def find_pr(self, head: str) -> Mapping[str, Any] | None: ...
    def create_pr(self, head: str, title: str, body: str) -> None: ...
    def get_issue(self, number: int) -> Mapping[str, Any]: ...
    def get_comment(self, comment_id: int) -> Mapping[str, Any]: ...


def publish_draft(port: GitHubDraftPort, binding: IssueBinding,
                  before: bytes, after: bytes, audit: Mapping[str, Any],
                  *, now: datetime, observed_at: str,
                  regression_checker: Callable[[bytes, str], Mapping[str, Any]]
                  ) -> dict[str, Any]:
    """Idempotent branch + draft PR; no push, merge, deploy or force-update.

    On uncertain HTTP side effects the adapter must raise and stop. A retry is
    allowed only after new GitHub readback reconciles the existing branch/PR.
    """
    branch = port.get_branch("main")
    require(branch is not None, "main_unavailable")
    checked = admit(port.get_issue(binding.number), port.get_comment(binding.comment_id),
                    branch, base_sha=binding.base_sha, observed_at=observed_at, now=now)
    require(checked == binding, "admission_revalidated_different")
    actual_source, source_blob = port.get_source(binding.base_sha)
    require(actual_source == before and type(source_blob) is str
            and SHA.fullmatch(source_blob) is not None, "base_source_changed")
    require(type(audit) is dict, "audit_binding_invalid")
    checked_audit = verify_patch(
        binding, before, after,
        model=audit.get("model"), model_digest=audit.get("model_digest"),
        model_calls=audit.get("model_calls"),
        produced_sha=audit.get("produced_sha"),
        regression_checker=regression_checker,
    )
    require(audit == checked_audit, "audit_binding_invalid")
    existing = port.get_branch(binding.branch)
    if existing is None:
        port.create_branch(binding.branch, binding.base_sha)
        existing = port.get_branch(binding.branch)
    require(existing is not None and type(existing.get("commit")) is dict,
            "draft_branch_unreadable")
    head = existing["commit"].get("sha")
    remote, _ = port.get_source(binding.branch)
    if head == binding.base_sha and remote == before:
        port.replace_source(binding.branch, after, source_blob)
    else:
        require(type(head) is str and SHA.fullmatch(head) is not None
                and head != binding.base_sha and remote == after,
                "branch_collision_or_divergence")
    final_branch = port.get_branch(binding.branch)
    final_source, _ = port.get_source(binding.branch)
    require(final_branch is not None and type(final_branch.get("commit")) is dict
            and type(final_branch["commit"].get("sha")) is str
            and SHA.fullmatch(final_branch["commit"]["sha"]) is not None
            and final_branch["commit"]["sha"] != binding.base_sha
            and final_source == after, "branch_readback_failed")
    head = final_branch["commit"]["sha"]
    require(port.changed_paths(binding.base_sha, head) == (PATH,),
            "draft_branch_diff_scope_invalid")
    body = (
        f"Closes #{binding.number} only after independent review.\n\n"
        f"Marker: {MARKER}\n"
        f"Authorization comment: {binding.comment_id}\n"
        f"Base SHA: {binding.base_sha}\n"
        f"Builder SHA: {audit['produced_sha']}\n"
        f"Model: {MODEL} (digest {audit['model_digest']})\n"
        f"Validation: 42/42 cases; {audit['regressions_passed']} regressions.\n"
        f"Source SHA-256: {audit['source_sha256']}\n"
        f"Request: {binding.request_id}\n"
        "Draft only. No automatic merge, deployment or product cutover.\n"
    )
    pr = port.find_pr(binding.branch)
    created = False
    if pr is None:
        port.create_pr(binding.branch, f"fix: issue #{binding.number} via local Qwen7B", body)
        created = True
        pr = port.find_pr(binding.branch)
    require(type(pr) is dict and type(pr.get("number")) is int
            and pr.get("state") == "open" and pr.get("draft") is True
            and pr.get("head_sha") == head and pr.get("base_ref") == "main"
            and pr.get("body") == body, "draft_pr_readback_failed")
    return {"result": "DRAFT_PR_VERIFIED", "issue": binding.number,
            "pr_number": pr["number"], "head_sha": head,
            "replayed": not created,
            "request_id": binding.request_id, "model": MODEL,
            "merge_performed": False}
