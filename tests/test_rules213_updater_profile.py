"""Rules #213 contract: allowlisted updater only, no Builder execution."""
from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import asdict

import pytest

from app.native_one_shot import TaskManifest
from app.rules213_updater_profile import (
    BASE_SHA, DESCRIPTOR, ISSUE, PROFILE, REPOSITORY, SOURCE_FUNCTIONS,
    Rules213Blocked, bind_base_manifest, check_generated_scope, verify_model_identity,
)

SOURCE = b"header = 1\n\ndef install_bundle():\n    return 1\n\nfooter = 2\n"
AFTER = b"header = 1\n\ndef install_bundle():\n    return 2\n\nfooter = 2\n"
SOURCE2 = b"header = 1\n\ndef preflight_runtime_replaceability():\n    return 1\n"
SOURCE2_AFTER = b"header = 1\n\ndef preflight_runtime_replaceability():\n    return 2\n"
PATHS = sorted(SOURCE_FUNCTIONS)
BEFORE = {PATHS[0]: SOURCE, PATHS[1]: SOURCE2}
UPDATED = {PATHS[0]: AFTER, PATHS[1]: SOURCE2_AFTER}


def manifest(**changes):
    raw = {
        "schema_version": 1, "task_id": "cwp-" + "a"*24,
        "repository": REPOSITORY, "base_sha": BASE_SHA,
        "workspace_sha": BASE_SHA, "adapter_id": PROFILE,
        "adapter_contract_sha256": DESCRIPTOR.contract_sha256,
        "before_sha256": {k: hashlib.sha256(v).hexdigest() for k,v in BEFORE.items()},
    }
    raw.update(changes)
    return TaskManifest.parse(raw)


def test_pre_admission_accepts_exact_source_but_not_builder():
    receipt = bind_base_manifest(manifest(), issue_number=ISSUE,
                                 live_main_sha=BASE_SHA, original=BEFORE)
    assert receipt.repository == REPOSITORY
    assert receipt.builder_executed is False
    assert receipt.ready_for_pr is False
    assert DESCRIPTOR.allowed_paths == tuple(PATHS)


@pytest.mark.parametrize("case", ["issue", "main", "repo", "profile", "hash", "path"])
def test_wrong_identity_fails_closed(case):
    kwargs = {"issue_number": ISSUE, "live_main_sha": BASE_SHA, "original": BEFORE}
    m = manifest()
    if case == "issue":
        kwargs["issue_number"] = ISSUE + 1
    if case == "main":
        kwargs["live_main_sha"] = "f"*40
    if case == "repo":
        m = manifest(repository="ericson-j-santos/engineering-worker-pool")
    if case == "profile":
        m = manifest(adapter_id="bridge.property.v1")
    if case == "hash":
        kwargs["original"] = {**BEFORE, PATHS[0]: b"tampered"}
    if case == "path":
        kwargs["original"] = {**BEFORE, "app/portfolio_bridge.py": b"malicious"}
    with pytest.raises(Rules213Blocked):
        bind_base_manifest(m, **kwargs)


def test_only_fixed_functions_can_change():
    decision = check_generated_scope(BEFORE, UPDATED)
    assert decision["changed_paths"] == PATHS
    assert decision["model_calls"] == 0
    assert decision["ready_for_pr"] is False


def test_import_and_top_level_mutations_are_rejected():
    malicious = {**UPDATED, PATHS[0]: UPDATED[PATHS[0]].replace(
        b"header = 1", b"header = 3")}
    with pytest.raises(Rules213Blocked, match="outside_function_changed"):
        check_generated_scope(BEFORE, malicious)


def test_non_target_paths_and_empty_patch_rejected():
    with pytest.raises(Rules213Blocked, match="paths_invalid"):
        check_generated_scope(BEFORE, {**UPDATED, "app/portfolio_bridge.py": b"new"})
    with pytest.raises(Rules213Blocked, match="patch_empty"):
        check_generated_scope(BEFORE, BEFORE)


def test_model_evidence_shape_does_not_grant_admission():
    with pytest.raises(Rules213Blocked):
        verify_model_identity("other", "a"*64, 1)
    with pytest.raises(Rules213Blocked):
        verify_model_identity("qwen2.5-coder:7b", "dae161e27b0e" + "0"*52, 0)
    verify_model_identity("qwen2.5-coder:7b", "dae161e27b0e" + "0"*52, 1)
    assert check_generated_scope(BEFORE, UPDATED)["ready_for_pr"] is False
