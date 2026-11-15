"""Validate the real workflow shape and security invariants."""
from pathlib import Path
import yaml

PATH = Path(".github/workflows/issue62-governed-continuation.yml")


def workflow():
    parsed = yaml.load(PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict)
    return parsed


def test_workflow_yaml_has_active_triggers_and_steps():
    doc = workflow()
    assert "workflow_dispatch" in doc["on"]
    assert "repository_dispatch" in doc["on"]
    job = doc["jobs"]["continuation"]
    assert job["runs-on"] == ["self-hosted", "pc24x7", "runtime-dev"]
    assert isinstance(job["steps"], list) and len(job["steps"]) >= 2
    assert doc["permissions"] == {"contents": "read"}


def test_checkout_never_uses_untrusted_task_sha():
    job = workflow()["jobs"]["continuation"]
    checkout = next(s for s in job["steps"] if
                    s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert checkout["with"]["persist-credentials"] == "false"
    assert job["env"]["EWP_CHECKED_OUT_SHA"] == "${{ github.sha }}"
    assert job["env"]["EWP_EXPECTED_REPOSITORY"] == (
        "ericson-j-santos/engineering-worker-pool"
    )
    for step in job["steps"]:
        if "uses" in step:
            assert len(step["uses"].split("@")[-1]) == 40


def test_workflow_does_not_expose_secret_or_trust_boolean_attestations():
    job = workflow()["jobs"]["continuation"]
    env = job["env"]
    assert "EWP_WORKER_POOL_URL" not in env
    assert "EWP_WORKER_POOL_TOKEN" not in env
    assert "EWP_WORKER_POOL_PORT" in env
    assert "EWP_WORKER_POOL_TOKEN_FILE" in env
    assert "EWP_GATEWAY_ATTESTED" not in env
    assert "EWP_SESSION_LAUNCH_OK" not in env
    assert "EWP_HANDLER_ATTESTED" not in env
    assert "secrets." not in PATH.read_text(encoding="utf-8")
