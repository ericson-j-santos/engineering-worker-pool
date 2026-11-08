"""Fail-closed, provider-neutral pre-PR gate for the existing Engineering Worker Pool.

This module is a coordinator, not a scheduler, a model, a GitHub token client,
or proof that any physical/hosted execution occurred. Callers MUST inject
authenticated, independently audited provider implementations. Test fakes
exercise policy only and cannot issue an operational READY_FOR_PR.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
IDENT = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{7,191}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
PATH = re.compile(r"(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9_./-]+\Z")
MAX_CAS_BYTES = 20_000_000
RETENTION_DAYS = 30


class PrePRBlocked(RuntimeError):
    """Public error codes never include provider input or secrets."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise PrePRBlocked(code)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False).encode("ascii")
    except (ValueError, TypeError, UnicodeError):
        raise PrePRBlocked("prepr_serialization_invalid") from None


@dataclass(frozen=True)
class PrePRIntent:
    repository: str
    issue_number: int
    todo_id: str
    request_id: str
    correlation_id: str
    idempotency_key: str
    base_ref: str
    base_sha: str
    candidate_ref: str
    candidate_sha: str
    source_sha: str
    allowed_paths: tuple[str, ...]
    task_id: str

    def check(self) -> None:
        require(type(self.repository) is str and REPO.fullmatch(self.repository)
                is not None and type(self.issue_number) is int
                and self.issue_number > 0, "prepr_issue_invalid")
        require(type(self.todo_id) is str and IDENT.fullmatch(self.todo_id)
                is not None, "prepr_todo_invalid")
        require(all(type(v) is str and IDENT.fullmatch(v) is not None for v in
                    (self.request_id, self.correlation_id, self.idempotency_key,
                     self.task_id)), "prepr_ids_invalid")
        require(self.base_ref == "main" and type(self.candidate_ref) is str
                and self.candidate_ref not in ("main", "") and
                not self.candidate_ref.startswith("refs/") and
                re.fullmatch(r"[A-Za-z0-9_./-]{3,160}", self.candidate_ref)
                is not None and ".." not in self.candidate_ref and
                not self.candidate_ref.endswith("/"),
                "prepr_branch_invalid")
        for sha in (self.base_sha, self.candidate_sha, self.source_sha):
            require(type(sha) is str and SHA40.fullmatch(sha) is not None,
                    "prepr_sha_invalid")
        require(self.base_sha != self.candidate_sha,
                "prepr_no_material_commit")
        require(type(self.allowed_paths) is tuple
                and 1 <= len(self.allowed_paths) <= 32
                and tuple(sorted(set(self.allowed_paths))) == self.allowed_paths
                and all(type(p) is str and 1 <= len(p) <= 240
                        and PATH.fullmatch(p) is not None and
                        not p.startswith(".") and
                        all(not part.startswith(".") for part in p.split("/"))
                        and "//" not in p
                        for p in self.allowed_paths),
                "prepr_scope_invalid")


class Authority(Protocol):
    def issue(self, repository: str, number: int) -> Mapping[str, Any]: ...
    def todo(self, todo_id: str) -> Mapping[str, Any]: ...
    def authorization(self, request_id: str) -> Mapping[str, Any]: ...
    def branch_sha(self, repository: str, ref: str) -> str: ...


class ExistingPool(Protocol):
    def get_task(self, task_id: str) -> Mapping[str, Any]: ...
    def get_latest_checkpoint(self, task_id: str
                              ) -> Mapping[str, Any] | None: ...


class ModelAudit(Protocol):
    def get_verified_run(self, run_id: str) -> Mapping[str, Any]: ...


class GitObjectSource(Protocol):
    def export(self, intent: PrePRIntent) -> Mapping[str, bytes]: ...
    def materialize_independently(self, raw: bytes, *,
                                  expected_sha: str) -> Mapping[str, Any]: ...


class CasWriter(Protocol):
    def put_if_absent(self, digest_hex: str, raw: bytes, *,
                      retention_days: int) -> str: ...


class CasReader(Protocol):
    def get_exact(self, ref: str) -> bytes: ...


