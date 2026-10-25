"""Fixed Qwen7B Builder/Validator adapter for one approved GitHub issue profile.

The Worker Pool retains claim/lease/fencing authority. This module supplies
ONLY a preinstalled, bounded code handler for bridge.property.v1; a queue task
cannot provide commands, Python paths, a model endpoint or repo identity.
Real execution is permitted only within an attested Session Launcher/Gateway
worktree. This module does not start jobs, publish GitHub branches or merge.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from app.issue_rag_handoff import BoundIssueRAG, bind_issue_rag
from app.issue_draft_pr import (
    FUNCTION, MODEL_DIGEST_PREFIX, PATH, PROFILE, REPO, IssueBinding,
    HandoffBlocked, require, sha256, trusted_task,
)
from app.native_one_shot import (
    AdapterContext, AdapterDescriptor, AdapterResult, TaskManifest,
)
from app.native_structured_ollama import NativeStructuredOllama
from app.structured_code_worker import (
    extracted, render, repair, validate,
)

DESCRIPTOR = AdapterDescriptor(
    adapter_id=PROFILE,
    allowed_paths=(PATH,),
    builder_commands=("issue.repair",),
    validator_commands=("issue.validate",),
)


class IssueQwenNativeAdapter:
    """Adapter for the existing NativeOneShotExecutor, never a scheduler."""

    descriptor = DESCRIPTOR

    def __init__(self, *, binding: IssueBinding, task_id: str, before: bytes,
                 state_directory: Path, propose: Callable[[dict, str], str],
                 regression_runner: Callable[[Path], Mapping[str, Any]],
                 require_native_model: bool = True,
                 rag_receipt: Mapping[str, Any] | None = None,
                 rag_source_reader: Callable[[str, str], bytes] | None = None):
        require(type(binding) is IssueBinding
                and type(task_id) is str
                and task_id.startswith("cwp-") and len(task_id) == 28
                and type(before) is bytes and 0 < len(before) <= 262144,
                "issue_native_identity_invalid")
        require(type(require_native_model) is bool,
                "issue_native_mode_invalid")
        if require_native_model:
            require(type(propose) is NativeStructuredOllama,
                    "issue_native_model_required")
        require(callable(propose) and callable(regression_runner),
                "issue_native_handlers_missing")
        require((rag_receipt is None and rag_source_reader is None) or
                (type(rag_receipt) is dict and callable(rag_source_reader)
                 and require_native_model), "issue_rag_opt_in_invalid")
        self.binding = binding
        self.task_id = task_id
        self.before = before
        self.state_directory = Path(state_directory)
        self.propose = propose
        self.regression_runner = regression_runner
        self.require_native_model = require_native_model
        self.rag_receipt = rag_receipt
        self.rag_source_reader = rag_source_reader

    def _manifest(self, ctx: AdapterContext) -> TaskManifest:
        manifest = ctx.manifest
        require(manifest.repository == REPO
                and manifest.task_id == self.task_id
                and manifest.base_sha == self.binding.base_sha
                and manifest.adapter_id == PROFILE
                and manifest.adapter_contract_sha256 == self.descriptor.contract_sha256
                and set(manifest.before_sha256) == {PATH},
                "issue_native_manifest_untrusted")
        return manifest

    def _workspace_file(self, ctx: AdapterContext) -> bytes:
        file = ctx.root / PATH
        require(file.is_file() and not file.is_symlink()
                and file.resolve().is_relative_to(ctx.root.resolve()),
                "issue_native_file_invalid")
        value = file.read_bytes()
        require(0 < len(value) <= 262144, "issue_native_file_invalid")
        return value

    def commands(self, root: Path) -> dict[str, Callable[[], dict[str, Any]]]:
        """Create fixed local commands, never from the issue/queue manifest."""
        def repair_exactly_one() -> dict[str, Any]:
            original = self.before
            task = trusted_task(self.binding, original)
            require((root / PATH).read_bytes() == original,
                    "issue_native_base_content_drift")
            original_report = validate(original.decode("utf-8"), task)
            require(original_report["total"] == 42
                    and original_report["passed"] < 42,
                    "issue_native_no_failing_baseline")
            context: BoundIssueRAG | None = None
            if self.rag_receipt is not None:
                require(self.rag_source_reader is not None,
                        "issue_rag_reader_unavailable")
                context = bind_issue_rag(
                    self.binding, self.rag_receipt, self.rag_source_reader,
                )
                require(getattr(self.propose, "issue_rag_context", None) is None,
                        "issue_rag_context_leak")
                self.propose.issue_rag_context = context
            try:
                result = repair(root, self.state_directory, task, self.propose)
            finally:
                if context is not None:
                    self.propose.issue_rag_context = None
            require(result.get("result") == "STRUCTURED_CODE_REPAIRED"
                    and result.get("base_sha") == self.binding.base_sha
                    and result.get("produced_sha") != self.binding.base_sha
                    and result.get("path") == PATH
                    and result.get("function") == FUNCTION
                    and result.get("validation", {}).get("passed") == 42
                    and result.get("validation", {}).get("errors") == 0
                    and type(result.get("model_calls")) is int
                    and result["model_calls"] in (1, 2),
                    "issue_native_repair_not_proved")
            if self.require_native_model:
                require(getattr(self.propose, "process_verified", False) is True
                        and type(getattr(self.propose, "model_digest", None)) is str
                        and self.propose.model_digest.startswith(MODEL_DIGEST_PREFIX),
                        "issue_native_model_not_attested")
                if context is not None:
                    require(self.propose.model_digest == context.model_digest,
                            "issue_rag_model_digest_drift")
            return result

        def independently_validate() -> dict[str, Any]:
            source = (root / PATH).read_bytes()
            task = trusted_task(self.binding, self.before)
            function = extracted(source.decode("utf-8"), task)
            reconstructed = render(
                self.before.decode("utf-8"), task,
                json.dumps({"function": function}, ensure_ascii=False),
            )
            require(reconstructed.encode("utf-8") == source,
                    "issue_native_scope_changed")
            report = validate(source.decode("utf-8"), task)
            require(report == {"passed": 42, "total": 42,
                               "errors": 0, "failures": []},
                    "issue_native_validation_failed")
            regressions = self.regression_runner(root)
            require(type(regressions) is dict
                    and regressions.get("exit_code") == 0
                    and regressions.get("category") == "passed"
                    and regressions.get("output_within_limit") is True
                    and type(regressions.get("passed_count")) is int
                    and regressions["passed_count"] >= 52
                    and regressions.get("failed_count") == 0
                    and regressions.get("error_count") == 0,
                    "issue_native_regressions_unproved")
            return {"source_sha256": sha256(source), "case_total": 42,
                    "regressions_passed": regressions["passed_count"]}

        return {"issue.repair": repair_exactly_one,
                "issue.validate": independently_validate}

    def build(self, ctx: AdapterContext) -> AdapterResult:
        manifest = self._manifest(ctx)
        require(manifest.workspace_sha == self.binding.base_sha
                and manifest.before_sha256[PATH] == sha256(self.before)
                and self._workspace_file(ctx) == self.before,
                "issue_native_builder_base_drift")
        evidence = ctx.invoke("issue.repair")
        require(type(evidence) is dict
                and type(evidence.get("produced_sha")) is str
                and type(evidence.get("patch_sha256")) is str
                and len(evidence["patch_sha256"]) == 64,
                "issue_native_builder_receipt_invalid")
        return AdapterResult(
            produced_sha=evidence["produced_sha"],
            changed_paths=(PATH,), invoked_commands=tuple(ctx.invoked),
            evidence_sha256=evidence["patch_sha256"],
        )

    def validate(self, ctx: AdapterContext, produced_sha: str) -> AdapterResult:
        manifest = self._manifest(ctx)
        require(manifest.workspace_sha == produced_sha
                and produced_sha != self.binding.base_sha
                and manifest.before_sha256[PATH] == sha256(self._workspace_file(ctx)),
                "issue_native_validator_source_drift")
        # The independently materialized Validator must re-read the same
        # citation at the issue's original SHA. A Builder receipt alone is not
        # authority, and no model call is made during validation.
        if self.rag_receipt is not None:
            require(self.rag_source_reader is not None,
                    "issue_rag_reader_unavailable")
            bind_issue_rag(self.binding, self.rag_receipt,
                           self.rag_source_reader)
        observed = ctx.invoke("issue.validate")
        require(type(observed) is dict and observed.get("case_total") == 42
                and type(observed.get("source_sha256")) is str,
                "issue_native_validator_receipt_invalid")
        digest = hashlib.sha256(json.dumps(
            {"produced_sha": produced_sha, "validation": observed},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return AdapterResult(
            produced_sha=produced_sha,
            changed_paths=(PATH,), invoked_commands=tuple(ctx.invoked),
            evidence_sha256=digest,
        )
