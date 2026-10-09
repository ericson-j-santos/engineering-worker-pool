"""Synthetic transports test preparation policy; real pull remains a CI step."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pull_local_model_image as image


def result(ok=True, text=""):
    return SimpleNamespace(returncode=0 if ok else 125, stdout=text, stderr="")


def success():
    return result(text=json.dumps([image.REPO_DIGEST]))


TIMEOUT = 'Get "https://auth.docker.io/token": context deadline exceeded'


def test_image_pull_and_digest_are_checked(monkeypatch):
    commands = []
    def command(argv):
        commands.append(argv)
        return result() if len(commands) == 1 else success()
    monkeypatch.setattr(image, "command", command)
    monkeypatch.setattr(image, "authorization_ready", lambda: pytest.fail("no retry needed"))
    assert image.prepare() == 1
    assert commands[0] == ["docker", "pull", image.IMAGE]
    assert commands[1][-1] == image.IMAGE


def test_timeout_requires_changed_precondition_then_verified_pull(monkeypatch):
    calls = []
    outputs = iter([result(False, TIMEOUT), result(), success()])
    monkeypatch.setattr(image, "command", lambda argv: (calls.append("command"), next(outputs))[1])
    monkeypatch.setattr(image.time, "sleep", lambda duration: calls.append("delay"))
    monkeypatch.setattr(image, "authorization_ready", lambda: (calls.append("readiness"), True)[1])
    assert image.prepare() == 2
    assert calls == ["command", "delay", "readiness", "command", "command"]


@pytest.mark.parametrize("error", [
    "denied", "unauthorized", "toomanyrequests", "manifest unknown",
    "digest mismatch", "not found", "generic error",
])
def test_non_transient_errors_never_retry(monkeypatch, error):
    calls = []
    monkeypatch.setattr(image, "command", lambda argv: (calls.append(argv), result(False, error))[1])
    monkeypatch.setattr(image, "authorization_ready", lambda: pytest.fail("not eligible"))
    with pytest.raises(image.PreparationBlocked):
        image.prepare()
    assert len(calls) == 1


def test_unavailable_registry_blocks_without_repeating_pull(monkeypatch):
    calls = []
    monkeypatch.setattr(image, "command", lambda argv: (calls.append(argv), result(False, TIMEOUT))[1])
    monkeypatch.setattr(image.time, "sleep", lambda _: None)
    monkeypatch.setattr(image, "authorization_ready", lambda: False)
    with pytest.raises(image.PreparationBlocked, match="image_registry_not_ready"):
        image.prepare()
    assert len(calls) == 1


def test_second_timeout_is_terminal(monkeypatch):
    calls = []
    monkeypatch.setattr(image, "command", lambda argv: (calls.append(argv), result(False, TIMEOUT))[1])
    monkeypatch.setattr(image.time, "sleep", lambda _: None)
    monkeypatch.setattr(image, "authorization_ready", lambda: True)
    with pytest.raises(image.PreparationBlocked, match="image_pull_failed"):
        image.prepare()
    assert len(calls) == 2


@pytest.mark.parametrize("payload", ["{}", "not json", '["wrong-digest"]'])
def test_pull_success_without_correct_digest_is_not_success(monkeypatch, payload):
    outputs = iter([result(), result(text=payload)])
    monkeypatch.setattr(image, "command", lambda argv: next(outputs))
    with pytest.raises(image.PreparationBlocked):
        image.prepare()


@pytest.mark.parametrize("status,payload,expected", [
    (200, {"token": "PRIVATE_VALUE", "expires_in": 300}, True),
    (200, {"token": "", "expires_in": 300}, False),
    (200, {"token": "PRIVATE_VALUE", "expires_in": 0}, False),
    (200, {"token": "PRIVATE_VALUE", "expires_in": True}, False),
    (401, {}, False), (301, {}, False), (429, {}, False), (503, {}, False),
])
def test_public_authorization_readiness_without_credentials(monkeypatch, capsys, status, payload, expected):
    real = httpx.Client
    def handler(request):
        assert request.url.host == "auth.docker.io" and request.url.path == "/token"
        assert request.url.params["scope"] == "repository:ollama/ollama:pull"
        assert "authorization" not in request.headers
        return httpx.Response(status, json=payload)
    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return real(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(image.httpx, "Client", client)
    assert image.authorization_ready() is expected
    assert "PRIVATE_VALUE" not in capsys.readouterr().out


def test_subprocess_timeout_is_terminal_sanitized(monkeypatch):
    def run(*args, **kwargs):
        assert kwargs["timeout"] == 75 and kwargs["check"] is False
        raise subprocess.TimeoutExpired("PRIVATE_VALUE", 75)
    monkeypatch.setattr(image.subprocess, "run", run)
    with pytest.raises(image.PreparationBlocked, match="^image_process_unavailable$"):
        image.command(["docker", "pull", image.IMAGE])


def test_ci_identity_guard_does_not_run_commands(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    monkeypatch.setattr(image, "prepare", lambda: pytest.fail("wrong runtime"))
    monkeypatch.setattr(sys, "argv", ["pull_local_model_image.py"])
    assert image.main() == 2
    assert capsys.readouterr().out == "IMAGE_PREPARATION_BLOCKED ci_identity\n"


def test_ci_uses_verified_digest_before_starting_same_container():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
    content = path.read_text()
    assert content.index("python scripts/pull_local_model_image.py") < content.index("docker run -d")
    assert "--pull=never" in content
    assert image.IMAGE in content
    assert "OLLAMA_NO_CLOUD=1" in content
