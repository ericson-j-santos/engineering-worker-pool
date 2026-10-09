from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from app import local_code_worker as worker

BASELINE = "def clamp(value, lower, upper):\n    return value\n"
GOOD = '{"expression":"lower if value < lower else upper if value > upper else value"}'


def make_task(base_sha: str = "a" * 40) -> dict:
    return {"task_id": "local-code-e2e", "path": "src/clamp.py", "function": "clamp",
            "parameters": ["value", "lower", "upper"], "instruction":
            "Clamp value between lower and upper, inclusive. Assume lower <= upper.",
            "base_sha": base_sha, "before_sha256": worker.digest(BASELINE.encode()),
            "cases": [{"args": [v, lo, hi], "expected": min(max(v, lo), hi)}
                      for lo, hi in [(-3, 3), (0, 10), (2, 2)]
                      for v in [-10, -3, 0, 2, 3, 10, 20]]}


@pytest.fixture
def checkout(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/clamp.py").write_text(BASELINE)
    worker.git(repo, "init", "-b", "worker/test")
    worker.git(repo, "add", "src/clamp.py")
    worker.git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
               "commit", "-m", "baseline")
    return repo, tmp_path / "state", make_task(worker.git(repo, "rev-parse", "HEAD"))


def test_real_git_repair_independent_process_and_replay(checkout):
    repo, state, task = checkout
    calls = []
    def propose(spec, source):
        assert source == BASELINE
        calls.append(spec["task_id"])
        return GOOD
    first = worker.repair(repo, state, task, propose)
    assert first["result"] == "LOCAL_CODE_REPAIRED"
    assert first["baseline_passed"] < first["passed"] == len(task["cases"])
    assert first["produced_sha"] == worker.git(repo, "rev-parse", "HEAD") != task["base_sha"]
    assert worker.git(repo, "diff", "--name-only", task["base_sha"], first["produced_sha"]) == task["path"]
    assert worker.digest((state / "change.patch").read_bytes()) == first["patch_sha256"]
    second = worker.repair(repo, state, task, propose)
    assert second["replayed"] is True and second["model_calls"] == 0
    assert second["produced_sha"] == first["produced_sha"]
    assert len(calls) == 1
    assert worker.git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("expression", [
    '__import__("os").system("PRIVATE_COMMAND")', "value.__class__", "value[0]",
    "(x for x in [1])", "[value]", "{'x':value}", "'secret'", "True",
    "10 ** 10000000", "(lambda: value)()", "max(value, lower)", "unknown",
    "(x := value)", "value / 0", "1e999", "9" * 1100,
])
def test_model_cannot_escape_numeric_grammar(expression):
    with pytest.raises(worker.WorkerBlocked):
        worker.source_for(make_task(), json.dumps({"expression": expression}))


@pytest.mark.parametrize("reply", ["not json", '{"expression":"value","path":"other.py"}',
                                   '[]', '{"expression":null}', '{"expression":true}'])
def test_reply_schema_is_closed(reply):
    with pytest.raises(worker.WorkerBlocked):
        worker.source_for(make_task(), reply)


@pytest.mark.parametrize(("field", "value"), [
    ("path", "../outside.py"), ("path", ".github/workflows/ci.yml"),
    ("path", "src/../outside.py"), ("base_sha", "main"), ("before_sha256", "bad"),
    ("parameters", ["value", "__builtins__"]), ("parameters", ["v", "v"]),
    ("cases", [{"args": [10**1000, 0, 1], "expected": 1}]),
    ("cases", [{"args": [True, 0, 1], "expected": 1}]),
])
def test_bad_tasks_fail_before_model_call(checkout, field, value):
    repo, state, task = checkout
    task[field] = value
    with pytest.raises(worker.WorkerBlocked):
        worker.repair(repo, state, task, lambda *_: pytest.fail("provider called"))


