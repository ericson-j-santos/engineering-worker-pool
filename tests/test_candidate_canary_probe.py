"""Unit gates for hosted-only candidate isolation (not physical E2E)."""
from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.candidate_canary_probe import CanaryBlocked, context, inspect_container


SHA = "a" * 40
REQSYS = "b" * 40
IMAGE = "sha256:" + "c" * 64
CONTAINER = "wp-canary-38390123456-1"


def fake_container(token_path: Path) -> dict:
    return {
        "Image": IMAGE,
        "Name": "/" + CONTAINER,
        "State": {"Running": True},
        "Config": {"User": "reqsys"},
        "HostConfig": {
            "Privileged": False,
            "CapDrop": ["ALL"],
            "ReadonlyRootfs": True,
            "NetworkMode": "bridge",
            "PortBindings": {"8097/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18098"}]},
        },
        "Mounts": [
            {"Destination": "/data", "Type": "volume", "Name": CONTAINER, "RW": True},
            {"Destination": "/run/secrets/canary-token", "Type": "bind",
             "Source": str(token_path), "RW": False},
        ],
    }


def test_positive_isolation_contract(tmp_path: Path) -> None:
    data = fake_container(tmp_path / "token")
    inspect_container(data, image_id=IMAGE, name=CONTAINER, volume=CONTAINER,
                      token_path=tmp_path / "token")


@pytest.mark.parametrize("mutation", [
    lambda d: d["HostConfig"].update(Privileged=True),
    lambda d: d["HostConfig"].update(ReadonlyRootfs=False),
    lambda d: d["HostConfig"].update(PortBindings={"8097/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18098"}]}),
    lambda d: d["HostConfig"].update(CapDrop=[]),
    lambda d: d["Mounts"].append({"Destination": "/live", "Type": "volume", "RW": True}),
    lambda d: d["Mounts"][0].update(Name="production-volume"),
    lambda d: d["Mounts"][1].update(RW=True),
    lambda d: d.update(Image="sha256:" + "d" * 64),
    lambda d: d["Config"].update(User="root"),
    lambda d: d["State"].update(Running=False),
])
def test_negative_isolation_contract(tmp_path: Path, mutation) -> None:
    data = fake_container(tmp_path / "token")
    mutation(data)
    with pytest.raises(CanaryBlocked):
        inspect_container(data, image_id=IMAGE, name=CONTAINER, volume=CONTAINER,
                          token_path=tmp_path / "token")


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
