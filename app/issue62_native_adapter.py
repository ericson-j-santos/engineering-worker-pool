"""Issue 62 native adapter: only the installed, pinned Gateway can edit code.

The Builder command must already exist in a trusted native Session Launcher
installation. Its Qwen inference and JSON sidecar are NOT produced by this
module, nor may a task provide the script, argv or receipt reader. This adapter
validates the Gateway effects, one-file function scope and model receipt before
the existing executor performs its SHA-bound CAS publication.

Physical operation requires separate Runtime Platform installation/attestation.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from app.issue62_typed_builder import ISSUE62_DESCRIPTOR
from app.native_one_shot import (
    AdapterContext, AdapterResult, NativeExecutorBlocked, TaskManifest,
)
from app.structured_ollama import MODEL

SOURCE = "app/next_action_bridge.py"
TARGET = "decide_next_action"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}\Z")


def _need(condition: bool, code: str) -> None:
    if not condition:
        raise NativeExecutorBlocked(code)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _module_scope(before: bytes, after: bytes) -> None:
    """Model may edit only one existing function, never imports or side effects."""
    _need(before != after and len(after) <= 262144,
          "issue62_source_unchanged_or_oversized")
    try:
        old = ast.parse(before.decode("utf-8"))
        new = ast.parse(after.decode("utf-8"))
    except (SyntaxError, UnicodeError, ValueError, RecursionError):
        raise NativeExecutorBlocked("issue62_source_syntax_invalid") from None
    old_f = [n for n in old.body if isinstance(n, ast.FunctionDef) and n.name == TARGET]
    new_f = [n for n in new.body if isinstance(n, ast.FunctionDef) and n.name == TARGET]
    _need(len(old_f) == len(new_f) == 1
          and len(old.body) == len(new.body),
          "issue62_function_identity_changed")
    old_fn, new_fn = old_f[0], new_f[0]
    _need(ast.dump(old_fn.args, include_attributes=False)
          == ast.dump(new_fn.args, include_attributes=False)
          and (None if old_fn.returns is None else ast.dump(
              old_fn.returns, include_attributes=False
          )) == (None if new_fn.returns is None else ast.dump(
              new_fn.returns, include_attributes=False
          ))
          and not old_fn.decorator_list
          and not new_fn.decorator_list,
          "issue62_signature_changed")
    for earlier, later in zip(old.body, new.body):
        if earlier is old_fn:
            _need(later is new_fn, "issue62_function_position_changed")
            continue
        _need(ast.dump(earlier, include_attributes=False)
              == ast.dump(later, include_attributes=False),
              "issue62_other_code_changed")
    _need(ast.dump(old_fn, include_attributes=False)
          != ast.dump(new_fn, include_attributes=False),
          "issue62_function_unchanged")

    # Only previously reviewed call targets may appear in AI-produced code.
    # Prevent import/exec/eval/dunder/nested-function or unbounded loops
    # BEFORE the generated module can be executed by an independent Validator.
    old_calls = {ast.dump(node.func, include_attributes=False)
                 for node in ast.walk(old_fn) if isinstance(node, ast.Call)}
    dangerous = (ast.Import, ast.ImportFrom, ast.For, ast.AsyncFor,
                 ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar,
                 ast.Raise, ast.Lambda, ast.Yield, ast.YieldFrom,
                 ast.Await, ast.Global, ast.Nonlocal, ast.ClassDef,
                 ast.AsyncFunctionDef)
    nodes = list(ast.walk(new_fn))
    _need(len(nodes) <= 600, "issue62_function_too_complex")
    for node in nodes:
        _need(not isinstance(node, dangerous)
              and not (isinstance(node, ast.FunctionDef) and node is not new_fn),
              "issue62_unsafe_generated_syntax")
        if isinstance(node, ast.Call):
            _need(ast.dump(node.func, include_attributes=False) in old_calls
                  and not any(isinstance(arg, ast.Starred) for arg in node.args),
                  "issue62_unapproved_call")
        if isinstance(node, (ast.Name, ast.Attribute)):
            ident = node.id if isinstance(node, ast.Name) else node.attr
            _need(not ident.startswith("__"), "issue62_dunder_forbidden")


def _governed(receipt: Any, head: str) -> None:
    _need(type(receipt) is dict
          and receipt.get("result") == "GOVERNED_COMMAND_VERIFIED"
          and receipt.get("head") == head,
          "issue62_command_receipt_unverified")


def _fixed_source(root: Path) -> bytes:
    path = root / SOURCE
    _need(path.is_file() and not path.is_symlink()
          and path.resolve().is_relative_to(root.resolve()),
          "issue62_source_path_untrusted")
    data = path.read_bytes()
    _need(0 < len(data) <= 262144, "issue62_source_size_invalid")
    return data


class Issue62NativeAdapter:
    """Use only pinned issue62.build/stage/commit and issue62.validate commands.

    Receipts must be read from a private, Gateway-allowlisted installation
    directory by a *trusted* fixed reader. A call supplied in GitHub payload
    or a task manifest is never a trusted reader. Tests use fixtures only.
    """

    descriptor = ISSUE62_DESCRIPTOR

    def __init__(
        self, *, task_id: str, original: bytes,
        model_receipt_reader: Callable[[], Mapping[str, Any]],
        validation_receipt_reader: Callable[[], Mapping[str, Any]],
        trusted_model_digest: str,
    ) -> None:
        _need(type(task_id) is str and ID.fullmatch(task_id) is not None
              and task_id.startswith("cwp-") and len(task_id) == 28
              and type(original) is bytes and 0 < len(original) <= 262144
              and callable(model_receipt_reader)
              and callable(validation_receipt_reader)
              and type(trusted_model_digest) is str
              and HEX64.fullmatch(trusted_model_digest) is not None,
              "issue62_adapter_installation_invalid")
        self.task_id = task_id
        self.original = original
        self.model_receipt_reader = model_receipt_reader
        self.validation_receipt_reader = validation_receipt_reader
        self.trusted_model_digest = trusted_model_digest
        self._verified_model_receipt: dict[str, Any] | None = None

    def _manifest(self, ctx: AdapterContext) -> TaskManifest:
        manifest = ctx.manifest
        _need(type(manifest) is TaskManifest
              and manifest.task_id == self.task_id
              and manifest.repository == "ericson-j-santos/engineering-worker-pool"
              and manifest.adapter_id == self.descriptor.adapter_id
              and manifest.adapter_contract_sha256 == self.descriptor.contract_sha256
              and set(manifest.before_sha256) == {SOURCE},
              "issue62_adapter_manifest_mismatch")
        return manifest

    def build(self, ctx: AdapterContext) -> AdapterResult:
        manifest = self._manifest(ctx)
        _need(manifest.base_sha == manifest.workspace_sha
              and manifest.before_sha256[SOURCE] == _sha(self.original)
              and _fixed_source(ctx.root) == self.original,
              "issue62_builder_base_drift")
        _governed(ctx.invoke("issue62.build"), manifest.base_sha)
        revised = _fixed_source(ctx.root)
        _module_scope(self.original, revised)
        evidence = self.model_receipt_reader()
        expected = {
            "task_id", "base_sha", "before_sha256", "after_sha256",
            "model", "model_digest", "model_reply_sha256", "model_calls",
            "process_verified", "run_id",
        }
        _need(type(evidence) is dict and set(evidence) == expected
              and evidence.get("task_id") == self.task_id
              and evidence.get("base_sha") == manifest.base_sha
              and evidence.get("before_sha256") == _sha(self.original)
              and evidence.get("after_sha256") == _sha(revised)
              and evidence.get("model") == MODEL
              and evidence.get("model_digest") == self.trusted_model_digest
              and type(evidence.get("model_reply_sha256")) is str
              and HEX64.fullmatch(evidence["model_reply_sha256"]) is not None
              and type(evidence.get("model_calls")) is int
              and 1 <= evidence["model_calls"] <= 2
              and evidence.get("process_verified") is True
              and type(evidence.get("run_id")) is str
              and ID.fullmatch(evidence["run_id"]) is not None,
              "issue62_model_receipt_unverified")
        # A Python script cannot commit its own result. The Git operations are
        # independent fixed Gateway commands with their own readback.
        _governed(ctx.invoke("issue62.stage"), manifest.base_sha)
        commit = ctx.invoke("issue62.commit")
        _need(type(commit) is dict
              and commit.get("result") == "GOVERNED_COMMAND_VERIFIED"
              and type(commit.get("head")) is str
              and HEX40.fullmatch(commit["head"]) is not None
              and commit["head"] != manifest.base_sha,
              "issue62_commit_unverified")
        _need(_fixed_source(ctx.root) == revised, "issue62_committed_source_drift")
        self._verified_model_receipt = dict(evidence)
        canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        return AdapterResult(
            produced_sha=commit["head"], changed_paths=(SOURCE,),
            invoked_commands=tuple(ctx.invoked), evidence_sha256=_sha(canonical),
        )

    def validated_model_receipt(self) -> dict[str, Any]:
        _need(type(self._verified_model_receipt) is dict,
              "issue62_model_receipt_not_available")
        return dict(self._verified_model_receipt)

    def validate(self, ctx: AdapterContext, produced_sha: str) -> AdapterResult:
        manifest = self._manifest(ctx)
        _need(type(produced_sha) is str
              and HEX40.fullmatch(produced_sha) is not None
              and produced_sha != manifest.base_sha
              and manifest.workspace_sha == produced_sha
              and manifest.before_sha256[SOURCE] == _sha(_fixed_source(ctx.root)),
              "issue62_validator_sha_drift")
        _module_scope(self.original, _fixed_source(ctx.root))
        _governed(ctx.invoke("issue62.validate"), produced_sha)
        evidence = self.validation_receipt_reader()
        _need(type(evidence) is dict
              and set(evidence) == {
                  "task_id", "produced_sha", "source_sha256",
                  "result", "passed", "failed", "skipped",
              }
              and evidence.get("task_id") == self.task_id
              and evidence.get("produced_sha") == produced_sha
              and evidence.get("source_sha256") == _sha(_fixed_source(ctx.root))
              and evidence.get("result") == "VALIDATED"
              and type(evidence.get("passed")) is int
              and evidence["passed"] >= 193
              and type(evidence.get("failed")) is int and evidence["failed"] == 0
              and type(evidence.get("skipped")) is int and evidence["skipped"] == 0,
              "issue62_validator_evidence_unverified")
        canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        return AdapterResult(
            produced_sha=produced_sha, changed_paths=(SOURCE,),
            invoked_commands=tuple(ctx.invoked), evidence_sha256=_sha(canonical),
        )
