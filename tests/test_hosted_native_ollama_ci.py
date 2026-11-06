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