@pytest.mark.parametrize("kind", ["dirty", "sha", "hash", "branch", "remote", "hook"])
def test_preconditions_fail_before_model_call(checkout, kind):
    repo, state, task = checkout
    if kind == "dirty":
        (repo / "unexpected.txt").write_text("existing user work")
    elif kind == "sha":
        task["base_sha"] = "b" * 40
    elif kind == "hash":
        task["before_sha256"] = "b" * 64
    elif kind == "branch":
        worker.git(repo, "branch", "-m", "main")
    elif kind == "remote":
        worker.git(repo, "remote", "add", "origin", "https://example.invalid/repo")
    else:
        (repo / ".git/hooks/pre-commit").write_text("PRIVATE_COMMAND")
    with pytest.raises(worker.WorkerBlocked):
        worker.repair(repo, state, task, lambda *_: pytest.fail("provider called"))
    assert (repo / task["path"]).read_text() == BASELINE


def test_wrong_candidate_keeps_repository_unchanged(checkout):
    repo, state, task = checkout
    with pytest.raises(worker.WorkerBlocked, match="candidate_tests_failed"):
        worker.repair(repo, state, task, lambda *_: '{"expression":"0"}')
    assert worker.git(repo, "rev-parse", "HEAD") == task["base_sha"]
    assert worker.git(repo, "status", "--porcelain") == ""


def test_no_failure_does_not_spend_inference(checkout):
    repo, state, task = checkout
    for case in task["cases"]:
        case["expected"] = case["args"][0]
    with pytest.raises(worker.WorkerBlocked, match="no_failing_baseline"):
        worker.repair(repo, state, task, lambda *_: pytest.fail("provider called"))


def test_worker_lock_blocks_duplicate_execution(checkout):
    repo, state, task = checkout
    state.mkdir()
    lock = state / (worker.digest(str(repo.resolve()).encode()) + ".lock")
    lock.write_text("existing executor")
    with pytest.raises(worker.WorkerBlocked, match="worker_busy"):
        worker.repair(repo, state, task, lambda *_: pytest.fail("provider called"))
    assert lock.read_text() == "existing executor"


def test_independent_validator_failure_restores_only_own_change(checkout, monkeypatch):
    repo, state, task = checkout
    def reject(*_):
        raise worker.WorkerBlocked("validator_rejected")
    monkeypatch.setattr(worker, "validate_independently", reject)
    with pytest.raises(worker.WorkerBlocked, match="validator_rejected"):
        worker.repair(repo, state, task, lambda *_: GOOD)
    assert worker.git(repo, "status", "--porcelain") == ""
    assert (repo / task["path"]).read_text() == BASELINE


def test_concurrent_source_change_is_preserved(checkout):
    repo, state, task = checkout
    def competing_writer(*_):
        (repo / task["path"]).write_text("# someone else's work\n" + BASELINE)
        return GOOD
    with pytest.raises(worker.WorkerBlocked, match="concurrent_repo_change"):
        worker.repair(repo, state, task, competing_writer)
    assert (repo / task["path"]).read_text().startswith("# someone else's work")


def test_replay_cannot_change_task(checkout):
    repo, state, task = checkout
    worker.repair(repo, state, task, lambda *_: GOOD)
    task["instruction"] += " new task"
    with pytest.raises(worker.WorkerBlocked, match="replay_task_mismatch"):
        worker.repair(repo, state, task, lambda *_: pytest.fail("provider called"))


def test_symlink_target_rejected(checkout, tmp_path):
    repo, state, task = checkout
    target = repo / task["path"]
    target.unlink()
    outside = tmp_path / "outside.py"
    outside.write_text(BASELINE)
    target.symlink_to(outside)
    with pytest.raises(worker.WorkerBlocked, match="target_not_regular"):
        worker.repair(repo, state, task, lambda *_: pytest.fail("provider called"))


@pytest.mark.parametrize("bad", [
    "import os\n" + BASELINE,
    "@print\ndef clamp(value, lower, upper):\n    return value",
    "def clamp(value, lower, upper):\n    while True: pass\n",
    "def clamp(value, lower, upper):\n    return value.__class__\n",
])
def test_validator_independently_rejects_bad_sources(bad):
    with pytest.raises(worker.WorkerBlocked):
        worker.check_source(bad, make_task())


