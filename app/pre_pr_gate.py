"""Generic fail-closed pre-PR readiness policy.

This module does not run a model, trust issue text, create branches, open pull
requests, merge, deploy, or touch a physical host. Trusted adapters must supply
the Builder CAS receipt and an independently produced Validator receipt.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

SHA40 = re.compile(r"[0-9a-f]{40}\\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\\Z")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\\Z")
TODO = re.compile(r"TODO-[1-9][0-9]*\\Z")
CORRELATION = re.compile(r"[a-z0-9][a-z0-9._-]{7,127}\\Z")
MODEL = re.compile(r"[A-Za-z0-9_.:/-]{3,128}\\Z")


class PrePRBlocked(RuntimeError):
    """Stable refusal code; remote or model content is never embedded."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise PrePRBlocked(reason)


def canonical_sha256(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sha(value: Any, reason: str) -> str:
    require(type(value) is str and SHA40.fullmatch(value) is not None, reason)
    return value


def _digest(value: Any, reason: str) -> str:
    require(type(value) is str and SHA256.fullmatch(value) is not None, reason)
    return value


def validate_request(request: Mapping[str, Any]) -> dict[str, Any]:
    require(type(request) is dict, "request_shape_invalid")
    expected = {
        "schema_version", "repository", "issue_number", "todo_id",
        "base_sha", "head_sha", "source_branch", "correlation_id",
    }
    require(set(request) == expected, "request_fields_invalid")
    require(request["schema_version"] == "pre-pr.request.v1", "request_schema_invalid")
    require(type(request["repository"]) is str
            and REPO.fullmatch(request["repository"]) is not None,
            "repository_invalid")
    require(type(request["issue_number"]) is int and request["issue_number"] > 0,
            "issue_number_invalid")
    require(type(request["todo_id"]) is str
            and TODO.fullmatch(request["todo_id"]) is not None, "todo_id_invalid")
    base = _sha(request["base_sha"], "base_sha_invalid")
    head = _sha(request["head_sha"], "head_sha_invalid")
    require(base != head, "base_head_must_differ")
    require(type(request["source_branch"]) is str
            and BRANCH.fullmatch(request["source_branch"]) is not None
            and not request["source_branch"].startswith("/")
            and ".." not in request["source_branch"],
            "source_branch_invalid")
    require(type(request["correlation_id"]) is str
            and CORRELATION.fullmatch(request["correlation_id"]) is not None,
            "correlation_id_invalid")
    return dict(request)


def _validate_builder(receipt: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    require(type(receipt) is dict, "builder_receipt_invalid")
    required = {
        "schema_version", "repository", "issue_number", "todo_id",
        "base_sha", "source_sha", "produced_sha", "source_branch",
        "correlation_id", "model_id", "model_digest", "model_calls",
        "baseline_failed", "changed_paths", "before_sha256", "after_sha256",
        "patch_sha256", "artifact_ref", "artifact_sha256",
        "lease_generation", "attempt_id", "builder_worker_id",
    }
    require(set(receipt) == required, "builder_fields_invalid")
    require(receipt["schema_version"] == "pre-pr.builder.v1",
            "builder_schema_invalid")
    for key in ("repository", "issue_number", "todo_id", "base_sha",
                "source_branch", "correlation_id"):
        require(receipt[key] == request[key], "builder_request_binding_invalid")
    require(_sha(receipt["source_sha"], "source_sha_invalid") == request["head_sha"],
            "builder_source_sha_mismatch")
    produced = _sha(receipt["produced_sha"], "produced_sha_invalid")
    require(produced not in {request["base_sha"], request["head_sha"]},
            "produced_sha_not_new")
    require(type(receipt["model_id"]) is str
            and MODEL.fullmatch(receipt["model_id"]) is not None,
            "model_identity_invalid")
    _digest(receipt["model_digest"], "model_digest_invalid")
    require(type(receipt["model_calls"]) is int
            and 1 <= receipt["model_calls"] <= 2, "model_calls_invalid")
    require(receipt["baseline_failed"] is True, "baseline_failure_unproved")
    require(type(receipt["changed_paths"]) is list
            and 1 <= len(receipt["changed_paths"]) <= 32
            and len(receipt["changed_paths"]) == len(set(receipt["changed_paths"]))
            and all(type(path) is str and 1 <= len(path) <= 240
                    and not path.startswith("/") and ".." not in path.split("/")
                    for path in receipt["changed_paths"]),
            "changed_paths_invalid")
    for key in ("before_sha256", "after_sha256", "patch_sha256",
                "artifact_sha256"):
        _digest(receipt[key], key + "_invalid")
    require(receipt["before_sha256"] != receipt["after_sha256"],
            "repair_bytes_unchanged")
    require(type(receipt["artifact_ref"]) is str
            and receipt["artifact_ref"].startswith("cas+git://sha256/")
            and receipt["artifact_ref"].endswith(receipt["artifact_sha256"])
            and len(receipt["artifact_ref"]) <= 256, "artifact_ref_invalid")
    require(type(receipt["lease_generation"]) is int
            and receipt["lease_generation"] >= 1, "lease_generation_invalid")
    require(type(receipt["attempt_id"]) is str
            and CORRELATION.fullmatch(receipt["attempt_id"]) is not None,
            "attempt_id_invalid")
    require(type(receipt["builder_worker_id"]) is str
            and CORRELATION.fullmatch(receipt["builder_worker_id"]) is not None,
            "builder_worker_invalid")


def _validate_validator(validator: Mapping[str, Any],
                        builder: Mapping[str, Any],
                        request: Mapping[str, Any]) -> None:
    require(type(validator) is dict, "validator_receipt_invalid")
    required = {
        "schema_version", "repository", "issue_number", "todo_id",
        "base_sha", "source_sha", "produced_sha", "correlation_id",
        "artifact_ref", "artifact_sha256", "cas_readback_sha256",
        "patch_sha256", "before_sha256", "after_sha256",
        "changed_paths", "independent_checkout", "validator_worker_id",
        "validator_model_calls", "positive_passed", "negative_passed",
        "regression_passed", "replay_passed", "lease_generation",
        "attempt_id",
    }
    require(set(validator) == required, "validator_fields_invalid")
    require(validator["schema_version"] == "pre-pr.validator.v1",
            "validator_schema_invalid")
    for key in (
        "repository", "issue_number", "todo_id", "base_sha", "source_sha",
        "produced_sha", "correlation_id", "artifact_ref", "artifact_sha256",
        "patch_sha256", "before_sha256", "after_sha256", "changed_paths",
        "lease_generation", "attempt_id",
    ):
        require(validator[key] == builder[key], "validator_builder_binding_invalid")
    require(validator["source_sha"] == request["head_sha"],
            "validator_source_sha_mismatch")
    require(_digest(validator["cas_readback_sha256"],
                    "cas_readback_sha256_invalid") == builder["artifact_sha256"],
            "cas_readback_mismatch")
    require(validator["independent_checkout"] is True,
            "independent_checkout_unproved")
    require(type(validator["validator_worker_id"]) is str
            and CORRELATION.fullmatch(validator["validator_worker_id"]) is not None
            and validator["validator_worker_id"] != builder["builder_worker_id"],
            "validator_independence_unproved")
    require(validator["validator_model_calls"] == 0,
            "validator_model_call_forbidden")
    for key in ("positive_passed", "negative_passed", "regression_passed",
                "replay_passed"):
        require(validator[key] is True, key + "_unproved")


def evaluate_pre_pr(
    request: Mapping[str, Any],
    builder_receipt: Mapping[str, Any],
    validator_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Return READY_FOR_PR only for one exact, independently validated CAS."""
    checked = validate_request(request)
    _validate_builder(builder_receipt, checked)
    _validate_validator(validator_receipt, builder_receipt, checked)
    evidence = {
        "request": checked,
        "builder": dict(builder_receipt),
        "validator": dict(validator_receipt),
    }
    return {
        "schema_version": "pre-pr.decision.v1",
        "status": "READY_FOR_PR",
        "repository": checked["repository"],
        "issue_number": checked["issue_number"],
        "todo_id": checked["todo_id"],
        "base_sha": checked["base_sha"],
        "source_sha": checked["head_sha"],
        "produced_sha": builder_receipt["produced_sha"],
        "artifact_ref": builder_receipt["artifact_ref"],
        "artifact_sha256": builder_receipt["artifact_sha256"],
        "patch_sha256": builder_receipt["patch_sha256"],
        "correlation_id": checked["correlation_id"],
        "evidence_sha256": canonical_sha256(evidence),
        "draft_pr_authorized": True,
        "merge_authorized": False,
        "physical_execution_authorized": False,
    }


def replay_pre_pr(
    expected_decision: Mapping[str, Any],
    request: Mapping[str, Any],
    builder_receipt: Mapping[str, Any],
    validator_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """An exact replay is stable; any drift or new model result is rejected."""
    observed = evaluate_pre_pr(request, builder_receipt, validator_receipt)
    require(type(expected_decision) is dict and dict(expected_decision) == observed,
            "replay_decision_drift")
    return {**observed, "replayed": True}
