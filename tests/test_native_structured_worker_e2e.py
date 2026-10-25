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
    assert 'git(source, "status", "--porcelain", "--untracked-files=all")' in source
    assert '"diff", "--name-only", "HEAD", "--"' in source
    assert '"ls-files", "--others", "--exclude-standard"' in source
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


def create_eol_repository(tmp_path):
    """Commit exact LF content, then let tests change only its physical checkout."""
    import subprocess
    repo = tmp_path / "physical-git-eol"
    (repo / "app").mkdir(parents=True)
    source = repo / "app" / "portfolio_bridge.py"
    source.write_bytes(b"def check():\n    return 123\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "core.autocrlf=false",
                    "add", "app/portfolio_bridge.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=CI",
                    "-c", "user.email=ci@localhost", "commit", "-qm", "eol baseline"],
                   check=True, capture_output=True)
    assert physical.git(repo, "status", "--porcelain", "--untracked-files=all") == ""
    return repo, source


def test_windows_crlf_checkout_normalizes_identical_source(tmp_path):
    repo, source = create_eol_repository(tmp_path)
    source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
    assert physical.git(repo, "status", "--porcelain", "--untracked-files=all") != ""
    assert physical.git(repo, "-c", "core.autocrlf=true",
                        "diff", "--name-only", "HEAD", "--") == ""
    assert physical.git(repo, "rev-parse", "HEAD:app/portfolio_bridge.py") == (
        physical.git(repo, "-c", "core.autocrlf=true", "hash-object",
                     "--path", "app/portfolio_bridge.py", "app/portfolio_bridge.py")
    )
    assert physical.verified_source_clean(repo, native_windows=True) == (
        "git_verified_crlf_normalization"
    )
    # Only Windows has this recovery path; no OS-wide or persistent Git config.
    with pytest.raises(StructuredBlocked, match="^source_tree_dirty$"):
        physical.verified_source_clean(repo, native_windows=False)


@pytest.mark.parametrize("kind", ["modified_content", "untracked", "staged_content"])
def test_windows_crlf_normalization_never_masks_real_changes(tmp_path, kind):
    repo, source = create_eol_repository(tmp_path)
    source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
    if kind == "modified_content":
        source.write_bytes(b"def check():\r\n    return 456\r\n")
    elif kind == "untracked":
        (repo / "unexpected-work.txt").write_text("other user work")
    else:
        source.write_bytes(b"def check():\r\n    return 456\r\n")
        physical.git(repo, "add", "app/portfolio_bridge.py")
    with pytest.raises(StructuredBlocked, match="^source_tree_dirty$"):
        physical.verified_source_clean(repo, native_windows=True)


def test_windows_native_clean_checkout_keeps_strict_mode(tmp_path):
    repo, source = create_eol_repository(tmp_path)
    assert source.is_file()
    assert physical.verified_source_clean(repo, native_windows=True) == "exact_clean"


def test_windows_normalization_requires_hash_match_even_after_clean_status(tmp_path, monkeypatch):
    repo, _ = create_eol_repository(tmp_path)
    original = physical.git

    def divergent(root, *args):
        if "hash-object" in args:
            return "f" * 40
        if "-c" in args and "diff" in args:
            return ""
        if args[:2] == ("status", "--porcelain"):
            return " M app/portfolio_bridge.py"
        return original(root, *args)

    monkeypatch.setattr(physical, "git", divergent)
    with pytest.raises(StructuredBlocked, match="^source_blob_mismatch$"):
        physical.verified_source_clean(repo, native_windows=True)


def test_real_patched_portfolio_passes_native_regression_runner(tmp_path):
    """Real Git clone and actual pytest, not a mocked subprocess."""
    import json
    from test_structured_code_worker import FIXED
    from app.structured_code_worker import render, sha256
    source_repo = Path(__file__).resolve().parents[1]
    independent = tmp_path / "independent-regression"
    physical.git(tmp_path, "clone", "--no-hardlinks", str(source_repo), str(independent))
    target = independent / "app/portfolio_bridge.py"
    before = target.read_text(encoding="utf-8")
    task = {
        "task_id": "independent-regression-probe",
        "path": "app/portfolio_bridge.py",
        "function": "_property",
        "parameters": ["properties", "name"],
        "instruction": INSTRUCTION,
        "base_sha": physical.git(independent, "rev-parse", "HEAD"),
        "before_sha256": sha256(before.encode()),
        "cases": cases(),
    }
    replacement = render(before, task, json.dumps({"function": FIXED}))
    target.write_text(replacement, encoding="utf-8", newline="")
    try:
        observed = physical.run_bridge_regressions(independent, tmp_path)
    except physical.BridgeRegressionFailed as exc:
        # Print ONLY fixed-schema counts/category; never raw subprocess output.
        pytest.fail("sanitized_regression_diagnosis=" + repr(exc.diagnosis))
    assert observed["category"] == "passed"
    assert observed["exit_code"] == 0 and observed["passed_count"] >= 10
    assert observed["output_within_limit"] is True