@pytest.mark.parametrize("values", [[], ["OLLAMA_NO_CLOUD=0"],
                                   ["OLLAMA_NO_CLOUD=1", "OLLAMA_NO_CLOUD=0"], None])
def test_provider_requires_effective_no_cloud_config(monkeypatch, values):
    monkeypatch.setattr(worker, "process", lambda *_: json.dumps(values))
    with pytest.raises(worker.WorkerBlocked):
        worker.LocalOllama.no_cloud()


def test_provider_real_contract_with_synthetic_http(monkeypatch):
    monkeypatch.setattr(worker, "process", lambda *_: '["OLLAMA_NO_CLOUD=1"]')
    calls = []
    def handler(request):
        calls.append(request.url.path)
        assert request.url.host == "127.0.0.1"
        values = {
            "/api/tags": {"models": [{"name": worker.MODEL, "digest": "d" * 64}]},
            "/api/show": {"details": {"format": "gguf"}},
            "/api/chat": {"model": worker.MODEL, "done": True, "eval_count": 20,
                          "message": {"content": GOOD}},
            "/api/ps": {"models": [{"name": worker.MODEL, "digest": "d" * 64, "size": 100}]},
        }
        if request.url.path == "/api/chat":
            payload = json.loads(request.content)
            assert payload["stream"] is False
            assert "tools" not in payload and payload["format"]["additionalProperties"] is False
        return httpx.Response(200, json=values[request.url.path])
    client = httpx.Client
    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(worker.httpx, "Client", factory)
    model = worker.LocalOllama()
    assert model(make_task(), BASELINE) == GOOD
    assert model.process_verified and model.model_digest == "d" * 64
    assert calls.count("/api/chat") == 1


@pytest.mark.parametrize("status", [302, 401, 503])
def test_http_failure_sanitized(status):
    with httpx.Client(base_url=worker.OLLAMA_URL, transport=httpx.MockTransport(
        lambda _: httpx.Response(status, text="PRIVATE_VALUE"))) as client:
        with pytest.raises(worker.WorkerBlocked) as error:
            worker.LocalOllama.request(client, "GET", "/api/tags")
        assert "PRIVATE_VALUE" not in str(error.value)


def test_workflow_preserves_guards_and_uses_real_e2e():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/ci.yml").read_text()
    assert "scripts/local_worker_e2e.py" in workflow
    assert "-e OLLAMA_NO_CLOUD=1" in workflow
    assert "127.0.0.1:11434:11434" in workflow
    assert "ollama/ollama:0.34.3@sha256:" in workflow
    assert "contents: write" not in workflow and "secrets." not in workflow
    assert "self-hosted" not in workflow and "schedule:" not in workflow
    script = (root / "scripts/local_worker_e2e.py").read_text()
    assert "session_launcher.py" in script and "command_gateway.py" in script
    assert "SESSION_LAUNCH_OK" in script and '["state_validated"] is True' in script
    assert 'vclaim["task"]["task_id"] == task_id' in script
    assert 'validation_sha": "f" * 40' in script
    assert '"git", "clone", "--no-hardlinks"' in script
    assert "LocalOllama()" in script
    assert "MockTransport" not in script
    assert " min(max(v, lo), hi)" in script  # trusted test oracle, not a model fallback
    assert 'merge_performed": False' in script


def test_independent_validator_rejects_invalid_code_on_disk(checkout):
    repo, state, task = checkout
    state.mkdir()
    (repo / task["path"]).write_text(BASELINE)
    with pytest.raises(worker.WorkerBlocked):
        worker.validate_independently(repo, task, state)


def test_no_cloud_positive_runtime_readback(monkeypatch):
    observed = []
    def inspect(argv, cwd, timeout=15):
        observed.append(argv)
        return '["PATH=/usr/bin","OLLAMA_NO_CLOUD=1"]'
    monkeypatch.setattr(worker, "process", inspect)
    worker.LocalOllama.no_cloud()
    assert observed == [["docker", "inspect", "--format", "{{json .Config.Env}}", worker.CONTAINER]]