class Validator(Protocol):
    def execute_independently(self, *, repository: str, sha: str,
                              artifact_ref: str, request_id: str
                              ) -> Mapping[str, Any]: ...


class DecisionLedger(Protocol):
    def read(self, key: str) -> Mapping[str, Any] | None: ...
    def put_if_absent(self, key: str, decision: Mapping[str, Any]) -> None: ...


class DraftGitHub(Protocol):
    def find_open_prs(self, repository: str, branch: str
                      ) -> list[Mapping[str, Any]]: ...
    def create_draft(self, *, repository: str, head: str, base: str,
                     body: str) -> None: ...
    def read_pr(self, repository: str, number: int
                ) -> Mapping[str, Any]: ...


def _revalidate(intent: PrePRIntent, authority: Authority) -> None:
    """Every call uses fresh trusted snapshots, never user-supplied issue text."""
    issue = authority.issue(intent.repository, intent.issue_number)
    todo = authority.todo(intent.todo_id)
    auth = authority.authorization(intent.request_id)
    require(type(issue) is dict and issue.get("state") == "open"
            and issue.get("repository") == intent.repository
            and issue.get("number") == intent.issue_number
            and issue.get("is_pull_request") is False,
            "prepr_issue_not_open")
    require(type(todo) is dict and todo.get("id") == intent.todo_id
            and todo.get("state") in ("AUTHORIZED", "IN_PROGRESS", "BLOCKED")
            and todo.get("repository") == intent.repository
            and todo.get("issue_number") == intent.issue_number,
            "prepr_todo_not_authorized")
    require(type(auth) is dict and auth.get("authorized") is True
            and auth.get("source") == "trusted-control-plane"
            and auth.get("repository") == intent.repository
            and auth.get("issue_number") == intent.issue_number
            and auth.get("request_id") == intent.request_id
            and auth.get("idempotency_key") == intent.idempotency_key
            and auth.get("base_sha") == intent.base_sha
            and auth.get("candidate_sha") == intent.candidate_sha
            and auth.get("source_sha") == intent.source_sha
            and auth.get("candidate_ref") == intent.candidate_ref
            and auth.get("todo_id") == intent.todo_id
            and auth.get("correlation_id") == intent.correlation_id
            and auth.get("allowed_paths") == list(intent.allowed_paths),
            "prepr_authority_unproved")
    require(authority.branch_sha(intent.repository, intent.base_ref)
            == intent.base_sha, "prepr_base_advanced")
    require(authority.branch_sha(intent.repository, intent.candidate_ref)
            == intent.candidate_sha, "prepr_candidate_advanced")


def _task(intent: PrePRIntent, pool: ExistingPool) -> Mapping[str, Any]:
    task = pool.get_task(intent.task_id)
    require(type(task) is dict and task.get("task_id") == intent.task_id
            and task.get("repository") == intent.repository
            and task.get("issue_number") == intent.issue_number
            and task.get("request_id") == intent.request_id
            and task.get("correlation_id") == intent.correlation_id
            and task.get("base_sha") == intent.base_sha
            and task.get("produced_sha") == intent.candidate_sha
            and task.get("fencing_required") is True
            and task.get("leased_by") is None
            and task.get("state") == "completed"
            and type(task.get("lease_generation")) is int
            and task["lease_generation"] >= 2
            and type(task.get("attempt_count")) is int
            and task["attempt_count"] >= 2
            and type(task.get("builder_worker_id")) is str
            and IDENT.fullmatch(task["builder_worker_id"]) is not None
            and type(task.get("validator_worker_id")) is str
            and IDENT.fullmatch(task["validator_worker_id"]) is not None
            and task["builder_worker_id"] != task["validator_worker_id"],
            "prepr_fenced_builder_unproved")
    checkpoint = pool.get_latest_checkpoint(intent.task_id)
    require(type(checkpoint) is dict
            and checkpoint.get("task_id") == intent.task_id
            and checkpoint.get("stage") == "validator_result"
            and checkpoint.get("next_action") == "complete"
            and checkpoint.get("lease_generation") == task["lease_generation"]
            and type(checkpoint.get("attempt_id")) is str
            and IDENT.fullmatch(checkpoint["attempt_id"]) is not None,
            "prepr_validator_checkpoint_unproved")
    return {**task, "fence_id": checkpoint["attempt_id"]}


