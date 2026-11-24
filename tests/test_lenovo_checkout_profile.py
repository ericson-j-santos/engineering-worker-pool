"""Fixed Lenovo checkout path admission, negatives and legacy regression."""
from dataclasses import replace
from pathlib import Path

import pytest

from app.native_one_shot import (
    AdapterContext, AdapterDescriptor, AdapterResult, LENOVO_CHECKOUT_BUILDER_COMMANDS,
    LENOVO_CHECKOUT_DESCRIPTOR, LENOVO_CHECKOUT_PATHS, LENOVO_CHECKOUT_PROFILE,
    LENOVO_CHECKOUT_REPOSITORY, LENOVO_CHECKOUT_TEST,
    LENOVO_CHECKOUT_VALIDATOR_COMMANDS, LENOVO_CHECKOUT_WORKFLOW,
    NativeExecutorBlocked, NativeOneShotExecutor, TaskManifest, _safe_path,
)

BASE = "a" * 40
DIGEST = "b" * 64


def manifest(**kwargs):
    m = {
        "schema_version": 1, "task_id": "cwp-" + "1" * 24,
        "repository": LENOVO_CHECKOUT_REPOSITORY, "base_sha": BASE,
        "workspace_sha": BASE, "adapter_id": LENOVO_CHECKOUT_PROFILE,
        "adapter_contract_sha256": LENOVO_CHECKOUT_DESCRIPTOR.contract_sha256,
        "before_sha256": {name: DIGEST for name in LENOVO_CHECKOUT_PATHS},
    }
    m.update(kwargs)
    return m


def test_exact_descriptor_and_manifest_positive():
    d = LENOVO_CHECKOUT_DESCRIPTOR
    assert d.allowed_paths == (
        ".github/workflows/lenovo-git-discovery-readonly.yml",
        "tests/test_lenovo_git_discovery_readonly_workflow.py",
    )
    assert d.builder_commands == LENOVO_CHECKOUT_BUILDER_COMMANDS
    assert d.validator_commands == LENOVO_CHECKOUT_VALIDATOR_COMMANDS
    assert d.roles == ("builder", "validator")
    assert d.normalized()["allowed_paths"] == list(LENOVO_CHECKOUT_PATHS)
    assert TaskManifest.parse(manifest()).repository == LENOVO_CHECKOUT_REPOSITORY


@pytest.mark.parametrize("path", [
    ".git/config", ".git/hooks/pre-commit", ".env", "src/.hidden",
    ".github/workflows/other.yml",
    ".github/workflows/lenovo-git-discovery-readonly.yml:ads",
    ".github/workflows/lenovo-git-discovery-readonly.yml/extra",
    ".github//workflows/lenovo-git-discovery-readonly.yml",
    "../.github/workflows/lenovo-git-discovery-readonly.yml",
    "/.github/workflows/lenovo-git-discovery-readonly.yml",
    ".\\github\\workflows\\lenovo-git-discovery-readonly.yml",
    ".github/workflows/../private.yml",
])
def test_no_hidden_alias_or_traversal(path):
    with pytest.raises(NativeExecutorBlocked):
        _safe_path(path, adapter_id=LENOVO_CHECKOUT_PROFILE)


def test_only_fixed_profile_may_use_literal_workflow():
    assert _safe_path(LENOVO_CHECKOUT_WORKFLOW,
                      adapter_id=LENOVO_CHECKOUT_PROFILE) == LENOVO_CHECKOUT_WORKFLOW
    assert _safe_path(LENOVO_CHECKOUT_TEST,
                      adapter_id=LENOVO_CHECKOUT_PROFILE) == LENOVO_CHECKOUT_TEST
    for other in (None, "bridge.property.v1", "other.adapter.v1"):
        with pytest.raises(NativeExecutorBlocked, match="manifest_path_hidden"):
            _safe_path(LENOVO_CHECKOUT_WORKFLOW, adapter_id=other)


