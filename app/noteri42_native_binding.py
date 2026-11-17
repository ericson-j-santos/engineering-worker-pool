"""Typed Noteri #42 consumer of the existing fenced NativeOneShotExecutor.

Preparatory engineering binding only. It never starts a task or installs an adapter.
Runtime commands, model receipts and baseline evidence must come from a separately
admitted, trusted controller/Gateway. Synthetic tests cannot authorize a host.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Mapping

from app.native_gateway_commands import GovernedCommand, NativeGatewayCommandRegistry
from app.native_one_shot import (
    AdapterContext, AdapterDescriptor, AdapterResult, DurableArtifactTransport,
    LocalWorkspace, NativeExecutorBlocked, NativeOneShotExecutor, ReceiptStore,
)

ISSUE_NUMBER = 42
REPOSITORY = "ericson-j-santos/noteri-runtime"
BASE_SHA = "a9387bcdf27150bd7e678df8162de72a9a3138c6"
SOURCE_PATH = "scripts/noteri_worker_pool_fence_sink.py"
ADAPTER_ID = "noteri42.sink-fence.v1"
COMMIT_KEY = "noteri42.builder.commit"
MODEL_KEY = "noteri42.builder.model"
STAGE_KEY = "noteri42.builder.stage"
VALIDATOR_KEY = "noteri42.validator.check"
MODEL_SCRIPT = "scripts/noteri42_model_repair.py"
VALIDATOR_SCRIPT = "scripts/noteri42_independent_validate.py"
COMMIT_MESSAGE = "fix(noteri42): model-repaired sink fencing"
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")

DESCRIPTOR = AdapterDescriptor(
    adapter_id=ADAPTER_ID,
    allowed_paths=(SOURCE_PATH,),
    builder_commands=(COMMIT_KEY, MODEL_KEY, STAGE_KEY),
    validator_commands=(VALIDATOR_KEY,),
)


def _need(ok: bool, reason: str) -> None:
    if not ok:
        raise NativeExecutorBlocked(reason)


def _valid_specs(registry: NativeGatewayCommandRegistry) -> None:
    _need(type(registry.specs) is dict
          and set(registry.specs) == set(DESCRIPTOR.command_keys),
          "noteri42_registry_catalog_drift")
    expected = {
        MODEL_KEY: ("builder", ("python", MODEL_SCRIPT), 2, False, False),
        STAGE_KEY: ("builder", ("git", "add", "--", SOURCE_PATH), 2, True, False),
        COMMIT_KEY: ("builder", ("git", "commit", "-m", COMMIT_MESSAGE), 2, True, True),
        VALIDATOR_KEY: ("validator", ("python", VALIDATOR_SCRIPT), 1, False, False),
    }
    for key, (role, argv, risk, dirty, head_change) in expected.items():
        spec = registry.specs[key]
        _need(type(spec) is GovernedCommand
              and (spec.role, spec.argv, spec.risk, spec.allow_dirty,
                   spec.allow_head_change) == (role, argv, risk, dirty, head_change),
              "noteri42_command_drift")
        spec.validate(DESCRIPTOR)
        if key in (MODEL_KEY, VALIDATOR_KEY):
            _need(type(spec.script_sha256) is str
                  and bool(SHA256.fullmatch(spec.script_sha256))
                  and spec.script_sha256 != "0" * 64,
                  "noteri42_script_not_pinned")


def _baseline(readback: Mapping[str, Any]) -> None:
    # This is a contract check, NOT authentication of the evidence source.
    # The controller must independently read the original failing test/log.
    _need(type(readback) is dict
          and readback.get("issue_number") == ISSUE_NUMBER
          and readback.get("repository") == REPOSITORY
          and readback.get("base_sha") == BASE_SHA
          and readback.get("result") == "reproduced_failure"
          and readback.get("independent_readback") is True
          and type(readback.get("test_id")) is str
          and 1 <= len(readback["test_id"]) <= 160
          and type(readback.get("evidence_sha256")) is str
          and bool(SHA256.fullmatch(readback["evidence_sha256"])),
          "noteri42_real_baseline_missing")


class _ScopedAdapter:
    descriptor = DESCRIPTOR

    def __init__(self, original: Any, task_id: str):
        self._original, self._task_id = original, task_id

    def _identity(self, context: AdapterContext, role: str) -> None:
        _need(type(context) is AdapterContext
              and context.descriptor == DESCRIPTOR
              and context.manifest.adapter_id == ADAPTER_ID
              and context.manifest.adapter_contract_sha256 == DESCRIPTOR.contract_sha256
              and context.manifest.task_id == self._task_id
              and context.manifest.repository == REPOSITORY
              and context.manifest.base_sha == BASE_SHA
              and set(context.manifest.before_sha256) == {SOURCE_PATH}
              and context._allowed == DESCRIPTOR.commands_for(role),
              "noteri42_task_scope_drift")

    def build(self, context: AdapterContext) -> AdapterResult:
        self._identity(context, "builder")
        _need(context.manifest.workspace_sha == BASE_SHA,
              "noteri42_builder_base_drift")
        result = self._original.build(context)
        _need(type(result) is AdapterResult
              and result.produced_sha != BASE_SHA
              and bool(SHA40.fullmatch(result.produced_sha))
              and result.changed_paths == (SOURCE_PATH,)
              and result.invoked_commands == (MODEL_KEY, STAGE_KEY, COMMIT_KEY)
              and tuple(context.invoked) == result.invoked_commands
              and bool(SHA256.fullmatch(result.evidence_sha256)),
              "noteri42_builder_receipt_unverified")
        return result

    def validate(self, context: AdapterContext, produced_sha: str) -> AdapterResult:
        self._identity(context, "validator")
        _need(type(produced_sha) is str and bool(SHA40.fullmatch(produced_sha))
              and produced_sha != BASE_SHA
              and context.manifest.workspace_sha == produced_sha,
              "noteri42_validator_sha_drift")
        result = self._original.validate(context, produced_sha)
        _need(type(result) is AdapterResult
              and result.produced_sha == produced_sha
              and result.changed_paths == (SOURCE_PATH,)
              and result.invoked_commands == (VALIDATOR_KEY,)
              and tuple(context.invoked) == result.invoked_commands
              and bool(SHA256.fullmatch(result.evidence_sha256)),
              "noteri42_validator_receipt_unverified")
        return result


def bind_noteri42_executor(
    *, registry: NativeGatewayCommandRegistry, installed_adapter: Any,
    api: Any, task_id: str, baseline_reader: Callable[[], Mapping[str, Any]],
    workspace: LocalWorkspace, receipts: ReceiptStore,
    artifact_transport: DurableArtifactTransport, role: str,
) -> NativeOneShotExecutor:
    """Bind existing fenced pool, without invoking Builder or claiming a task.

    The caller MUST authenticate source issue, trusted baseline/model/Validator
    receipt readers, installed scripts and their SHA256, cost-zero and host lease.
    No arbitrary issue text can create a command or grant runtime authority.
    """
    _need(isinstance(registry, NativeGatewayCommandRegistry)
          and type(workspace) is LocalWorkspace
          and type(receipts) is ReceiptStore
          and role in ("builder", "validator")
          and type(task_id) is str and bool(re.fullmatch(r"cwp-[0-9a-f]{24}", task_id))
          and callable(baseline_reader)
          and type(getattr(installed_adapter, "descriptor", None)) is AdapterDescriptor
          and installed_adapter.descriptor == DESCRIPTOR
          and callable(getattr(installed_adapter, "build", None))
          and callable(getattr(installed_adapter, "validate", None))
          and artifact_transport is not None
          and all(callable(getattr(artifact_transport, key, None))
                  for key in ("preflight", "publish", "verify", "materialize")),
          "noteri42_binding_dependencies_unattested")
    _need(callable(getattr(api, "get_task", None)),
          "noteri42_canonical_task_unavailable")
    task = api.get_task(task_id)  # READ ONLY: canonical authority, not issue prose.
    _need(type(task) is dict
          and task.get("task_id") == task_id
          and task.get("repository") == REPOSITORY
          and task.get("base_sha") == BASE_SHA
          and task.get("issue_number") == ISSUE_NUMBER
          and task.get("fencing_required") is True
          and type(task.get("request_id")) is str and bool(task["request_id"])
          and type(task.get("idempotency_key")) is str and bool(task["idempotency_key"]),
          "noteri42_canonical_task_not_admitted")
    _baseline(baseline_reader())  # Does not itself authenticate the reader.
    registry.boundary.validate()
    _need(registry.boundary.expected_head
          == (BASE_SHA if role == "builder" else workspace.head())
          and Path(registry.workspace).resolve() == workspace.root,
          "noteri42_session_workspace_drift")
    _valid_specs(registry)
    callbacks = registry.for_role(DESCRIPTOR, role)
    return NativeOneShotExecutor(
        api=api, workspace=workspace, receipts=receipts,
        adapters={ADAPTER_ID: _ScopedAdapter(installed_adapter, task_id)},
        command_registries={ADAPTER_ID: callbacks}, boundary=registry.boundary,
        artifact_transport=artifact_transport,
        fenced_adapter_ids=frozenset({ADAPTER_ID}),
    )
