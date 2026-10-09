from __future__ import annotations

import copy
import json
from unittest.mock import patch

import pytest

from app.ci_entrypoint import COMMAND, OWNER, REPOSITORY, admitted, main

SHA = "a" * 40


def event():
    return {
        "action": "created",
        "repository": {"full_name": REPOSITORY, "private": False},
        "issue": {"number": 13, "state": "open"},
        "comment": {
            "id": 123,
            "body": COMMAND,
            "user": {"login": OWNER, "type": "User"},
        },
    }


def test_valid_event_is_pure_and_replay_stable():
    data = event()
    before = copy.deepcopy(data)
    assert admitted(data, SHA, OWNER, REPOSITORY)
    assert admitted(data, SHA, OWNER, REPOSITORY)
    assert data == before


@pytest.mark.parametrize(("section", "key", "value"), [
    ("", "action", "edited"),
    ("repository", "full_name", "other/repo"),
    ("repository", "private", True),
    ("issue", "number", 14),
    ("issue", "number", True),
    ("issue", "state", "closed"),
    ("issue", "pull_request", {}),
    ("comment", "body", COMMAND + " extra"),
    ("comment", "body", COMMAND + "\n"),
    ("comment", "body", "/worker recover"),
    ("comment", "id", True),
    ("comment", "id", 0),
    ("comment", "user", {"login": "other", "type": "User"}),
    ("comment", "user", {"login": OWNER, "type": "Bot"}),
])
def test_unauthorized_events_are_blocked(section, key, value):
    data = event()
    target = data[section] if section else data
    target[key] = value
    assert not admitted(data, SHA, OWNER, REPOSITORY)


@pytest.mark.parametrize("data", [None, [], {}, {"comment": []}])
def test_malformed_events_are_blocked(data):
    assert not admitted(data, SHA, OWNER, REPOSITORY)


@pytest.mark.parametrize(("sha", "actor", "repo"), [
    ("main", OWNER, REPOSITORY),
    ("a" * 39, OWNER, REPOSITORY),
    (SHA, "github-actions[bot]", REPOSITORY),
    (SHA, OWNER, "other/repo"),
])
def test_environment_identity_is_required(sha, actor, repo):
    assert not admitted(event(), sha, actor, repo)


def test_entrypoint_positive_negative_and_replay(tmp_path, monkeypatch, capsys):
    path = tmp_path / "github-event.json"
    path.write_text(json.dumps(event()), encoding="utf-8")
    values = {
        "GITHUB_EVENT_PATH": str(path),
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "issue_comment",
        "EXPECTED_SHA": SHA,
        "GITHUB_ACTOR": OWNER,
        "GITHUB_REPOSITORY": REPOSITORY,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert main() == 0
    assert main() == 0
    bad = event()
    bad["comment"]["body"] = "PRIVATE_VALUE"
    path.write_text(json.dumps(bad), encoding="utf-8")
    assert main() == 2
    path.write_text("{invalid", encoding="utf-8")
    assert main() == 2
    output = capsys.readouterr().out
    assert "PRIVATE_VALUE" not in output
    assert output.count("NATIVE_CI_ADMITTED") == 2
    assert output.count("NATIVE_CI_BLOCKED") == 2


def test_not_a_github_comment_is_blocked(tmp_path, monkeypatch):
    path = tmp_path / "github-event.json"
    path.write_text(json.dumps(event()), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    assert main() == 2


def test_invalid_path_is_sanitized(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_PATH", "/missing/PRIVATE_VALUE")
    assert main() == 2
    assert capsys.readouterr().out == "NATIVE_CI_BLOCKED\n"


def test_guard_control_detects_deliberate_identity_failure():
    with patch("app.ci_entrypoint.OWNER", "invalid-owner"):
        assert not admitted(event(), SHA, OWNER, REPOSITORY)

def test_workflow_keeps_scope_and_has_no_host_or_billing_side_effects():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    assert "issue_comment:\n    types: [created]" in text
    assert "github.event.issue.number == 13" in text
    assert "github.event.comment.body == '/worker validate'" in text
    assert "github.event.comment.user.type == 'User'" in text
    assert "python -m app.ci_entrypoint" in text
    assert "runs-on: ubuntu-latest" in text
    assert "persist-credentials: false" in text
    assert "schedule:" not in text
    assert "self-hosted" not in text
    assert "actions: write" not in text
    assert "contents: write" not in text
    assert "secrets." not in text


@pytest.mark.parametrize("invalid", [None, 123, [], {}])
def test_non_text_sha_is_rejected(invalid):
    assert not admitted(event(), invalid, OWNER, REPOSITORY)
