"""Contratos da entrada live TODO-74; nenhum teste usa rede ou credencial real."""
import json
from pathlib import Path

import pytest

from app.pre_pr_cas_gate import PrePRBlocked
from app import reqsys_todo74_readonly_probe as probe


class ReaderStub:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {
            "id": "TODO-74",
            "state": "BLOCKED",
            "repository": "ericson-j-santos/reqsys-v2-enterprise-real",
            "issue_number": 715,
        }
        self.error = error
        self.seen = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def todo(self, todo_id):
        self.seen.append(todo_id)
        if self.error:
            raise self.error
        return self.result


def _configure(monkeypatch, stub):
    pointer = Path.cwd() / "unused-private-credential.txt"
    monkeypatch.setenv("PORTFOLIO_NOTION_TOKEN_FILE", str(pointer))
    seen_paths = []

    def factory(*, token_file):
        seen_paths.append(token_file)
        return stub

    monkeypatch.setattr(
        probe.NotionReqSysTodoReader, "from_private_token_file", factory
    )
    return pointer, seen_paths


def test_missing_pointer_never_calls_reader(monkeypatch, capsys):
    monkeypatch.delenv("PORTFOLIO_NOTION_TOKEN_FILE", raising=False)
    assert probe.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        "result": "BLOCKED", "reason": "reqsys_notion_pointer_missing"
    }


def test_relative_pointer_denied(monkeypatch, capsys):
    monkeypatch.setenv("PORTFOLIO_NOTION_TOKEN_FILE", "relative.secret")
    assert probe.main() == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "reqsys_notion_private_path_invalid"


def test_exact_page_result_is_sanitized_and_cannot_authorize(monkeypatch, capsys):
    stub = ReaderStub()
    pointer, seen = _configure(monkeypatch, stub)
    assert probe.main() == 0
    assert seen == [pointer]
    assert stub.seen == ["TODO-74"]
    assert json.loads(capsys.readouterr().out) == {
        "result": "TODO74_GET_VERIFIED",
        "todo_id": "TODO-74",
        "state": "BLOCKED",
        "authorized": False,
        "dispatched": False,
    }


@pytest.mark.parametrize("result", [
    {"id": "TODO-74", "state": "READY", "repository": "ericson-j-santos/reqsys-v2-enterprise-real", "issue_number": 715},
    {"id": "TODO-75", "state": "BLOCKED", "repository": "ericson-j-santos/reqsys-v2-enterprise-real", "issue_number": 715},
    {"id": "TODO-74", "state": "BLOCKED", "repository": "ericson-j-santos/reqsys-v2-enterprise-real", "issue_number": 715, "authorized": True},
])
def test_unexpected_readback_denied(monkeypatch, capsys, result):
    _configure(monkeypatch, ReaderStub(result=result))
    assert probe.main() == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "reqsys_notion_readback_invalid"


def test_provider_exception_never_leaks_details(monkeypatch, capsys):
    _configure(monkeypatch, ReaderStub(error=RuntimeError("SENSITIVE_TOKEN_SAMPLE")))
    assert probe.main() == 2
    output = capsys.readouterr().out
    assert "SENSITIVE_TOKEN_SAMPLE" not in output
    assert json.loads(output)["reason"] == "reqsys_notion_probe_failed"


def test_known_denial_is_sanitized(monkeypatch, capsys):
    _configure(monkeypatch, ReaderStub(error=PrePRBlocked("reqsys_notion_credential_unavailable")))
    assert probe.main() == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "reqsys_notion_credential_unavailable"


def test_unknown_denial_cannot_echo_untrusted_string(monkeypatch, capsys):
    _configure(monkeypatch, ReaderStub(error=PrePRBlocked("token=SENSITIVE_TOKEN_SAMPLE")))
    assert probe.main() == 2
    output = capsys.readouterr().out
    assert "SENSITIVE_TOKEN_SAMPLE" not in output
    assert json.loads(output)["reason"] == "reqsys_notion_probe_failed"