def _model(intent: PrePRIntent, task: Mapping[str, Any],
           provider: ModelAudit) -> Mapping[str, Any]:
    """Authenticated provider run lookup; a retrospective JSON receipt fails."""
    run = provider.get_verified_run(intent.task_id)
    require(type(run) is dict and run.get("provider_verified") is True
            and run.get("task_id") == intent.task_id
            and type(run.get("run_id")) is str
            and IDENT.fullmatch(run["run_id"]) is not None
            and run.get("request_id") == intent.request_id
            and run.get("base_sha") == intent.base_sha
            and run.get("produced_sha") == intent.candidate_sha
            and run.get("source_sha") == intent.source_sha
            and run.get("builder_id") == task["builder_worker_id"]
            and type(run.get("model")) is str and bool(run["model"])
            and type(run.get("model_digest")) is str
            and SHA256.fullmatch(run["model_digest"]) is not None
            and type(run.get("call_count")) is int
            and run["call_count"] >= 1
            and type(run.get("baseline_failures")) is int
            and run["baseline_failures"] > 0
            and type(run.get("baseline_sha256")) is str
            and SHA256.fullmatch(run["baseline_sha256"]) is not None,
            "prepr_real_model_unproved")
    return run


def _git_package(intent: PrePRIntent, source: GitObjectSource
                 ) -> tuple[bytes, Mapping[str, str]]:
    objects = source.export(intent)
    require(type(objects) is dict and set(objects) ==
            {"git_bundle", "patch", "before", "after"},
            "prepr_git_export_invalid")
    for value in objects.values():
        require(type(value) is bytes and 0 < len(value) <= MAX_CAS_BYTES,
                "prepr_git_export_invalid")
    require(objects["before"] != objects["after"]
            and objects["patch"].strip(), "prepr_empty_patch")
    hashes = {key + "_sha256": digest(value) for key, value in objects.items()}
    envelope = {
        "schema": "prepr_git_objects_v1",
        "repository": intent.repository,
        "base_sha": intent.base_sha,
        "produced_sha": intent.candidate_sha,
        "published_sha": intent.candidate_sha,
        "source_sha": intent.source_sha,
        "allowed_paths": list(intent.allowed_paths),
        "hashes": hashes,
        "objects": {key: __import__("base64").b64encode(value).decode("ascii")
                    for key, value in objects.items()},
    }
    raw = canonical(envelope)
    require(0 < len(raw) <= MAX_CAS_BYTES, "prepr_cas_size_invalid")
    return raw, hashes


def _materialize(intent: PrePRIntent, source: GitObjectSource,
                 raw: bytes, hashes: Mapping[str, str]) -> None:
    result = source.materialize_independently(
        raw, expected_sha=intent.candidate_sha,
    )
    require(type(result) is dict
            and result.get("checkout_isolated") is True
            and result.get("commit_verified") is True
            and result.get("source_integrity") is True
            and result.get("base_sha") == intent.base_sha
            and result.get("produced_sha") == intent.candidate_sha
            and result.get("published_sha") == intent.candidate_sha
            and result.get("source_sha") == intent.source_sha
            and result.get("changed_paths") == list(intent.allowed_paths)
            and result.get("diff_nonempty") is True
            and result.get("hashes") == hashes
            and result.get("parent_is_base_ancestor") is True,
            "prepr_independent_git_unproved")


