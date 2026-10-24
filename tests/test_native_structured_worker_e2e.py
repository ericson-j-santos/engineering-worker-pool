"""Native Windows source/validator checks. No model download and no Docker."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.structured_code_worker import StructuredBlocked, sha256, validate
from scripts import native_structured_worker_e2e as physical
from scripts.structured_acceptance import cases, INSTRUCTION


def task_for(before: bytes) -> dict:
    return {
        "task_id": "native-structured-validation",
        "path": "app/portfolio_bridge.py",
        "function": "_property",
        "parameters": ["properties", "name"],
        "instruction": INSTRUCTION,
        "base_sha": "a" * 40,
        "before_sha256": sha256(before),
        "cases": cases(),
    }


def test_true_source_bug_has_real_failing_baseline():
    source = (Path(__file__).resolve().parents[1] / physical.SOURCE_FILE).read_bytes()
    report = validate(source.decode("utf-8"), task_for(source))
    assert report["total"] == 42
    assert report["passed"] < report["total"]


def test_native_repair_rejects_non_governed_worktree(tmp_path):
    with pytest.raises(StructuredBlocked, match="source_unavailable"):
        physical.require_source(tmp_path / "not-a-checkout", "a" * 40, tmp_path)


def test_native_repair_rejects_different_or_missing_source(tmp_path):
    with pytest.raises(StructuredBlocked, match="source_sha_invalid"):
        physical.require_source(tmp_path, "not-a-sha", tmp_path)


def test_native_script_is_bounded_to_real_source_and_approved_runner():
    source = (Path(__file__).resolve().parents[1] / "scripts/native_structured_worker_e2e.py").read_text()
    assert 'SOURCE_FILE = "app/portfolio_bridge.py"' in source
    assert "native-qwen7b-" in source
    assert "github_workspace_missing" in source
    assert "governed_worktree_required" in source
    assert "source_not_authorized_checkout" in source
    assert "git(independent, \"checkout\", \"--detach\"" in source
    assert "existing_bridge_regressions_failed" in source
    assert "replay_not_idempotent" in source
    assert "NATIVE_QWEN7B_CODE_E2E_PASSED" in source
    assert "docker run" not in source.lower()
    assert "git push" not in source.lower()
