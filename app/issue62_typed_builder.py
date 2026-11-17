"""Typed Builder entrypoint for issue 62, driven ONLY by installed native authority.

This library does not install workers, create tasks, accept arbitrary commands,
fetch credentials from an event, or certify that a physical host is ready.
Its caller must be the pinned native Runtime Platform installation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.native_gateway_commands import GovernedCommand, NativeGatewayCommandRegistry
from app.native_gateway_preflight import attest_execution_boundary
from app.native_one_shot import (
    AdapterDescriptor, LocalWorkspace, NativeExecutorBlocked, ReceiptStore,
    TaskManifest,
)
from app.native_pool_http import LocalWorkerApi
from app.next_action_bridge import continue_with_attested_native
from app.store import identity, task_id as canonical_task_id
from app.structured_ollama import MODEL

HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")

# Issue 62 is a scoped Worker Pool feature. The existing Qwen adapter
# is bound to ReqSys bridge.property.v1 and must not be reused here.
ISSUE62_DESCRIPTOR = AdapterDescriptor(
    adapter_id="issue62.next-action.v1",
    allowed_paths=("app/next_action_bridge.py",),
    builder_commands=("issue62.build", "issue62.commit", "issue62.stage"),
    validator_commands=("issue62.validate",),
)



def fixed_issue62_gateway_specs(
    *, trusted_builder_sha256: str, trusted_validator_sha256: str,
) -> dict[str, GovernedCommand]:
    """Declare the four fixed #62 commands for an ALREADY trusted installation.

    The trusted Runtime Platform must obtain both digests from its pinned,
    independently verified installation manifest, never a task, GitHub event,
    model response or environment flag. This declaration does not install a
    script, grant Gateway access, attest cost or execute anything. The existing
    registry verifies the script bytes and the real Gateway before each call.
    """
    need(
        type(trusted_builder_sha256) is str
        and HEX64.fullmatch(trusted_builder_sha256) is not None
        and type(trusted_validator_sha256) is str
        and HEX64.fullmatch(trusted_validator_sha256) is not None,
        "issue62_fixed_script_digest_required",
    )
    specs = {
        "issue62.build": GovernedCommand(
            key="issue62.build", role="builder",
            argv=("python", "scripts/issue62_builder.py"), risk=2,
            timeout_seconds=120, script_sha256=trusted_builder_sha256,
        ),
        "issue62.stage": GovernedCommand(
            key="issue62.stage", role="builder",
            argv=("git", "add", "--", "app/next_action_bridge.py"), risk=2,
            timeout_seconds=30,
        ),
        "issue62.commit": GovernedCommand(
            key="issue62.commit", role="builder",
            argv=("git", "commit", "-m", "feat(issue62): advance next action"),
            risk=2, timeout_seconds=30, allow_dirty=True,
            allow_head_change=True,
        ),
        "issue62.validate": GovernedCommand(
            key="issue62.validate", role="validator",
            argv=("python", "scripts/issue62_validator.py"), risk=1,
            timeout_seconds=120, script_sha256=trusted_validator_sha256,
        ),
    }
    need(set(specs) == set(ISSUE62_DESCRIPTOR.command_keys),
         "issue62_fixed_command_set_mismatch")
    for command in specs.values():
        command.validate(ISSUE62_DESCRIPTOR)
    return specs


def need(ok: bool, code: str) -> None:
    if not ok:
        raise NativeExecutorBlocked(code)


@dataclass(frozen=True)
class TypedBuilderAction:
    """Task identity supplied by authenticated control plane, not the model."""
    task_id: str
    repository: str
    issue_number: int
    request_id: str
    idempotency_key: str
    correlation_id: str
    todo_id: str
    base_sha: str
    rules_sha: str

    def validate(self) -> None:
        need(self.repository == "ericson-j-santos/engineering-worker-pool"
             and type(self.issue_number) is int and self.issue_number == 62
             and type(self.todo_id) is str and self.todo_id == "TODO-69"
             and type(self.request_id) is str and 1 <= len(self.request_id) <= 128
             and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", self.request_id) is not None
             and type(self.correlation_id) is str
             and re.fullmatch(r"[A-Za-z0-9._:-]{3,128}", self.correlation_id) is not None
             and type(self.base_sha) is str and HEX40.fullmatch(self.base_sha) is not None
             and type(self.rules_sha) is str and HEX40.fullmatch(self.rules_sha) is not None
             and type(self.idempotency_key) is str
             and HEX64.fullmatch(self.idempotency_key) is not None,
             "typed_builder_identity_invalid")
        key = identity(self.repository, self.issue_number, self.request_id)
        need(self.idempotency_key == key
             and self.task_id == canonical_task_id(key),
             "typed_builder_idempotency_invalid")


def execute_installed_builder_once(
    *, action: TypedBuilderAction, api: LocalWorkerApi,
    registry: NativeGatewayCommandRegistry, workspace: LocalWorkspace,
    receipts: ReceiptStore, adapter: Any, artifact_transport: Any,
    manifest: dict[str, Any], worker_id: str, installed_rules_sha: str,
    installed_model_digest: str,
    cost_preflight: Callable[[TypedBuilderAction], None],
) -> dict[str, Any]:
    """Exactly one native Builder stage, then independent task/CAS readback.

    A real native model invocation is required before reporting model evidence.
    A callable cost preflight MUST be supplied by trusted host configuration;
    this module does not validate the origin of injected objects itself.
    The existing Gateway and NativeOneShotExecutor remain the authority for
    session/commands/lease/commit/CAS and refuse unsafe recovery.
    """
    need(type(action) is TypedBuilderAction
         and type(api) is LocalWorkerApi
         and type(registry) is NativeGatewayCommandRegistry
         and type(workspace) is LocalWorkspace
         and type(receipts) is ReceiptStore
         and type(manifest) is dict
         and callable(cost_preflight), "typed_builder_installation_missing")
    action.validate()
    # Reject incompatible or incomplete adapter/manifest BEFORE cost checks,
    # Gateway inspection, task reads, lease acquisition or Qwen inference.
    parsed = TaskManifest.parse(manifest)
    descriptor = getattr(adapter, "descriptor", None)
    need(type(descriptor) is AdapterDescriptor
         and descriptor == ISSUE62_DESCRIPTOR
         and parsed.task_id == action.task_id
         and parsed.repository == action.repository
         and parsed.base_sha == action.base_sha
         and parsed.workspace_sha == action.base_sha
         and parsed.adapter_id == descriptor.adapter_id
         and parsed.adapter_contract_sha256 == descriptor.contract_sha256
         and set(parsed.before_sha256) == set(descriptor.allowed_paths)
         and callable(getattr(adapter, "build", None))
         and callable(getattr(adapter, "validate", None)),
         "typed_builder_adapter_profile_incompatible")
    from app.issue62_native_adapter import Issue62NativeAdapter
    need(type(adapter) is Issue62NativeAdapter
         and adapter.trusted_model_digest == installed_model_digest
         and type(installed_rules_sha) is str
         and installed_rules_sha == action.rules_sha
         and type(installed_model_digest) is str
         and HEX64.fullmatch(installed_model_digest) is not None
         and type(worker_id) is str and bool(re.fullmatch(r"[a-z][a-z0-9_.-]{2,127}", worker_id))
         and registry.correlation_id == action.correlation_id
         and registry.boundary.expected_head == action.base_sha
         and manifest.get("task_id") == action.task_id
         and manifest.get("repository") == action.repository
         and manifest.get("base_sha") == action.base_sha
         and manifest.get("workspace_sha") == action.base_sha,
         "typed_builder_binding_mismatch")
    need(adapter._verified_model_receipt is None,
         "typed_builder_model_not_fresh")
    # No event boolean can attest budget. The installed cost authority must
    # independently fail closed (raise) on unknown cost or any overage.
    need(cost_preflight(action) is None, "typed_builder_cost_preflight_invalid")
    boundary = attest_execution_boundary(
        gateway=registry.gateway, policy_path=registry.policy_path,
        workspace=registry.workspace, session_id=registry.session_id,
        correlation_id=registry.correlation_id,
        expected_head=registry.boundary.expected_head,
        trusted_gateway_sha256=registry.trusted_gateway_sha256,
        trusted_policy_sha256=registry.trusted_policy_sha256,
    )
    need(boundary == registry.boundary, "typed_builder_session_changed")
    task = api.get_task(action.task_id)
    need(type(task) is dict
         and all(task.get(k) == getattr(action, k) for k in (
             "task_id", "repository", "issue_number", "request_id",
             "idempotency_key", "correlation_id", "base_sha"))
         and task.get("fencing_required") is True,
         "typed_builder_canonical_identity_drift")

    outcome = continue_with_attested_native(
        api, registry=registry, workspace=workspace, receipts=receipts,
        adapter=adapter, artifact_transport=artifact_transport,
        manifest=manifest, role="builder", worker_id=worker_id,
        task_id=action.task_id, expected_repository=action.repository,
        expected_issue=action.issue_number,
        expected_request_id=action.request_id, expected_base_sha=action.base_sha,
    )
    # Waiting, blocked and replay outcomes are never evidence of model execution.
    if outcome.get("executed") is not True:
        need(outcome.get("state") in {"wait", "blocked", "done"}
             and outcome.get("task_id") == action.task_id,
             "typed_builder_noop_untrusted")
        return {"result": "BUILDER_NOT_INVOKED", "task_id": action.task_id,
                "state": outcome["state"], "executed": False}

    model_receipt = adapter.validated_model_receipt()
    need(outcome.get("state") == "validator"
         and outcome.get("executed_role") == "builder"
         and model_receipt.get("model") == MODEL
         and model_receipt.get("model_digest") == installed_model_digest
         and type(model_receipt.get("model_reply_sha256")) is str
         and HEX64.fullmatch(model_receipt["model_reply_sha256"]) is not None,
         "typed_builder_model_proof_missing")
    lock, receipt_path = receipts.paths(action.task_id, "builder")
    need(not receipt_path.is_symlink() and receipt_path.is_file()
         and receipt_path.stat().st_size <= 8192
         and not lock.exists(), "typed_builder_receipt_unavailable")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise NativeExecutorBlocked("typed_builder_receipt_invalid") from None
    task = api.get_task(action.task_id)
    checkpoint = api.get_latest_checkpoint(action.task_id)
    need(type(receipt) is dict and type(task) is dict
         and type(checkpoint) is dict
         and receipt.get("schema_version") == 2
         and receipt.get("result") == "NATIVE_ONE_SHOT_BUILT"
         and receipt.get("task_id") == action.task_id
         and receipt.get("worker_id") == worker_id
         and receipt.get("session_id") == registry.session_id
         and receipt.get("correlation_id") == action.correlation_id
         and receipt.get("gateway_verified") is True
         and receipt.get("replayed") is False
         and receipt.get("fencing_required") is True
         and type(receipt.get("attempt_id")) is str
         and receipt["attempt_id"] == checkpoint.get("attempt_id")
         and receipt["attempt_id"] == model_receipt.get("run_id")
         and receipt.get("lease_generation") == checkpoint.get("lease_generation")
         and receipt.get("artifact_ref") == checkpoint.get("artifact_ref")
         and receipt.get("artifact_sha256") == checkpoint.get("artifact_sha256")
         and type(receipt.get("artifact_sha256")) is str
         and HEX64.fullmatch(receipt["artifact_sha256"]) is not None
         and checkpoint.get("stage") == "builder_result"
         and checkpoint.get("effect_state") == "confirmed"
         and checkpoint.get("next_action") == "submit_validation"
         and task.get("state") == "validating"
         and task.get("builder_worker_id") == worker_id
         and task.get("produced_sha") == receipt.get("produced_sha")
         and type(receipt.get("produced_sha")) is str
         and HEX40.fullmatch(receipt["produced_sha"]) is not None
         and receipt["produced_sha"] != action.base_sha,
         "typed_builder_cas_readback_unverified")
    return {
        "result": "TYPED_NATIVE_BUILDER_CONFIRMED",
        "task_id": action.task_id,
        "native_run_id": receipt["attempt_id"],
        "model": MODEL, "model_digest": model_receipt["model_digest"],
        "model_reply_sha256": model_receipt["model_reply_sha256"],
        "produced_sha": receipt["produced_sha"],
        "artifact_ref": receipt["artifact_ref"],
        "artifact_sha256": receipt["artifact_sha256"],
        "session_id": registry.session_id,
        "validator_pending": True,
    }