def _validate(intent: PrePRIntent, task: Mapping[str, Any],
              validator: Validator, artifact_ref: str,
              hashes: Mapping[str, str], model: Mapping[str, Any]
              ) -> Mapping[str, Any]:
    result = validator.execute_independently(
        repository=intent.repository, sha=intent.candidate_sha,
        artifact_ref=artifact_ref, request_id=intent.request_id,
    )
    require(type(result) is dict and
            result.get("checkout_isolated") is True
            and result.get("model_calls") == 0
            and result.get("validator_id") == task["validator_worker_id"]
            and result.get("validator_id") != task["builder_worker_id"]
            and type(result.get("validator_id")) is str
            and IDENT.fullmatch(result["validator_id"]) is not None
            and result.get("sha") == intent.candidate_sha
            and result.get("source_sha") == intent.source_sha
            and result.get("before_sha256") == hashes["before_sha256"]
            and result.get("after_sha256") == hashes["after_sha256"]
            and result.get("baseline_sha256") == model["baseline_sha256"]
            and type(result.get("baseline_failures")) is int
            and result["baseline_failures"] >= 1
            and type(result.get("passed")) is int and result["passed"] > 0
            and result.get("passed") == result.get("total")
            and type(result.get("regression_passed")) is int
            and result["regression_passed"] > 0
            and result.get("regression_passed") ==
            result.get("regression_total")
            and result.get("failed") == 0 and result.get("errors") == 0
            and result.get("skipped") == 0,
            "prepr_validator_unproved")
    return result


def ready_for_pr(*, intent: PrePRIntent, authority: Authority,
                 pool: ExistingPool, model_audit: ModelAudit,
                 source: GitObjectSource, writer: CasWriter,
                 independent_reader: CasReader, validator: Validator,
                 ledger: DecisionLedger, cost: Mapping[str, Any]
                 ) -> dict[str, Any]:
    """Read-only authority checks -> CAS write/readback -> Validator -> durable READY.

    Replay reads existing READY with fresh head/cost/task checks and no model
    invocation, CAS write, or validation. A production ledger must be atomic,
    independently readable and protected by idempotency key/fencing.
    """
    intent.check()
    _revalidate(intent, authority)
    task = _task(intent, pool)
    require(type(cost) is dict and cost.get("verified") is True
            and type(cost.get("incremental_brl")) in (int, float)
            and cost["incremental_brl"] == 0
            and cost.get("overage_blocked") is True,
            "prepr_cost_unknown")
    saved = ledger.read(intent.idempotency_key)
    if saved is not None:
        require(type(saved) is dict and saved.get("state") == "READY_FOR_PR"
                and saved.get("repository") == intent.repository
                and saved.get("request_id") == intent.request_id
                and saved.get("base_sha") == intent.base_sha
                and saved.get("head_sha") == intent.candidate_sha
                and saved.get("source_sha") == intent.source_sha
                and saved.get("todo_id") == intent.todo_id
                and saved.get("task_id") == intent.task_id
                and saved.get("changed_paths") == list(intent.allowed_paths)
                and saved.get("validated_sha") == intent.candidate_sha
                and saved.get("fence_id") == task["fence_id"]
                and saved.get("lease_generation") == task["lease_generation"],
                "prepr_replay_conflict")
        require(type(saved.get("artifact_ref")) is str
                and type(saved.get("artifact_sha256")) is str
                and SHA256.fullmatch(saved["artifact_sha256"]) is not None
                and digest(independent_reader.get_exact(saved["artifact_ref"]))
                == saved["artifact_sha256"],
                "prepr_replay_artifact_unreadable")
        return {**saved, "replayed": True}

    require(writer is not independent_reader,
            "prepr_cas_readback_not_independent")
    model = _model(intent, task, model_audit)
    raw, hashes = _git_package(intent, source)
    artifact_sha = digest(raw)
    artifact_ref = writer.put_if_absent(
        artifact_sha, raw, retention_days=RETENTION_DAYS,
    )
    require(type(artifact_ref) is str and
            artifact_ref.startswith("cas://sha256/") and
            artifact_ref.endswith(artifact_sha),
            "prepr_cas_ref_invalid")
    require(independent_reader.get_exact(artifact_ref) == raw,
            "prepr_cas_readback_failed")
    _materialize(intent, source, raw, hashes)
    validation = _validate(intent, task, validator, artifact_ref, hashes, model)
    # After expensive operations, recheck base/HEAD/fencing before READY.
    _revalidate(intent, authority)
    require(_task(intent, pool) == task, "prepr_fence_changed")
    decision = {
        "state": "READY_FOR_PR",
        "repository": intent.repository,
        "issue_number": intent.issue_number,
        "todo_id": intent.todo_id,
        "request_id": intent.request_id,
        "correlation_id": intent.correlation_id,
        "idempotency_key": intent.idempotency_key,
        "base_sha": intent.base_sha,
        "head_sha": intent.candidate_sha,
        "source_sha": intent.source_sha,
        "task_id": intent.task_id,
        "fence_id": task["fence_id"],
        "lease_generation": task["lease_generation"],
        "model_run_id": model["run_id"],
        "model": model["model"],
        "model_digest": model["model_digest"],
        "model_calls": model["call_count"],
        "validator_id": validation["validator_id"],
        "validated_sha": validation["sha"],
        "artifact_ref": artifact_ref,
        "artifact_sha256": artifact_sha,
        "patch_sha256": hashes["patch_sha256"],
        "before_sha256": hashes["before_sha256"],
        "after_sha256": hashes["after_sha256"],
        "retention_days": RETENTION_DAYS,
        "cost_incremental_brl": 0,
        "changed_paths": list(intent.allowed_paths),
    }
    ledger.put_if_absent(intent.idempotency_key, decision)
    require(ledger.read(intent.idempotency_key) == decision,
            "prepr_ready_readback_failed")
    return {**decision, "replayed": False}