def test_regression_fail_closed_without_tests_and_without_leaking_output(tmp_path):
    missing = tmp_path / "empty-clone"
    missing.mkdir()
    with pytest.raises(physical.BridgeRegressionFailed) as err:
        physical.run_bridge_regressions(missing, tmp_path)
    sanitized = err.value.diagnosis
    assert sanitized["category"] in {"process_error", "import_or_collection"}
    assert sanitized["exit_code"] != 0
    assert "stdout" not in str(sanitized) and "stderr" not in str(sanitized)
    assert str(missing) not in str(sanitized)


def test_regression_runner_resets_project_quietness_and_keeps_summary(
        monkeypatch, tmp_path):
    observed = {}

    def completed(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return physical.subprocess.CompletedProcess(
            argv, 0, stdout="12 passed in 0.10s\n", stderr=""
        )

    monkeypatch.setattr(physical.subprocess, "run", completed)
    result = physical.run_bridge_regressions(tmp_path, tmp_path)

    assert observed["argv"][:6] == [
        physical.sys.executable, "-m", "pytest", "-o", "addopts=", "-q"
    ]
    assert observed["kwargs"]["check"] is False
    assert result == {
        "exit_code": 0,
        "passed_count": 12,
        "failed_count": 0,
        "error_count": 0,
        "category": "passed",
        "output_within_limit": True,
    }


def test_regression_environment_only_contains_explicit_nonsecret_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("PRIVATE_SECRET_TOKEN", "PRIVATE_VALUE")
    env = physical.bridge_regression_environment(tmp_path)
    assert "PRIVATE_SECRET_TOKEN" not in env
    assert "PRIVATE_VALUE" not in str(env)
    assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert env["TEMP"] == str(tmp_path.resolve())
    assert env["TMP"] == str(tmp_path.resolve())
    assert env["PYTHONPATH"] == ""


def test_preflight_collects_real_portfolio_modules_before_model(tmp_path):
    """Collect actual regression tests in a separate checkout (no model needed)."""
    source_repo = Path(__file__).resolve().parents[1]
    independent = tmp_path / "preflight-copy"
    physical.git(tmp_path, "clone", "--no-hardlinks", str(source_repo), str(independent))
    report = physical.preflight_bridge_regressions(independent, tmp_path)
    assert report["phase"] == "collection_preflight"
    assert report["collected_count"] >= 10


def test_preflight_fails_closed_when_pytest_missing(monkeypatch, tmp_path):
    """Simulate the precise native Python failure without leaking its stderr."""
    def missing(argv, **kwargs):
        assert argv[:5] == [
            physical.sys.executable, "-m", "pytest", "-o", "addopts="
        ]
        assert "--collect-only" in argv
        return physical.subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="python.exe: No module named pytest"
        )
    monkeypatch.setattr(physical.subprocess, "run", missing)
    with pytest.raises(physical.BridgeRegressionFailed) as failure:
        physical.preflight_bridge_regressions(tmp_path, tmp_path)
    detail = failure.value.diagnosis
    assert detail == {
        "phase": "collection_preflight",
        "category": "pytest_unavailable",
        "exit_code": 1,
        "collected_count": 0,
        "output_within_limit": True,
    }
    assert "python.exe" not in str(detail)
    assert "stderr" not in str(detail)


def test_preflight_rejects_success_without_collected_tests(monkeypatch, tmp_path):
    """A zero exit code is not proof of test collection."""
    def empty(argv, **kwargs):
        return physical.subprocess.CompletedProcess(
            argv, 0, stdout="no collection evidence", stderr=""
        )
    monkeypatch.setattr(physical.subprocess, "run", empty)
    with pytest.raises(physical.BridgeRegressionFailed) as failure:
        physical.preflight_bridge_regressions(tmp_path, tmp_path)
    assert failure.value.diagnosis["category"] == "process_error"
    assert failure.value.diagnosis["collected_count"] == 0


def test_missing_pytest_is_distinguished_from_code_regression(monkeypatch, tmp_path):
    def missing(argv, **kwargs):
        return physical.subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="python.exe: No module named 'pytest'"
        )
    monkeypatch.setattr(physical.subprocess, "run", missing)
    with pytest.raises(physical.BridgeRegressionFailed) as failure:
        physical.run_bridge_regressions(tmp_path, tmp_path)
    assert failure.value.diagnosis["category"] == "pytest_unavailable"
    assert failure.value.diagnosis["passed_count"] == 0


def test_preflight_must_run_before_model_inference():
    source = (Path(__file__).resolve().parents[1] /
              "scripts/native_structured_worker_e2e.py").read_text(encoding="utf-8")
    assert source.index("preflight_bridge_regressions(checkout, sandbox)") < (
        source.index("provider = NativeStructuredOllama()")
    )
    assert "PIP_INSTALL" not in source
    assert "docker run" not in source.lower()
