"""Pure controls for hosted native Ollama CI. No network/model/download invoked."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys

import pytest

from scripts import hosted_native_ollama_ci as native


def test_git_sha_pattern_accepts_actual_40_hex_and_rejects_invalid():
    assert native.HEX40.fullmatch("a" * 40)
    assert native.HEX40.fullmatch("0" * 40)
    assert native.HEX40.fullmatch("g" * 40) is None
    assert native.HEX40.fullmatch("a" * 39) is None
    assert native.HEX40.fullmatch("a" * 41) is None


def test_native_ci_requires_exact_public_repo_and_branch(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(native.NativeCiBlocked, match="ci_identity_rejected"):
        native.preflight()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", native.REPOSITORY)
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("RUNNER_OS", "Linux")
    with pytest.raises(native.NativeCiBlocked, match="ci_identity_rejected"):
        native.preflight()
    monkeypatch.setenv("GITHUB_REF", "refs/heads/" + native.BRANCH)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    with pytest.raises(native.NativeCiBlocked, match="ci_identity_rejected"):
        native.preflight()


def test_archive_uses_exact_published_digest_and_only_https():
    assert native.RELEASE_URL == (
        "https://github.com/ollama/ollama/releases/download/v0.34.3/"
        "ollama-linux-amd64.tar.zst"
    )
    assert native.RELEASE_SHA256 == (
        "e83a089fd0cd2f79ee2933cca2085846a2065f497adbc6467c402177c68423f9"
    )
    assert native.MODEL_DIGEST_PREFIX == "dae161e27b0e"


def test_digest_matches_bytes_and_detects_tamper(tmp_path: Path):
    artifact = tmp_path / "model-archive-fixture"
    artifact.write_bytes(b"published binary fixture")
    expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert native._sha256(artifact) == expected
    artifact.write_bytes(b"modified binary fixture")
    assert native._sha256(artifact) != expected


def test_command_failure_is_fail_closed_without_printing_output(monkeypatch, tmp_path):
    class Result:
        returncode = 17

    monkeypatch.setattr(native.subprocess, "run", lambda *a, **kw: Result())
    with pytest.raises(native.NativeCiBlocked, match="native_command_failed"):
        native._run(["not-run"], cwd=tmp_path)


class FakeHttpx:
    def __init__(self, models):
        self.models = models

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, path):
        assert path == "/api/tags"

        class Response:
            status_code = 200

            def json(self_inner):
                return {"models": self.models}

        return Response()


def _item(**overrides):
    model = {
        "name": native.MODEL,
        "digest": native.MODEL_DIGEST_PREFIX
        + "0" * (64 - len(native.MODEL_DIGEST_PREFIX)),
        "details": {"format": "gguf"},
    }
    model.update(overrides)
    return model


def test_model_identity_is_pinned_and_no_remote(monkeypatch):
    monkeypatch.setattr(native.httpx, "Client", lambda **_: FakeHttpx([_item()]))
    assert native.verify_model().startswith(native.MODEL_DIGEST_PREFIX)
    for candidate in (
        [_item(remote_host="untrusted.example")],
        [_item(digest="f" * 64)],
        [_item(details={"format": "onnx"})],
        [_item(), _item()],
        [],
    ):
        monkeypatch.setattr(
            native.httpx, "Client", lambda *a, models=candidate, **kw: FakeHttpx(models)
        )
        with pytest.raises(native.NativeCiBlocked):
            native.verify_model()


def test_hosted_diagnostic_uses_native_model_guard_not_docker(monkeypatch):
    # The existing diagnostic instrumentation remains in the MRO while
    # NativeStructuredOllama.no_cloud replaces Docker inspect.
    sys.path.insert(0, str(native.ROOT / "scripts"))
    from scripts.structured_worker_e2e import HostedNativeDiagnosticOllama
    import app.local_code_worker as local

    def deny_docker(*_args, **_kwargs):
        pytest.fail("Docker invocation forbidden in native adapter")

    monkeypatch.setattr(local, "process", deny_docker)
    monkeypatch.setenv("NATIVE_OLLAMA_DEV", "1")
    monkeypatch.setenv("NATIVE_MODEL_DIGEST_PREFIX", native.MODEL_DIGEST_PREFIX)
    HostedNativeDiagnosticOllama.no_cloud()
    monkeypatch.setenv("NATIVE_MODEL_DIGEST_PREFIX", "invalid")
    with pytest.raises(Exception, match="native_digest_pin_required"):
        HostedNativeDiagnosticOllama.no_cloud()


def _rag_receipt(**overrides):
    proof = {
        "rag_real_github_and_native_qwen_e2e": True,
        "pr_head_sha": "b083a23af7c55f96ee1c7f8daabbd51167bf4de6",
        "model_digest_prefix": native.MODEL_DIGEST_PREFIX,
        "model_calls": 2,
        "independent_pr_and_source_readback": True,
        "stale_head_negative_no_model_calls": True,
        "no_git_writes_or_worker_dispatch": True,
    }
    proof.update(overrides)
    return proof


def test_pr_rag_receipt_gate_checks_real_readonly_source_and_model(monkeypatch):
    import json

    observed = []

    class Result:
        returncode = 0
        stdout = json.dumps(_rag_receipt())

    def invoke(args, **kwargs):
        observed.append((args, kwargs))
        return Result()

    monkeypatch.setattr(native.subprocess, "run", invoke)
    env = {"NATIVE_OLLAMA_DEV": "1",
           "NATIVE_MODEL_DIGEST_PREFIX": native.MODEL_DIGEST_PREFIX,
           "OLLAMA_NO_CLOUD": "1"}
    receipt = native.verify_readonly_rag_e2e(env)
    assert receipt["result"] == "HOSTED_NATIVE_QWEN7B_PR_RAG_VERIFIED"
    assert receipt["independent_readback"] is True
    assert observed[0][0] == [sys.executable, "-m", "scripts.rag_real_qwen_e2e"]
    assert observed[0][1]["stderr"] == native.subprocess.DEVNULL
    assert observed[0][1]["timeout"] <= 350
    assert observed[0][1]["env"] is env


@pytest.mark.parametrize("bad", [
    {"pr_head_sha": "f" * 40},
    {"model_digest_prefix": "f" * 12},
    {"model_calls": 0},
    {"model_calls": True},
    {"stale_head_negative_no_model_calls": False},
    {"independent_pr_and_source_readback": False},
    {"no_git_writes_or_worker_dispatch": False},
    {"rag_real_github_and_native_qwen_e2e": False},
])
def test_pr_rag_receipt_gate_rejects_spoofed_proofs(monkeypatch, bad):
    import json

    class Result:
        returncode = 0
        stdout = json.dumps(_rag_receipt(**bad))

    monkeypatch.setattr(native.subprocess, "run", lambda *_a, **_k: Result())
    env = {"NATIVE_OLLAMA_DEV": "1",
           "NATIVE_MODEL_DIGEST_PREFIX": native.MODEL_DIGEST_PREFIX,
           "OLLAMA_NO_CLOUD": "1"}
    with pytest.raises(native.NativeCiBlocked, match="rag_readonly_evidence_invalid"):
        native.verify_readonly_rag_e2e(env)


def test_pr_rag_process_failure_and_missing_optin_fail_closed(monkeypatch):
    import json

    class Failed:
        returncode = 1
        stdout = json.dumps(_rag_receipt())

    monkeypatch.setattr(native.subprocess, "run", lambda *_a, **_k: Failed())
    env = {"NATIVE_OLLAMA_DEV": "1",
           "NATIVE_MODEL_DIGEST_PREFIX": native.MODEL_DIGEST_PREFIX,
           "OLLAMA_NO_CLOUD": "1"}
    with pytest.raises(native.NativeCiBlocked, match="rag_real_process_failed"):
        native.verify_readonly_rag_e2e(env)
    with pytest.raises(native.NativeCiBlocked, match="rag_native_context_invalid"):
        native.verify_readonly_rag_e2e({**env, "OLLAMA_NO_CLOUD": "0"})


def test_numeric_model_identity_and_fixed_digest(monkeypatch):
    item = _item(
        name=native.NUMERIC_MODEL,
        digest=native.NUMERIC_MODEL_DIGEST_PREFIX
        + "0" * (64 - len(native.NUMERIC_MODEL_DIGEST_PREFIX)),
    )
    monkeypatch.setattr(native.httpx, "Client", lambda **_: FakeHttpx([item]))
    assert native.verify_model(
        native.NUMERIC_MODEL, native.NUMERIC_MODEL_DIGEST_PREFIX
    ).startswith(native.NUMERIC_MODEL_DIGEST_PREFIX)
    with pytest.raises(native.NativeCiBlocked, match="model_selection_unapproved"):
        native.verify_model(native.NUMERIC_MODEL, native.MODEL_DIGEST_PREFIX)
    with pytest.raises(native.NativeCiBlocked, match="model_identity_unverified"):
        native.verify_model(native.MODEL, native.MODEL_DIGEST_PREFIX)


def test_native_numeric_queue_adapter_fail_closed_and_no_docker(monkeypatch):
    from scripts.local_worker_e2e import HostedNativeQueueOllama
    import app.local_code_worker as local

    def forbidden_docker(*_a, **_kw):
        pytest.fail("Docker forbidden from native numeric queue adapter")

    monkeypatch.setattr(local, "process", forbidden_docker)
    values = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": native.REPOSITORY,
        "HOSTED_NATIVE_OLLAMA_CI": "1",
        "NATIVE_OLLAMA_DEV": "1",
        "OLLAMA_NO_CLOUD": "1",
        "OLLAMA_HOST": "127.0.0.1:11434",
        "NATIVE_NUMERIC_DIGEST_PREFIX": native.NUMERIC_MODEL_DIGEST_PREFIX,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    HostedNativeQueueOllama.no_cloud()
    for key, wrong in (
        ("HOSTED_NATIVE_OLLAMA_CI", "0"),
        ("GITHUB_REPOSITORY", "untrusted/other"),
        ("OLLAMA_NO_CLOUD", "0"),
        ("OLLAMA_HOST", "https://remote.invalid"),
        ("NATIVE_NUMERIC_DIGEST_PREFIX", "f" * 12),
    ):
        monkeypatch.setenv(key, wrong)
        with pytest.raises(local.WorkerBlocked, match="native_numeric_queue_not_admitted"):
            HostedNativeQueueOllama.no_cloud()
        monkeypatch.setenv(key, values[key])


def test_current_owner_issue8_bindings_preserve_historical_cas_gate(monkeypatch):
    from scripts import issue_rag_real_qwen_e2e as historical
    from scripts import issue_rag_readonly_current_e2e as current

    assert historical.MAIN == "87a8de64ea5ed2e4aef1a9cde51861edb61ffe5e"
    assert historical.COMMENT_ID == 6430299388
    assert current.CURRENT_MAIN == "e22aabb14e1a646e274c331e83cc06c470607a38"
    assert current.CURRENT_OWNER_COMMENT == 6458684416

    observed = []
    monkeypatch.setattr(
        current, "run_authorized_issue",
        lambda sha, comment: observed.append((sha, comment)),
    )
    values = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": current.REPO,
        "GITHUB_REF": "refs/heads/" + current.BRANCH,
        "GITHUB_EVENT_NAME": "push",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    current.main()
    assert observed == [(current.CURRENT_MAIN, current.CURRENT_OWNER_COMMENT)]
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    with pytest.raises(RuntimeError, match="current_issue_rag_ci_identity_rejected"):
        current.main()
    assert len(observed) == 1


def _issue8_replies(**changes):
    import json

    base = {
        "result": "ISSUE8_RAG_CANDIDATE_LINES_ATTESTED",
        "worker_sha": "e22aabb14e1a646e274c331e83cc06c470607a38",
        "source_path": "docs/ISSUE_QWEN7B_DRAFT_HANDOFF.md",
        "source_sha256": "b" * 64,
        "selection_decision": "supported",
        "source_contains_acceptable_line": True,
        "model_process_verified": True,
        "model_calls": 2,
        "independent_source_readback": True,
        "raw_source_or_model_text_logged": False,
        "code_modified": False,
        "cas_write": False,
        "draft_pr_created": False,
    }
    final = {
        "result": "ISSUE8_REAL_QWEN_CITATION_VERIFIED",
        "worker_sha": base["worker_sha"],
        "source_path": base["source_path"],
        "source_sha256": base["source_sha256"],
        "model_digest_prefix": native.MODEL_DIGEST_PREFIX,
        "model_calls": 2,
        "independent_source_readback": True,
        "stale_sha_blocked_before_model": True,
        "code_modified": False,
        "cas_write": False,
        "draft_pr_created": False,
    }
    first, second = changes.get("first", {}), changes.get("second", {})
    return json.dumps({**base, **first}) + "\n" + json.dumps({**final, **second})


def test_issue8_rag_receipts_are_both_required_and_sha_bound(monkeypatch, capsys):
    env = {
        "NATIVE_OLLAMA_DEV": "1",
        "NATIVE_MODEL_DIGEST_PREFIX": native.MODEL_DIGEST_PREFIX,
        "OLLAMA_NO_CLOUD": "1",
    }

    class Result:
        returncode = 0
        stdout = _issue8_replies()

    observed = []

    def invoke(args, **kwargs):
        observed.append((args, kwargs))
        return Result()

    monkeypatch.setattr(native.subprocess, "run", invoke)
    proof = native.verify_readonly_issue_rag_e2e(env)
    assert proof["result"] == "HOSTED_NATIVE_QWEN7B_ISSUE8_RAG_VERIFIED"
    assert proof["read_only"] and proof["independent_readback"]
    assert observed[0][0] == [
        sys.executable, "-m", "scripts.issue_rag_readonly_current_e2e",
    ]
    assert observed[0][1]["stderr"] == native.subprocess.DEVNULL
    assert observed[0][1]["env"] is env
    assert "source_sha256" not in capsys.readouterr().out


@pytest.mark.parametrize("changes", [
    {"first": {"worker_sha": "f" * 40}},
    {"second": {"worker_sha": "f" * 40}},
    {"first": {"model_calls": 3}},
    {"second": {"model_digest_prefix": "f" * 12}},
    {"first": {"independent_source_readback": False}},
    {"second": {"independent_source_readback": False}},
    {"second": {"cas_write": True}},
    {"first": {"selection_decision": "insufficient"}},
    {"second": {"stale_sha_blocked_before_model": False}},
])
def test_issue8_rag_negative_spoofed_receipts(monkeypatch, changes):
    class Result:
        returncode = 0
        stdout = _issue8_replies(**changes)

    monkeypatch.setattr(native.subprocess, "run", lambda *_a, **_kw: Result())
    env = {
        "NATIVE_OLLAMA_DEV": "1",
        "NATIVE_MODEL_DIGEST_PREFIX": native.MODEL_DIGEST_PREFIX,
        "OLLAMA_NO_CLOUD": "1",
    }
    with pytest.raises(native.NativeCiBlocked, match="issue_rag_receipt_unverified"):
        native.verify_readonly_issue_rag_e2e(env)
