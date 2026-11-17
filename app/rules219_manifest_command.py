"""Governed, exact Rules #219 manifest command binding. No runtime activation."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from app.native_gateway_commands import GovernedCommand
from app.native_one_shot import (
    AdapterContext, AdapterDescriptor, AdapterResult, NativeExecutorBlocked,
    TaskManifest, SHA40,
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
