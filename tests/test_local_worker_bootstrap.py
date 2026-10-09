from pathlib import Path
import json

import pytest
from app import local_code_worker as worker


def _harness():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "scripts/local_worker_e2e.py"
    spec = importlib.util.spec_from_file_location("local_worker_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(("head", "ref", "expected"), [
    ("feat/local-code-worker-e2e-20261009", "17/merge", "origin/feat/local-code-worker-e2e-20261009"),
    ("", "main", "origin/main"),
])
def test_session_has_source_ref_and_exact_sha(monkeypatch, tmp_path, head, ref, expected):
    from types import SimpleNamespace
    harness = _harness()
    monkeypatch.setenv("GITHUB_HEAD_REF", head)
    monkeypatch.setenv("GITHUB_REF_NAME", ref)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs == {"cwd": tmp_path, "capture_output": True, "text": True,
                          "timeout": 60, "check": False}
        return SimpleNamespace(returncode=0, stdout='{"result":"SESSION_LAUNCH_OK"}', stderr="")
    monkeypatch.setattr(harness.subprocess, "run", run)
    for _ in range(2):
        assert "SESSION_LAUNCH_OK" in harness.launch_session(
            tmp_path, tmp_path / "_rules", tmp_path / "policy.json", "a" * 40, "e2e-test")
    assert calls[0] == calls[1]
    argv = calls[0]
    assert argv[argv.index("--sync-ref") + 1] == expected
    assert argv[argv.index("--expected-head") + 1] == "a" * 40


@pytest.mark.parametrize("branch", ["", "../main", "feat//invalid", "-bad", "x;echo", "x.lock", "x/"])
def test_session_invalid_ref_blocks_before_subprocess(monkeypatch, tmp_path, branch):
    harness = _harness()
    monkeypatch.setenv("GITHUB_HEAD_REF", branch)
    monkeypatch.setenv("GITHUB_REF_NAME", branch)
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: pytest.fail("process executed"))
    with pytest.raises(worker.WorkerBlocked, match="source_ref_invalid"):
        harness.launch_session(tmp_path, tmp_path, tmp_path / "p", "a" * 40, "test")


def test_failed_bootstrap_preserves_gate_and_sanitizes_output(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace
    harness = _harness()
    monkeypatch.setenv("GITHUB_HEAD_REF", "main")
    monkeypatch.setattr(harness.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=28, stdout="PRIVATE_VALUE", stderr=json.dumps({
            "result": "SESSION_LAUNCH_BLOCKED", "gateway_exit_code": 28,
            "error": "PRIVATE_VALUE"})))
    with pytest.raises(worker.WorkerBlocked, match="bootstrap_process_failed"):
        harness.launch_session(tmp_path, tmp_path, tmp_path / "p", "a" * 40, "test")
    assert capsys.readouterr().out == "SESSION_LAUNCH_BLOCKED code=28\n"