@pytest.mark.parametrize("paths", [
    (LENOVO_CHECKOUT_WORKFLOW,), (LENOVO_CHECKOUT_TEST,),
    (LENOVO_CHECKOUT_TEST, LENOVO_CHECKOUT_WORKFLOW),
    (LENOVO_CHECKOUT_WORKFLOW, LENOVO_CHECKOUT_TEST, "anything.py"),
])
def test_fixed_descriptor_cannot_expand_narrow_or_reorder(paths):
    with pytest.raises(NativeExecutorBlocked, match="(?:lenovo_adapter_paths|adapter_paths)"):
        replace(LENOVO_CHECKOUT_DESCRIPTOR, allowed_paths=paths).normalized()


def test_fixed_command_roles_immutable():
    for bad in (
        replace(LENOVO_CHECKOUT_DESCRIPTOR, builder_commands=("shell.exec",)),
        replace(LENOVO_CHECKOUT_DESCRIPTOR, validator_commands=("external.write",)),
        replace(LENOVO_CHECKOUT_DESCRIPTOR, roles=("builder",)),
    ):
        with pytest.raises(NativeExecutorBlocked, match="lenovo_adapter"):
            bad.normalized()


def test_repository_and_both_manifest_files_required():
    with pytest.raises(NativeExecutorBlocked, match="lenovo_manifest_repository"):
        TaskManifest.parse(manifest(repository="ericson-j-santos/noteri-runtime"))
    for name in LENOVO_CHECKOUT_PATHS:
        with pytest.raises(NativeExecutorBlocked, match="lenovo_manifest_paths"):
            TaskManifest.parse(manifest(before_sha256={name: DIGEST}))
    with pytest.raises(NativeExecutorBlocked, match="manifest_path_hidden"):
        TaskManifest.parse(manifest(before_sha256={
            **manifest()["before_sha256"], ".git/config": DIGEST,
        }))


def test_legacy_adapter_is_unchanged_and_forbids_workflow_dot_path():
    legacy = AdapterDescriptor("bridge.property.v1", ("app/portfolio_bridge.py",),
                               ("issue.repair",), ("issue.validate",))
    assert legacy.normalized()["allowed_paths"] == ["app/portfolio_bridge.py"]
    with pytest.raises(NativeExecutorBlocked, match="manifest_path_hidden"):
        replace(legacy, allowed_paths=(LENOVO_CHECKOUT_WORKFLOW,)).normalized()


def test_adapter_result_restricts_paths_and_commands():
    d = LENOVO_CHECKOUT_DESCRIPTOR
    context = AdapterContext(
        Path("."), TaskManifest.parse(manifest()), d, {
            "lenovo.checkout.build": lambda: None,
            "lenovo.checkout.validate": lambda: None,
        }, "builder",
    )
    context.invoke("lenovo.checkout.build")
    result = AdapterResult(BASE, (LENOVO_CHECKOUT_WORKFLOW,),
                           tuple(context.invoked), DIGEST)
    NativeOneShotExecutor._validate_result(result, d, context, "builder")
    NativeOneShotExecutor._validate_recovered_result(result, d)
    for bad in (".git/config", ".github/workflows/other.yml", "other.py"):
        with pytest.raises(NativeExecutorBlocked):
            NativeOneShotExecutor._validate_result(
                replace(result, changed_paths=(bad,)), d, context, "builder")
        with pytest.raises(NativeExecutorBlocked):
            NativeOneShotExecutor._validate_recovered_result(
                replace(result, changed_paths=(bad,)), d)
    with pytest.raises(NativeExecutorBlocked, match="adapter_result_commands"):
        NativeOneShotExecutor._validate_result(
            replace(result, invoked_commands=("shell.exec",)), d, context, "builder")


def test_manifest_replay_is_deterministic_without_side_effects():
    assert TaskManifest.parse(manifest()) == TaskManifest.parse(manifest())
    assert LENOVO_CHECKOUT_DESCRIPTOR.contract_sha256 == LENOVO_CHECKOUT_DESCRIPTOR.contract_sha256
