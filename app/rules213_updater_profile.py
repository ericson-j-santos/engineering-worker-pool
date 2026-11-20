"""Pre-admission contract for Rules #213 updater repair.

This is NOT a runnable Builder, attestation of a model, CAS or READY_FOR_PR.
Only the existing Worker Pool claim/lease/Validator may perform such transitions.
No endpoints, commands, host permissions or workflows are configured here.
"""
from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.native_one_shot import AdapterDescriptor, TaskManifest

ISSUE = 213
REPOSITORY = "ericson-j-santos/chatgpt-operational-rules"
BASE_SHA = "58dcd4200f412a866278027ba9e433439a03f3b3"
PROFILE = "rules213.updater.repair.v1"
MODEL = "qwen2.5-coder:7b"
MODEL_DIGEST_PREFIX = "dae161e27b0e"
SOURCE_FUNCTIONS = {
    "scripts/install_command_gateway_host.py": "install_bundle",
    "scripts/update_command_gateway_host.py": "preflight_runtime_replaceability",
}
DESCRIPTOR = AdapterDescriptor(
    adapter_id=PROFILE,
    allowed_paths=tuple(sorted(SOURCE_FUNCTIONS)),
    builder_commands=("rules213.repair",),
    validator_commands=("rules213.validate",),
)
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA64 = re.compile(r"[0-9a-f]{64}\Z")


class Rules213Blocked(RuntimeError):
    pass


def require(value: bool, reason: str) -> None:
    if not value:
        raise Rules213Blocked(reason)


@dataclass(frozen=True)
class ScopeReceipt:
    adapter_id: str
    contract_sha256: str
    repository: str
    base_sha: str
    allowed_paths: tuple[str, ...]
    ready_for_pr: bool = False
    builder_executed: bool = False


def bind_base_manifest(manifest: TaskManifest, *, issue_number: int,
                       live_main_sha: str, original: Mapping[str, bytes]) -> ScopeReceipt:
    """Only constrain inputs; this never authenticates an owner or invokes AI."""
    require(type(issue_number) is int and issue_number == ISSUE, "rules213_issue_mismatch")
    require(type(manifest) is TaskManifest, "rules213_manifest_invalid")
    require(manifest.repository == REPOSITORY
            and manifest.base_sha == BASE_SHA
            and manifest.workspace_sha == BASE_SHA
            and manifest.adapter_id == PROFILE
            and manifest.adapter_contract_sha256 == DESCRIPTOR.contract_sha256,
            "rules213_manifest_identity_mismatch")
    require(type(live_main_sha) is str and live_main_sha == BASE_SHA,
            "rules213_live_main_drift")
    require(set(manifest.before_sha256) == set(SOURCE_FUNCTIONS)
            and type(original) is dict
            and set(original) == set(SOURCE_FUNCTIONS),
            "rules213_source_scope_invalid")
    for path in SOURCE_FUNCTIONS:
        value = original[path]
        require(type(value) is bytes and 0 < len(value) <= 300_000,
                "rules213_source_invalid")
        require(hashlib.sha256(value).hexdigest() == manifest.before_sha256[path],
                "rules213_source_hash_mismatch")
    return ScopeReceipt(
        adapter_id=PROFILE, contract_sha256=DESCRIPTOR.contract_sha256,
        repository=REPOSITORY, base_sha=BASE_SHA,
        allowed_paths=tuple(sorted(SOURCE_FUNCTIONS)),
    )


def _without_function(source: bytes, function: str) -> tuple[bytes, bytes]:
    """Isolate exactly one top-level function; preserve all other source bytes."""
    require(type(source) is bytes and 0 < len(source) <= 300_000,
            "rules213_source_invalid")
    try:
        text = source.decode("utf-8")
        tree = ast.parse(text)
    except (UnicodeError, SyntaxError):
        raise Rules213Blocked("rules213_python_invalid") from None
    matches = [node for node in tree.body if isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef)
    ) and node.name == function]
    require(len(matches) == 1, "rules213_function_missing")
    node = matches[0]
    # A decorator is part of function authority; deny it to keep the scope exact.
    require(not node.decorator_list, "rules213_decorators_forbidden")
    lines = text.splitlines(keepends=True)
    require(node.lineno >= 1 and node.end_lineno is not None
            and node.end_lineno <= len(lines), "rules213_span_invalid")
    # Original module prefix and suffix must stay byte-for-byte identical.
    prefix = "".join(lines[:node.lineno - 1]).encode("utf-8")
    suffix = "".join(lines[node.end_lineno:]).encode("utf-8")
    function_bytes = "".join(lines[node.lineno - 1:node.end_lineno]).encode("utf-8")
    return prefix + b"\0" + suffix, function_bytes


def check_generated_scope(before: Mapping[str, bytes], after: Mapping[str, bytes]
                          ) -> dict[str, object]:
    """Check model output without running it or assigning model provenance."""
    require(type(before) is dict and type(after) is dict
            and set(before) == set(after) == set(SOURCE_FUNCTIONS),
            "rules213_paths_invalid")
    changes: list[str] = []
    for path, function in SOURCE_FUNCTIONS.items():
        old_outside, old_function = _without_function(before[path], function)
        new_outside, new_function = _without_function(after[path], function)
        require(old_outside == new_outside, "rules213_outside_function_changed")
        if old_function != new_function:
            changes.append(path)
    require(bool(changes), "rules213_patch_empty")
    return {
        "result": "RULES213_SOURCE_SCOPE_VERIFIED",
        "changed_paths": sorted(changes),
        "source_mode": "scope_check_only",
        "model_calls": 0,
        "ready_for_pr": False,
        "validator_approved": False,
        "builder_executed": False,
    }


def verify_model_identity(model: str, digest: str, calls: int) -> None:
    """Shape check ONLY: caller must independently attest real process/calls."""
    require(model == MODEL
            and type(digest) is str
            and SHA64.fullmatch(digest) is not None
            and digest.startswith(MODEL_DIGEST_PREFIX)
            and type(calls) is int and calls >= 1,
            "rules213_model_identity_invalid")
