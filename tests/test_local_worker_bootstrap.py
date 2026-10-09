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


def test_repair_prompt_includes_failure_context_without_solution_fallback(monkeypatch):
    import importlib.util
    path = Path(__file__).with_name("test_local_code_worker.py")
    spec = importlib.util.spec_from_file_location("code_worker_fixtures", path)
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    make_task, BASELINE, GOOD = fixtures.make_task, fixtures.BASELINE, fixtures.GOOD
    observed = []
    monkeypatch.setattr(worker.LocalOllama, "no_cloud", staticmethod(lambda: None))
    def request(client, method, path, **kwargs):
        if path == "/api/tags":
            return {"models": [{"name": worker.MODEL, "digest": "d" * 64}]}
        if path == "/api/show":
            return {"details": {"format": "gguf"}}
        if path == "/api/ps":
            return {"models": [{"name": worker.MODEL, "digest": "d" * 64, "size": 100}]}
        assert path == "/api/chat"
        observed.append(kwargs["json"]["messages"])
        return {"model": worker.MODEL, "done": True, "eval_count": 10,
                "message": {"content": GOOD}}
    monkeypatch.setattr(worker.LocalOllama, "request", staticmethod(request))
    assert worker.LocalOllama()(make_task(), BASELINE) == GOOD
    messages = observed[0]
    assert messages[0]["role"] == "system"
    assert "current implementation fails" in messages[1]["content"]
    assert json.dumps(make_task()["cases"][:8], separators=(",", ":")) in messages[1]["content"]
    assert json.loads(GOOD)["expression"] not in messages[1]["content"]


def test_model_pulled_matches_strict_provider_inventory():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/ci.yml").read_text()
    assert f"ollama pull {worker.MODEL}" in workflow
    assert worker.MODEL == "qwen2.5-coder:1.5b"


def test_second_proposal_requires_real_failure_feedback(tmp_path):
    import importlib.util
    path = Path(__file__).with_name("test_local_code_worker.py")
    spec = importlib.util.spec_from_file_location("feedback_fixtures", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    repo, state = tmp_path / "repo", tmp_path / "state"
    (repo / "src").mkdir(parents=True)
    (repo / "src/clamp.py").write_text(fixture.BASELINE)
    worker.git(repo, "init", "-b", "worker/feedback")
    worker.git(repo, "add", "src/clamp.py")
    worker.git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
               "commit", "-m", "baseline")
    task = fixture.make_task(worker.git(repo, "rev-parse", "HEAD"))
    calls = []
    def propose(received, source):
        calls.append((received, source))
        assert received["instruction"] == task["instruction"]
        assert sorted(json.dumps(v, sort_keys=True) for v in received["cases"]) == sorted(
            json.dumps(v, sort_keys=True) for v in task["cases"])
        if len(calls) == 1:
            return '{"expression":"0"}'
        assert "return 0" in source
        assert received["cases"][0]["expected"] != 0
        return fixture.GOOD
    result = worker.repair(repo, state, task, propose)
    assert result["model_calls"] == len(calls) == 2
    assert result["attempts"][0]["passed"] < result["attempts"][0]["total"]
    assert result["attempts"][1]["passed"] == len(task["cases"])
    assert worker.repair(repo, state, task, propose)["model_calls"] == 0
    assert len(calls) == 2
