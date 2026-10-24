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


def test_python_imports_must_not_dirty_exact_git_source():
    source = (Path(__file__).resolve().parents[1] /
              "scripts/native_structured_worker_e2e.py").read_text(encoding="utf-8")
    setter = source.index("sys.dont_write_bytecode = True")
    first_project_import = source.index("from app.local_code_worker import WorkerBlocked")
    assert setter < first_project_import
    assert 'git(source, "status", "--porcelain", "--untracked-files=all") == ""' in source
    assert "source_tree_dirty" in source


def test_dirty_source_is_rejected_even_when_only_untracked(tmp_path, monkeypatch):
    import subprocess
    base = tmp_path / "checkout-workspace"
    repo = base / "external" / "engineering-worker-pool"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True, capture_output=True)
    (repo / "test.py").write_text("DATA = 1\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "test.py", ".gitignore"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=UnitTest",
                    "-c", "user.email=test@localhost", "commit", "-m", "test",
                    "--quiet"], check=True, capture_output=True)
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    monkeypatch.setattr(physical, "ROOT", repo.resolve())
    monkeypatch.setenv("GITHUB_WORKSPACE", str(base))
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "test.cpython-312.pyc").write_bytes(b"dummy")
    assert subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        text=True
    ) == ""
    # Ignored bytecode alone cannot reproduce source_tree_dirty in the real repo.
    # A distinct, non-ignored file must remain blocked.
    (repo / "unexpected.untracked").write_text("other work", encoding="utf-8")
    worktree = tmp_path / "chatgpt-workers" / "worktree"
    worktree.mkdir(parents=True)
    with pytest.raises(StructuredBlocked, match="^source_tree_dirty$"):
        physical.require_source(repo.resolve(), sha, worktree.resolve())


def test_dirty_diagnostic_is_sanitized_and_can_isolate_line_endings(monkeypatch):
    import hashlib
    calls = []
    def fake_git(root, *args):
        calls.append(args)
        if "-c" in args:
            return ""
        return " M app/example.py\n?? .local-secret/credential.pem"
    monkeypatch.setattr(physical, "git", fake_git)
    report = physical.dirty_checkout_diagnostic()
    assert report["diagnostic_state"] == "status_classified"
    assert report["status_entry_count"] == 2
    assert report["autocrlf_true_would_be_clean"] is True
    assert report["status_entries"][0]["category"] == "app"
    assert report["status_entries"][0]["extension"] == ".py"
    assert report["status_entries"][1]["category"] == "root_or_other"
    assert "credential" not in str(report)
    assert "example.py" not in str(report)
    assert report["status_entries"][1]["path_fingerprint"] == (
        hashlib.sha256(b".local-secret/credential.pem").hexdigest()[:12]
    )
    assert len(calls) == 2


def test_dirty_diagnostic_error_fails_closed(monkeypatch):
    def unavailable(*_args):
        raise StructuredBlocked("git_operation")
    monkeypatch.setattr(physical, "git", unavailable)
    assert physical.dirty_checkout_diagnostic() == {
        "diagnostic_state": "status_unavailable"
    }
