"""Governed, exact Rules #219 manifest command binding. No runtime activation."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Any

from app.native_gateway_commands import GovernedCommand, NativeGatewayCommandRegistry
from app.native_one_shot import (
    AdapterContext, AdapterDescriptor, AdapterResult, NativeExecutorBlocked,
    TaskManifest, SHA40, LocalWorkspace, NativeOneShotExecutor,
    ReceiptStore, DurableArtifactTransport,
)

REPOSITORY = "ericson-j-santos/chatgpt-operational-rules"
RULES_HEAD = "e4f624579eb44f941c11b2cfb35ab65bfa39e8a7"
WRAPPER_PATH = "scripts/builder_manifest_stage.py"
WRAPPER_SHA256 = "422924c9bb5a409135328d41c1d5508ccd1e88da7fae02dc8beead5294ea1010"
COMMAND_KEY = "rules219.manifest.stage"
ADAPTER_ID = "rules219.builder.v1"


def make_stage_spec(*, repository: str, rules_head: str,
                    role: str, descriptor: AdapterDescriptor) -> GovernedCommand:
    """Bind a fixed Builder command; caller must independently attest inputs.

    This validates a command SHAPE, not a trusted Gateway, lease, Builder,
    model, CAS, Validator or READY_FOR_PR. Never use user/issue data as proof.
    """
    if not (type(repository) is str and repository == REPOSITORY
            and type(rules_head) is str and rules_head == RULES_HEAD
            and role == "builder"
            and type(descriptor) is AdapterDescriptor
            and descriptor.adapter_id == ADAPTER_ID
            and descriptor.roles == ("builder", "validator")
            and COMMAND_KEY in descriptor.builder_commands
            and "MANIFEST.json" in descriptor.allowed_paths
            and COMMAND_KEY not in descriptor.validator_commands):
        raise NativeExecutorBlocked("rules219_manifest_binding_not_admitted")
    descriptor.normalized()
    spec = GovernedCommand(
        key=COMMAND_KEY, role="builder",
        argv=("python", WRAPPER_PATH), risk=2,
        timeout_seconds=30, allow_dirty=True,
        allow_head_change=False, script_sha256=WRAPPER_SHA256,
    )
    spec.validate(descriptor)
    return spec


# This is an *inert* ordering helper for a future, separately admitted
# NativeAdapter.build. Only a preinstalled NativeGatewayCommandRegistry may
# supply its callables. Never invoke on a host based on issue/model text.
SOURCE_STAGE_KEY = "rules219.source.stage"
COMMIT_KEY = "rules219.builder.commit"


def _require(value: bool, reason: str) -> None:
    if not value:
        raise NativeExecutorBlocked(reason)


def _read_git(root: Path, *argv: str) -> bytes:
    """Fixed read-only Git observations, not a command-dispatch surface."""
    try:
        result = subprocess.run(
            ["git", *argv], cwd=root, check=False, capture_output=True,
            timeout=15, env={
                "PATH": os.environ.get("PATH", os.defpath),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
            },
        )
    except (OSError, subprocess.TimeoutExpired):
        raise NativeExecutorBlocked("rules219_git_readback_unavailable") from None
    _require(result.returncode == 0 and len(result.stdout) <= 4_194_304,
             "rules219_git_readback_failed")
    return result.stdout


def _names(payload: bytes) -> set[str]:
    try:
        names = [s.decode("utf-8") for s in payload.split(b"\0") if s]
    except UnicodeError:
        raise NativeExecutorBlocked("rules219_git_path_invalid") from None
    _require(len(names) == len(set(names)) and all(
        name and "\n" not in name and "\r" not in name for name in names
    ), "rules219_git_path_invalid")
    return set(names)


def _staged(root: Path) -> set[str]:
    return _names(_read_git(root, "diff", "--cached", "--name-only",
                            "--no-renames", "-z"))


def stage_manifest_then_commit(context: AdapterContext) -> AdapterResult:
    """Enforce source-stage -> typed manifest-stage -> typed commit.

    Must be called INSIDE an already claimed/fenced Builder NativeAdapter.
    Verifies Git index bytes and resulting commit; never supplies a shell
    command, bypasses Gateway, publishes a branch, or emits READY_FOR_PR.
    """
    _require(type(context) is AdapterContext
             and type(context.manifest) is TaskManifest
             and type(context.descriptor) is AdapterDescriptor,
             "rules219_context_invalid")
    descriptor = context.descriptor
    manifest = context.manifest
    descriptor.normalized()
    _require(manifest.repository == REPOSITORY
             and manifest.adapter_id == ADAPTER_ID
             and manifest.adapter_contract_sha256 == descriptor.contract_sha256
             and manifest.base_sha == manifest.workspace_sha
             and descriptor.adapter_id == ADAPTER_ID
             and descriptor.roles == ("builder", "validator")
             and all(key in descriptor.builder_commands for key in (
                 SOURCE_STAGE_KEY, COMMAND_KEY, COMMIT_KEY,
             ))
             and "rules219.validate" in descriptor.validator_commands
             and "MANIFEST.json" in descriptor.allowed_paths
             and context._allowed == descriptor.builder_commands,
             "rules219_stage_identity_invalid")
    _require(tuple(context.invoked[-1:]) == (SOURCE_STAGE_KEY,)
             and context.invoked.count(SOURCE_STAGE_KEY) == 1
             and COMMAND_KEY not in context.invoked
             and COMMIT_KEY not in context.invoked,
             "rules219_source_stage_not_attested")
    root = Path(context.root)
    _require(root.is_dir() and not root.is_symlink()
             and root.resolve() == root, "rules219_workspace_invalid")
    _require(Path(_read_git(root, "rev-parse", "--show-toplevel").decode().strip())
             .resolve() == root
             and _read_git(root, "rev-parse", "HEAD").decode().strip()
             == manifest.workspace_sha
             and _read_git(root, "remote") == b"",
             "rules219_workspace_head_invalid")
    _require(not _read_git(root, "diff", "--name-only", "-z")
             and not _read_git(root, "ls-files", "--others",
                               "--exclude-standard", "-z"),
             "rules219_source_not_cleanly_staged")
    source = _staged(root)
    _require(bool(source) and "MANIFEST.json" not in source
             and source.issubset(descriptor.allowed_paths),
             "rules219_source_scope_invalid")
    before = {item: hashlib.sha256(
        _read_git(root, "show", ":" + item)
    ).hexdigest() for item in sorted(source)}
    outcome = context.invoke(COMMAND_KEY)
    _require(type(outcome) is dict
             and outcome.get("result") == "GOVERNED_COMMAND_VERIFIED"
             and outcome.get("head") == manifest.workspace_sha
             and _read_git(root, "rev-parse", "HEAD").decode().strip()
             == manifest.workspace_sha,
             "rules219_manifest_command_unverified")
    _require(_staged(root) == source | {"MANIFEST.json"}
             and not _read_git(root, "diff", "--name-only", "-z")
             and not _read_git(root, "ls-files", "--others",
                               "--exclude-standard", "-z")
             and all(hashlib.sha256(_read_git(root, "show", ":" + path))
                     .hexdigest() == sha for path, sha in before.items()),
             "rules219_manifest_stage_scope_drift")
    staged_manifest = _read_git(root, "show", ":MANIFEST.json")
    manifest_file = root / "MANIFEST.json"
    _require(manifest_file.is_file() and not manifest_file.is_symlink()
             and manifest_file.stat().st_size <= 4_194_304
             and staged_manifest == manifest_file.read_bytes(),
             "rules219_manifest_index_drift")
    try:
        data = json.loads(staged_manifest)
        entries = {entry["path"]: entry for entry in data["files"]}
        _require(type(data["files"]) is list
                 and len(entries) == len(data["files"])
                 and all(entries[path]["sha256"] == digest and
                         entries[path]["size"] == len(_read_git(root, "show", ":" + path))
                         for path, digest in before.items()),
                 "rules219_manifest_content_drift")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise NativeExecutorBlocked("rules219_manifest_content_drift") from None
    committed = context.invoke(COMMIT_KEY)
    produced = (committed.get("head") if type(committed) is dict else None)
    _require(type(committed) is dict
             and committed.get("result") == "GOVERNED_COMMAND_VERIFIED"
             and type(produced) is str and bool(SHA40.fullmatch(produced))
             and produced != manifest.workspace_sha
             and _read_git(root, "rev-parse", "HEAD").decode().strip() == produced
             and not _read_git(root, "status", "--porcelain",
                               "--untracked-files=all"),
             "rules219_commit_unverified")
    changes = _names(_read_git(
        root, "diff", "--no-renames", "--name-only", "-z",
        manifest.workspace_sha, produced,
    ))
    _require(changes == source | {"MANIFEST.json"},
             "rules219_commit_scope_drift")
    local_evidence = json.dumps({
        "produced_sha": produced,
        "base_sha": manifest.workspace_sha,
        "manifest_sha256": hashlib.sha256(staged_manifest).hexdigest(),
        "source_index_sha256": before,
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return AdapterResult(
        produced_sha=produced,
        changed_paths=tuple(sorted(changes)),
        invoked_commands=tuple(context.invoked),
        evidence_sha256=hashlib.sha256(local_evidence).hexdigest(),
    )


# Native profile for ONE independently admitted source/test increment. The
# three supporting scripts are NOT currently installed in Rules #219. An
# installed Runtime Platform must pin their actual bytes and authorize Risk2
# before any call. This module cannot turn a synthetic receipt into a real
# model invocation.
MODEL_PROPOSE_KEY = "rules219.model.propose"
MODEL_APPLY_KEY = "rules219.model.apply"
VALIDATOR_KEY = "rules219.validate"
SOURCE_PATH = "tests/test_generate_manifest_builder.py"
MODEL_NAME = "qwen2.5-coder:7b"
PROPOSE_SCRIPT = "scripts/rules219_model_propose.py"
APPLY_SCRIPT = "scripts/rules219_model_apply.py"
VALIDATOR_SCRIPT = "scripts/rules219_validator.py"

RULES219_NATIVE_DESCRIPTOR = AdapterDescriptor(
    adapter_id=ADAPTER_ID,
    allowed_paths=("MANIFEST.json", WRAPPER_PATH, SOURCE_PATH),
    builder_commands=(
        COMMIT_KEY, COMMAND_KEY, MODEL_APPLY_KEY, MODEL_PROPOSE_KEY,
        SOURCE_STAGE_KEY,
    ),
    validator_commands=(VALIDATOR_KEY,),
)


def fixed_rules219_native_specs(
    *, proposer_sha256: str, applier_sha256: str, validator_sha256: str,
) -> dict[str, GovernedCommand]:
    """Fixed command catalog; digests originate at a trusted installation.

    Refuse unset/untrusted digest shape; every script is checked again by the
    already-existing Gateway registry at invocation. No issue/task argv.
    """
    digests = (proposer_sha256, applier_sha256, validator_sha256)
    _require(all(type(d) is str and len(d) == 64 and
                 all(c in "0123456789abcdef" for c in d) for d in digests),
             "rules219_installed_digests_required")
    specs = {
        MODEL_PROPOSE_KEY: GovernedCommand(
            MODEL_PROPOSE_KEY, "builder", ("python", PROPOSE_SCRIPT), 2,
            timeout_seconds=120, script_sha256=proposer_sha256,
        ),
        MODEL_APPLY_KEY: GovernedCommand(
            MODEL_APPLY_KEY, "builder", ("python", APPLY_SCRIPT), 2,
            timeout_seconds=30, script_sha256=applier_sha256,
        ),
        SOURCE_STAGE_KEY: GovernedCommand(
            SOURCE_STAGE_KEY, "builder", ("git", "add", "--", SOURCE_PATH), 2,
            allow_dirty=True,
        ),
        COMMAND_KEY: make_stage_spec(
            repository=REPOSITORY, rules_head=RULES_HEAD, role="builder",
            descriptor=RULES219_NATIVE_DESCRIPTOR,
        ),
        COMMIT_KEY: GovernedCommand(
            COMMIT_KEY, "builder",
            ("git", "commit", "-m", "test(rules219): model-generated regression"),
            2, allow_dirty=True, allow_head_change=True,
        ),
        VALIDATOR_KEY: GovernedCommand(
            VALIDATOR_KEY, "validator", ("python", VALIDATOR_SCRIPT), 1,
            timeout_seconds=120, script_sha256=validator_sha256,
        ),
    }
    _require(set(specs) == set(RULES219_NATIVE_DESCRIPTOR.command_keys),
             "rules219_command_catalog_incomplete")
    for spec in specs.values():
        spec.validate(RULES219_NATIVE_DESCRIPTOR)
    return specs


def _command_ok(result: Any, expected_head: str) -> bool:
    return type(result) is dict and result.get("result") == "GOVERNED_COMMAND_VERIFIED" and result.get("head") == expected_head


def _sha_file(root: Path, relative: str) -> str:
    target = root / relative
    _require(target.is_file() and not target.is_symlink()
             and target.resolve().is_relative_to(root)
             and target.stat().st_size < 300000,
             "rules219_source_file_untrusted")
    return hashlib.sha256(target.read_bytes()).hexdigest()


class Rules219NativeBuilderAdapter:
    """NativeOneShot-owned Builder/Validator; no alternate queue or runner.

    The real model/validator receipt readers MUST be installed trusted readers.
    They are invoked only after verified Gateway commands, not from a
    synthetic issue comment or the generated code itself.
    """
    descriptor = RULES219_NATIVE_DESCRIPTOR

    def __init__(
        self, *, expected_model_digest: str,
        model_receipt_reader: Callable[[], Mapping[str, Any]],
        validator_receipt_reader: Callable[[], Mapping[str, Any]],
    ) -> None:
        _require(type(expected_model_digest) is str
                 and len(expected_model_digest) == 64
                 and all(c in "0123456789abcdef"
                         for c in expected_model_digest),
                 "rules219_model_digest_not_pinned")
        _require(callable(model_receipt_reader)
                 and callable(validator_receipt_reader),
                 "rules219_receipt_reader_required")
        self.expected_model_digest = expected_model_digest
        self.model_receipt_reader = model_receipt_reader
        self.validator_receipt_reader = validator_receipt_reader

    @staticmethod
    def _identity(ctx: AdapterContext, role: str) -> TaskManifest:
        _require(type(ctx) is AdapterContext
                 and type(ctx.manifest) is TaskManifest
                 and ctx.manifest.repository == REPOSITORY
                 and ctx.manifest.base_sha == RULES_HEAD
                 and ctx.manifest.adapter_id == ADAPTER_ID
                 and ctx.manifest.adapter_contract_sha256
                 == RULES219_NATIVE_DESCRIPTOR.contract_sha256
                 and ctx.descriptor == RULES219_NATIVE_DESCRIPTOR
                 and ctx._allowed == ctx.descriptor.commands_for(role),
                 "rules219_native_identity_not_attested")
        root = Path(ctx.root)
        _require(root.is_dir() and not root.is_symlink()
                 and root.resolve() == root
                 and _read_git(root, "remote") == b"",
                 "rules219_native_workspace_invalid")
        return ctx.manifest

    def build(self, ctx: AdapterContext) -> AdapterResult:
        manifest = self._identity(ctx, "builder")
        _require(manifest.workspace_sha == RULES_HEAD
                 and _read_git(Path(ctx.root), "rev-parse", "HEAD").decode().strip()
                 == RULES_HEAD,
                 "rules219_native_builder_base_drift")
        root = Path(ctx.root)
        source_before = _sha_file(root, SOURCE_PATH)
        _require(_read_git(root, "status", "--porcelain",
                           "--untracked-files=all") == b"",
                 "rules219_native_builder_workspace_dirty")
        proposed = ctx.invoke(MODEL_PROPOSE_KEY)
        _require(_command_ok(proposed, RULES_HEAD)
                 and _sha_file(root, SOURCE_PATH) == source_before
                 and _read_git(root, "status", "--porcelain",
                               "--untracked-files=all") == b"",
                 "rules219_model_proposal_unverified")
        receipt = self.model_receipt_reader()
        _require(type(receipt) is dict
                 and receipt.get("task_id") == manifest.task_id
                 and receipt.get("source_sha") == RULES_HEAD
                 and receipt.get("source_path") == SOURCE_PATH
                 and receipt.get("model") == MODEL_NAME
                 and receipt.get("digest") == self.expected_model_digest
                 and receipt.get("process_verified") is True
                 and receipt.get("origin") == "http://127.0.0.1:11434"
                 and type(receipt.get("model_calls")) is int
                 and 1 <= receipt["model_calls"] <= 2
                 and type(receipt.get("before_sha256")) is str
                 and receipt["before_sha256"] == source_before
                 and type(receipt.get("after_sha256")) is str
                 and len(receipt["after_sha256"]) == 64
                 and all(c in "0123456789abcdef"
                         for c in receipt["after_sha256"])
                 and receipt["after_sha256"] != source_before,
                 "rules219_model_provenance_unverified")
        applied = ctx.invoke(MODEL_APPLY_KEY)
        _require(_command_ok(applied, RULES_HEAD)
                 and _sha_file(root, SOURCE_PATH)
                 == receipt["after_sha256"]
                 and _read_git(root, "diff", "--cached", "--name-only",
                               "-z") == b""
                 and _names(_read_git(root, "diff", "--name-only", "-z"))
                 == {SOURCE_PATH}
                 and _read_git(root, "ls-files", "--others",
                               "--exclude-standard", "-z") == b"",
                 "rules219_model_patch_unverified")
        staged = ctx.invoke(SOURCE_STAGE_KEY)
        _require(_command_ok(staged, RULES_HEAD),
                 "rules219_source_stage_gateway_unverified")
        result = stage_manifest_then_commit(ctx)
        _require(result.changed_paths == ("MANIFEST.json", SOURCE_PATH)
                 and tuple(ctx.invoked) == (
                     MODEL_PROPOSE_KEY, MODEL_APPLY_KEY, SOURCE_STAGE_KEY,
                     COMMAND_KEY, COMMIT_KEY,
                 ), "rules219_native_builder_result_invalid")
        return result

    def validate(self, ctx: AdapterContext, produced_sha: str) -> AdapterResult:
        manifest = self._identity(ctx, "validator")
        _require(type(produced_sha) is str and SHA40.fullmatch(produced_sha)
                 and produced_sha != RULES_HEAD
                 and manifest.workspace_sha == produced_sha
                 and LocalWorkspace(ctx.root).head() == produced_sha
                 and LocalWorkspace(ctx.root).status() == "",
                 "rules219_native_validator_checkout_invalid")
        outcome = ctx.invoke(VALIDATOR_KEY)
        _require(_command_ok(outcome, produced_sha),
                 "rules219_validator_command_unverified")
        report = self.validator_receipt_reader()
        _require(type(report) is dict
                 and report.get("result") == "INDEPENDENT_TESTS_PASSED"
                 and report.get("produced_sha") == produced_sha
                 and report.get("source_sha") == RULES_HEAD
                 and report.get("validator_role") == "validator"
                 and type(report.get("tests_passed")) is int
                 and report["tests_passed"] > 0
                 and report.get("tests_failed") == 0
                 and report.get("negative_control_passed") is True
                 and report.get("idempotency_passed") is True,
                 "rules219_independent_validator_unverified")
        changes = LocalWorkspace(ctx.root).changed_paths(RULES_HEAD, produced_sha)
        _require(changes == ("MANIFEST.json", SOURCE_PATH),
                 "rules219_native_validator_scope_drift")
        evidence = json.dumps({
            "produced_sha": produced_sha, "report": report,
        }, sort_keys=True, separators=(",", ":")).encode()
        return AdapterResult(
            produced_sha, changes, tuple(ctx.invoked),
            hashlib.sha256(evidence).hexdigest(),
        )


def bind_rules219_native_executor(
    *, registry: NativeGatewayCommandRegistry,
    adapter: Rules219NativeBuilderAdapter,
    api: Any, workspace: LocalWorkspace, receipts: ReceiptStore,
    artifact_transport: DurableArtifactTransport,
    role: str,
) -> NativeOneShotExecutor:
    """Compose the EXISTING fenced executor, not an independent dispatcher.

    Caller must prove installed Gateway/session, worker identity, durable CAS,
    source content, cost and host policy. This function does NOT call run().
    """
    _require(type(registry) is NativeGatewayCommandRegistry
             and type(adapter) is Rules219NativeBuilderAdapter
             and type(workspace) is LocalWorkspace
             and type(receipts) is ReceiptStore
             and role in ("builder", "validator")
             and artifact_transport is not None
             and all(callable(getattr(artifact_transport, key, None))
                     for key in ("preflight", "publish", "verify",
                                 "materialize")),
             "rules219_executor_dependencies_unattested")
    registry.boundary.validate()
    _require(registry.boundary.expected_head
             == (RULES_HEAD if role == "builder"
                 else workspace.head())
             and registry.workspace.resolve() == workspace.root
             and set(registry.specs)
             == set(RULES219_NATIVE_DESCRIPTOR.command_keys)
             and all(registry.specs[key].validate(
                 RULES219_NATIVE_DESCRIPTOR) is None
                 for key in registry.specs),
             "rules219_native_registry_identity_invalid")
    callbacks = registry.for_role(RULES219_NATIVE_DESCRIPTOR, role)
    return NativeOneShotExecutor(
        api=api, workspace=workspace, receipts=receipts,
        adapters={ADAPTER_ID: adapter},
        command_registries={ADAPTER_ID: callbacks},
        boundary=registry.boundary, artifact_transport=artifact_transport,
        fenced_adapter_ids=frozenset({ADAPTER_ID}),
    )
