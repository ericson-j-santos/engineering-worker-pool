"""Unit gates for hosted-only candidate isolation (not physical E2E)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.candidate_canary_probe import CanaryBlocked, context, validate_native_identity

SHA = "a" * 40
REQSYS = "b" * 40


def native_fixture(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    state = tmp_path / "wp-canary-38390123456-1"
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    token = state / "token"
    token.write_text("test-token-value", encoding="ascii")
    token.chmod(0o400)
    db = state / "canary.db"
    db.write_bytes(b"fixture")
    db.chmod(0o600)
    data = dict(root=root, state_dir=state, token_path=token, db_path=db,
                argv=("/usr/bin/python", "-m", "uvicorn", "app.main:app",
                      "--host", "127.0.0.1", "--port", "18098"),
                cwd=root, owner_uid=os.geteuid(), current_uid=os.geteuid())
    return data


@pytest.mark.skipif(os.name != "posix", reason="Linux-only native file isolation")
def test_native_identity_is_pinned_and_isolated(tmp_path):
    validate_native_identity(**native_fixture(tmp_path))


@pytest.mark.skipif(os.name != "posix", reason="Linux-only native file isolation")
@pytest.mark.parametrize("change", [
    ("owner_uid", 0),
    ("current_uid", 0),
    ("argv", ("/usr/bin/python", "-m", "uvicorn", "app.main:app",
              "--host", "0.0.0.0", "--port", "18098")),
    ("argv", ("/usr/bin/python", "-m", "uvicorn", "app.other:app",
              "--host", "127.0.0.1", "--port", "18098")),
    ("argv", ("/usr/bin/python", "-m", "uvicorn", "app.main:app",
              "--host", "127.0.0.1", "--port", "18099")),
])
def test_native_identity_refuses_privileges_other_app_and_network(tmp_path, change):
    d = native_fixture(tmp_path)
    d[change[0]] = change[1]
    with pytest.raises(CanaryBlocked):
        validate_native_identity(**d)


@pytest.mark.skipif(os.name != "posix", reason="Linux-only native file isolation")
@pytest.mark.parametrize("change", ["token_world_readable", "db_world_readable",
                                    "state_world_readable", "wrong_cwd",
                                    "token_symlink"])
def test_native_identity_rejects_unsafe_paths(tmp_path, change):
    d = native_fixture(tmp_path)
    if change == "token_world_readable":
        d["token_path"].chmod(0o644)
    elif change == "db_world_readable":
        d["db_path"].chmod(0o644)
    elif change == "state_world_readable":
        d["state_dir"].chmod(0o755)
    elif change == "wrong_cwd":
        d["cwd"] = tmp_path
    elif change == "token_symlink":
        original = d["token_path"]
        original.unlink()
        original.symlink_to(d["db_path"])
    with pytest.raises(CanaryBlocked):
        validate_native_identity(**d)


def test_positive_ci_identity(tmp_path: Path) -> None:
    consumer = tmp_path / ".reqsys-consumer"
    consumer.mkdir()
    env = {
        "EXPECTED_SHA": SHA, "REQSYS_SHA": REQSYS, "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux", "GITHUB_REPOSITORY": "ericson-j-santos/engineering-worker-pool",
        "GITHUB_EVENT_NAME": "pull_request", "GITHUB_RUN_ID": "38390123456",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    with patch("scripts.candidate_canary_probe.command", side_effect=[SHA, REQSYS, ""]):
        assert context(tmp_path, consumer, env) == (SHA, REQSYS, "canary-38390123456-1")


@pytest.mark.parametrize("change", [
    ("GITHUB_ACTIONS", "false"), ("RUNNER_OS", "Windows"),
    ("GITHUB_EVENT_NAME", "issue_comment"), ("GITHUB_REPOSITORY", "fake/repo"),
    ("REQSYS_SHA", "notsha"), ("EXPECTED_SHA", "deadbeef"),
])
def test_negative_ci_identity(tmp_path: Path, change) -> None:
    consumer = tmp_path / ".reqsys-consumer"
    consumer.mkdir()
    env = {
        "EXPECTED_SHA": SHA, "REQSYS_SHA": REQSYS, "GITHUB_ACTIONS": "true",
        "RUNNER_OS": "Linux", "GITHUB_REPOSITORY": "ericson-j-santos/engineering-worker-pool",
        "GITHUB_EVENT_NAME": "pull_request", "GITHUB_RUN_ID": "38390123456",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    env[change[0]] = change[1]
    with patch("scripts.candidate_canary_probe.command", side_effect=[SHA, REQSYS, ""]):
        with pytest.raises(CanaryBlocked):
            context(tmp_path, consumer, env)


def test_native_workflow_never_invokes_docker():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/candidate-canary.yml").read_text(encoding="utf-8")
    assert "python -m uvicorn app.main:app --host 127.0.0.1 --port 18098" in workflow
    assert "python scripts/candidate_canary_probe.py" in workflow
    assert "docker " not in workflow.lower()
    assert "docker run" not in workflow.lower()
    assert "CANARY_DB_PATH" in workflow


def test_workflow_uses_single_pinned_reqsys_consumer_sha():
    """Pin one immutable ReqSys consumer SHA; reject duplicated/drifting checkout refs."""
    import re

    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/candidate-canary.yml").read_text(
        encoding="utf-8"
    )

    def assert_single_pin(config: str) -> None:
        pins = re.findall(r"(?m)^      REQSYS_SHA: ([0-9a-f]{40})$", config)
        assert len(pins) == 1
        checkout = config.split("      - name: Checkout fixed ReqSys consumer\n", 1)
        assert len(checkout) == 2
        block = checkout[1].split("\n      - name:", 1)[0]
        assert block.count("          ref: ${{ env.REQSYS_SHA }}") == 1
        assert 'test "$(git -C .reqsys-consumer rev-parse HEAD)" = "$REQSYS_SHA"' in config
        assert "persist-credentials: false" in block

    assert_single_pin(workflow)
    assert_single_pin(workflow)  # Idempotent check on immutable input.
    drifted = workflow.replace("ref: ${{ env.REQSYS_SHA }}", "ref: " + "f" * 40, 1)
    duplicate = workflow.replace("      REQSYS_SHA: ", "      REQSYS_SHA: " + "f" * 40 + "\n      REQSYS_SHA: ", 1)
    for mutation in (drifted, duplicate):
        assert mutation != workflow
        with pytest.raises(AssertionError):
            assert_single_pin(mutation)