def publish_single_draft(*, intent: PrePRIntent, authority: Authority,
                         ledger: DecisionLedger, github: DraftGitHub,
                         independent_reader: CasReader
                         ) -> dict[str, Any]:
    """Only after persisted READY; uncertain writes require readback, not retry."""
    intent.check()
    _revalidate(intent, authority)
    ready = ledger.read(intent.idempotency_key)
    require(type(ready) is dict and ready.get("state") == "READY_FOR_PR"
            and ready.get("head_sha") == intent.candidate_sha
            and ready.get("base_sha") == intent.base_sha
            and ready.get("repository") == intent.repository
            and ready.get("request_id") == intent.request_id
            and ready.get("source_sha") == intent.source_sha
            and ready.get("todo_id") == intent.todo_id
            and ready.get("task_id") == intent.task_id
            and ready.get("validated_sha") == intent.candidate_sha
            and ready.get("changed_paths") == list(intent.allowed_paths)
            and type(ready.get("artifact_ref")) is str
            and type(ready.get("artifact_sha256")) is str
            and SHA256.fullmatch(ready["artifact_sha256"]) is not None
            and digest(independent_reader.get_exact(ready["artifact_ref"]))
            == ready["artifact_sha256"], "prepr_ready_missing")
    prs = github.find_open_prs(intent.repository, intent.candidate_ref)
    require(type(prs) is list and len(prs) <= 1,
            "prepr_duplicate_pr")
    created = False
    if not prs:
        github.create_draft(
            repository=intent.repository, head=intent.candidate_ref,
            base=intent.base_ref,
            body=("Pre-PR validated: " + intent.idempotency_key + "\n"
                  + "Task: " + intent.task_id + "\n"
                  + "Artifact: " + ready["artifact_ref"] + "\n"
                  + "Head: " + intent.candidate_sha + "\n"
                  + "No automatic merge/deploy.\n"),
        )
        created = True
    # Query again even after ambiguous publication; never blindly create twice.
    observed = github.find_open_prs(intent.repository, intent.candidate_ref)
    require(type(observed) is list and len(observed) == 1
            and type(observed[0]) is dict and
            type(observed[0].get("number")) is int,
            "prepr_publication_ambiguous")
    number = observed[0]["number"]
    pr = github.read_pr(intent.repository, number)
    require(type(pr) is dict and pr.get("state") == "open"
            and pr.get("draft") is True
            and pr.get("head_ref") == intent.candidate_ref
            and pr.get("head_sha") == intent.candidate_sha
            and pr.get("base_ref") == intent.base_ref
            and pr.get("base_sha") == intent.base_sha
            and pr.get("changed_paths") == list(intent.allowed_paths)
            and pr.get("diff_nonempty") is True
            and pr.get("idempotency_key") == intent.idempotency_key,
            "prepr_draft_readback_failed")
    _revalidate(intent, authority)
    return {"state": "DRAFT_PR_VERIFIED", "pr_number": number,
            "head_sha": intent.candidate_sha, "base_sha": intent.base_sha,
            "replayed": not created, "merge_performed": False}
