"""Rules #213: fixed native Builder/Validator adapter for the existing Worker Pool.

The queue never defines commands or model prompts. Only a separately
preinstalled, hash-bound NativeGatewayCommandRegistry may invoke the approved
risk-2 repair/stage/commit and risk-1 validation commands. This adapter checks
source scope, model receipt, Git effect and the independent regression receipt.

This module does not claim leases, create CAS, start a model, access a host,
install any runtime, create PRs or assert READY_FOR_PR. NativeOneShotExecutor
and its separately authorized durable artifact transport own those controls.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from app.native_one_shot import (
    AdapterContext, AdapterDescriptor, AdapterResult,
    LocalWorkspace, NativeExecutorBlocked, TaskManifest,
)
from app.rules213_updater_profile import (
    BASE_SHA, ISSUE, MODEL, REPOSITORY, SOURCE_FUNCTIONS,
    check_generated_scope, require, Rules213Blocked,
)

# New native contract; do NOT modify the historical pre-admission v1 hash.
NATIVE_PROFILE = "rules213.updater.native.v1"
OFFICIAL_DIGEST = "dae161e27b0e90dd1856c8bb3209201fd6736d8eb66298e75ed87571486f4364"
NATIVE_DESCRIPTOR = AdapterDescriptor(
    adapter_id=NATIVE_PROFILE,
    allowed_paths=tuple(sorted(SOURCE_FUNCTIONS)),
    builder_commands=("rules213.apply", "rules213.commit", "rules213.propose", "rules213.stage"),
    validator_commands=("rules213.validate",),
)
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA64 = re.compile(r"[0-9a-f]{64}\Z")
CASE = re.compile(r"[A-Za-z0-9._:-]{4,160}\Z")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def _read_sources(root: Path) -> dict[str, bytes]:
    """Only two constant paths; refuse symlinks, junctions and oversize files."""
    result: dict[str, bytes] = {}
    require(root.is_dir() and not root.is_symlink(), "rules213_workspace_invalid")
    base = root.resolve()
    for relative in sorted(SOURCE_FUNCTIONS):
        file = root / relative
        require(file.is_file() and not file.is_symlink()
                and file.resolve().is_relative_to(base)
                and file.stat().st_nlink == 1
                and 0 < file.stat().st_size <= 300000,
                "rules213_workspace_file_invalid")
        result[relative] = file.read_bytes()
    return result


class Rules213NativeAdapter:
    """No command implementation here: NativeOneShot supplies trusted registry."""

    descriptor = NATIVE_DESCRIPTOR

    def __init__(
        self, *, task_id: str, original: Mapping[str, bytes],
        live_main_reader: Callable[[], str],
        issue_reader: Callable[[], Mapping[str, Any]],
        baseline_checker: Callable[[Path], Mapping[str, Any]],
        model_receipt_reader: Callable[[], Mapping[str, Any]],
        validator_evidence_reader: Callable[[Path, str], Mapping[str, Any]],
    ) -> None:
        require(type(task_id) is str
                and re.fullmatch(r"cwp-[0-9a-f]{24}", task_id) is not None,
                "rules213_task_id_invalid")
        require(type(original) is dict
                and set(original) == set(SOURCE_FUNCTIONS)
                and all(type(data) is bytes and 0 < len(data) <= 300000
                        for data in original.values()),
                "rules213_original_invalid")
        require(all(callable(fn) for fn in (
            live_main_reader, issue_reader, baseline_checker,
            model_receipt_reader, validator_evidence_reader)),
            "rules213_attestors_required")
        self.task_id = task_id
        self.original = dict(original)
        self.live_main_reader = live_main_reader
        self.issue_reader = issue_reader
        self.baseline_checker = baseline_checker
        self.model_receipt_reader = model_receipt_reader
        self.validator_evidence_reader = validator_evidence_reader

    def _admit(self, context: AdapterContext, *, role: str,
               produced_sha: str | None = None) -> TaskManifest:
        manifest = context.manifest
        require(type(manifest) is TaskManifest
                and manifest.task_id == self.task_id
                and manifest.repository == REPOSITORY
                and manifest.base_sha == BASE_SHA
                and manifest.adapter_id == NATIVE_PROFILE
                and manifest.adapter_contract_sha256 == NATIVE_DESCRIPTOR.contract_sha256
                and manifest.workspace_sha == (
                    BASE_SHA if role == "builder" else produced_sha),
                "rules213_native_manifest_drift")
        live_main = self.live_main_reader()
        require(type(live_main) is str and live_main == BASE_SHA,
                "rules213_native_main_advanced")
        issue = self.issue_reader()
        require(type(issue) is dict
                and issue.get("number") == ISSUE
                and issue.get("state") == "open"
                and issue.get("repository") == REPOSITORY,
                "rules213_native_issue_unproved")
        require(set(manifest.before_sha256) == set(SOURCE_FUNCTIONS),
                "rules213_native_manifest_paths")
        current = _read_sources(context.root)
        for path in SOURCE_FUNCTIONS:
            require(manifest.before_sha256[path] == _sha(current[path]),
                    "rules213_native_workspace_hash_drift")
        if role == "builder":
            require(current == self.original, "rules213_native_original_drift")
            for path in SOURCE_FUNCTIONS:
                require(manifest.before_sha256[path] == _sha(self.original[path]),
                        "rules213_native_original_hash_drift")
        else:
            require(type(produced_sha) is str
                    and SHA40.fullmatch(produced_sha) is not None
                    and produced_sha != BASE_SHA,
                    "rules213_native_validator_head")
            check_generated_scope(self.original, current)
        return manifest

    def build(self, context: AdapterContext) -> AdapterResult:
        self._admit(context, role="builder")
        baseline = self.baseline_checker(context.root)
        require(type(baseline) is dict
                and baseline.get("result") == "FAILED_REPRODUCED"
                and baseline.get("base_sha") == BASE_SHA
                and type(baseline.get("failed_count")) is int
                and baseline["failed_count"] >= 1
                and type(baseline.get("case_id")) is str
                and CASE.fullmatch(baseline["case_id"]) is not None,
                "rules213_failing_baseline_missing")
        # PHASE 1: propose in a trusted external receipt WITHOUT writing source.
        proposed = context.invoke("rules213.propose")
        require(type(proposed) is dict
                and proposed.get("result") == "GOVERNED_COMMAND_VERIFIED",
                "rules213_propose_command_unverified")
        require(_read_sources(context.root) == self.original,
                "rules213_proposal_changed_source")
        # Independent trusted model-transport reader must bind its receipt to
        # this task, base, exact pinned model and the candidate file hashes.
        model = self.model_receipt_reader()
        require(type(model) is dict
                and model.get("task_id") == self.task_id
                and model.get("base_sha") == BASE_SHA
                and model.get("model") == MODEL
                and model.get("digest") == OFFICIAL_DIGEST
                and model.get("process_verified") is True
                and model.get("origin") == "http://127.0.0.1:11434"
                and type(model.get("model_calls")) is int
                and 1 <= model["model_calls"] <= 2
                and type(model.get("after_sha256")) is dict
                and set(model["after_sha256"]) == set(SOURCE_FUNCTIONS)
                and all(type(item) is str and SHA64.fullmatch(item)
                        for item in model["after_sha256"].values()),
                "rules213_model_provenance_unattested")
        # PHASE 2: only the fixed, trusted, Gateway-mediated 'apply' command
        # may mutate the repo. Its result must match the independently-read
        # Qwen proposal exactly; otherwise no Git stage or commit is allowed.
        applied = context.invoke("rules213.apply")
        require(type(applied) is dict
                and applied.get("result") == "GOVERNED_COMMAND_VERIFIED",
                "rules213_apply_command_unverified")
        after = _read_sources(context.root)
        scope = check_generated_scope(self.original, after)
        require(model["after_sha256"] == {
            path: _sha(after[path]) for path in sorted(SOURCE_FUNCTIONS)
        }, "rules213_applied_candidate_hash_mismatch")
        staged = context.invoke("rules213.stage")
        require(type(staged) is dict
                and staged.get("result") == "GOVERNED_COMMAND_VERIFIED",
                "rules213_stage_command_unverified")
        committed = context.invoke("rules213.commit")
        require(type(committed) is dict
                and committed.get("result") == "GOVERNED_COMMAND_VERIFIED",
                "rules213_commit_command_unverified")
        produced_sha = committed.get("head")
        require(type(produced_sha) is str
                and SHA40.fullmatch(produced_sha) is not None
                and produced_sha != BASE_SHA,
                "rules213_produced_sha_unverified")
        git = LocalWorkspace(context.root)
        require(git.head() == produced_sha
                and git.status() == ""
                and git.changed_paths(BASE_SHA, produced_sha)
                == tuple(scope["changed_paths"]),
                "rules213_git_scope_unverified")
        evidence = _sha(_canonical({
            "task_id": self.task_id,
            "source_sha": BASE_SHA, "produced_sha": produced_sha,
            "changed_paths": scope["changed_paths"],
            "source_hashes": model["after_sha256"],
            "model_digest": OFFICIAL_DIGEST,
            "model_calls": model["model_calls"],
            "baseline_case_id": baseline["case_id"],
        }))
        return AdapterResult(
            produced_sha, tuple(scope["changed_paths"]),
            tuple(context.invoked), evidence,
        )

    def validate(self, context: AdapterContext, produced_sha: str) -> AdapterResult:
        self._admit(context, role="validator", produced_sha=produced_sha)
        git = LocalWorkspace(context.root)
        require(git.head() == produced_sha and git.status() == ""
                and git.changed_paths(BASE_SHA, produced_sha),
                "rules213_validator_checkout_unverified")
        result = context.invoke("rules213.validate")
        require(type(result) is dict
                and result.get("result") == "GOVERNED_COMMAND_VERIFIED"
                and result.get("head") == produced_sha,
                "rules213_validator_command_unverified")
        report = self.validator_evidence_reader(context.root, produced_sha)
        require(type(report) is dict
                and report.get("result") == "INDEPENDENT_TESTS_PASSED"
                and report.get("produced_sha") == produced_sha
                and report.get("source_sha") == BASE_SHA
                and report.get("validator_role") == "validator"
                and type(report.get("tests_passed")) is int
                and report["tests_passed"] > 0
                and report.get("tests_failed") == 0
                and report.get("negative_control_passed") is True
                and report.get("idempotency_passed") is True,
                "rules213_independent_validator_unproved")
        scope = check_generated_scope(self.original, _read_sources(context.root))
        return AdapterResult(
            produced_sha, tuple(scope["changed_paths"]),
            tuple(context.invoked),
            _sha(_canonical({"sha": produced_sha, "report": report,
                             "paths": scope["changed_paths"]})),
        )
