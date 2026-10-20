"""Keep both distinct continuity proofs on one fail-closed PR execution chain."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CALLER = ".github/workflows/hosted-checkpoint-continuity.yml"
CALLED = ".github/workflows/hosted-continuity-rehearsal.yml"
OWNER_GUARD = (
    "github.repository == 'ericson-j-santos/engineering-worker-pool' && "
    "github.event.pull_request.head.repo.full_name == github.repository && "
    "github.event.pull_request.user.login == 'ericson-j-santos' && "
    "github.actor == 'ericson-j-santos'"
)
PRODUCER_GUARD = (
    "always() && !cancelled() && "
    "github.repository == 'ericson-j-santos/engineering-worker-pool' && "
    "((github.event_name == 'push' && needs.store_rehearsal.result == 'skipped') || "
    "(github.event_name == 'pull_request' && needs.store_rehearsal.result == 'success' && "
    "github.event.pull_request.head.repo.full_name == github.repository && "
    "github.event.pull_request.user.login == 'ericson-j-santos' && "
    "github.actor == 'ericson-j-santos'))"
)
REQUIRED_PATHS = {
    "scripts/hosted_checkpoint_continuity.py",
    "scripts/hosted_rehearsal.py",
    "tests/test_hosted_checkpoint_continuity.py",
    "tests/test_hosted_launcher_contract.py",
    "tests/test_hosted_rehearsal.py",
    "tests/test_continuity_workflow_coordination.py",
    "config/hosted-rehearsal-gateway.json",
    "docs/HOSTED_COLD_CHECKPOINT_CONTINUITY.md",
    "docs/HOSTED_CONTINUITY_REHEARSAL.md",
    CALLER, CALLED,
}


def parse(text: str) -> dict:
    # BaseLoader preserves "on" rather than applying YAML 1.1 bool coercion.
    value = yaml.load(text, Loader=yaml.BaseLoader)
    assert isinstance(value, dict)
    return value


def normalized(value: str) -> str:
    return " ".join(value.split())


def assert_coordination(caller: dict, called: dict) -> None:
    assert set(called["on"]) == {"workflow_call"}
    assert set(caller["on"]) == {"push", "pull_request"}
    assert caller["on"]["push"]["branches"] == [
        "feat/hosted-cold-checkpoint-resume-20261020"
    ]
    assert REQUIRED_PATHS <= set(caller["on"]["pull_request"]["paths"])
    for workflow in (caller, called):
        assert workflow["permissions"] == {"contents": "read"}
        for job in workflow["jobs"].values():
            assert "secrets" not in job and "continue-on-error" not in job
            assert "environment" not in job and "permissions" not in job
            for step in job.get("steps", []):
                assert "continue-on-error" not in step
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] == "false"
    jobs = caller["jobs"]
    assert set(jobs) == {"store_rehearsal", "producer", "consumer"}
    call = jobs["store_rehearsal"]
    assert call["uses"] == "./" + CALLED  # Same commit, never @main.
    assert "steps" not in call and "runs-on" not in call
    assert normalized(call["if"]) == "github.event_name == 'pull_request' && " + OWNER_GUARD
    assert jobs["producer"]["needs"] == "store_rehearsal"
    assert normalized(jobs["producer"]["if"]) == PRODUCER_GUARD
    assert jobs["consumer"]["needs"] == "producer"
    assert "if" not in jobs["consumer"]
    for name in ("producer", "consumer"):
        assert jobs[name]["runs-on"] == "ubuntu-latest"
        assert int(jobs[name]["timeout-minutes"]) <= 6
        assert jobs[name]["env"]["EXPECTED_SHA"] == (
            "${{ github.event.pull_request.head.sha || github.sha }}"
        )
        commands = [step.get("run") for step in jobs[name]["steps"]]
        assert "python -m pip install --disable-pip-version-check -r requirements.txt" in commands
        assert (
            "python -B scripts/hosted_checkpoint_continuity.py --stage "
            + name + " --rules-scripts _rules/scripts"
        ) in commands
    assert "python -m pytest -ra" in [
        step.get("run") for step in jobs["producer"]["steps"]
    ]
    assert set(called["jobs"]) == {"produce", "resume"}
    assert normalized(called["jobs"]["produce"]["if"]) == OWNER_GUARD
    assert called["jobs"]["resume"]["needs"] == "produce"
    for name in ("produce", "resume"):
        job = called["jobs"][name]
        assert job["runs-on"] == "ubuntu-24.04"
        assert int(job["timeout-minutes"]) <= 6
        assert "python -B scripts/hosted_rehearsal.py run-" + name in [
            step.get("run") for step in job["steps"]
        ]
    # The caller and callee must not contend for the same concurrency lock.
    assert caller["concurrency"]["group"] != called["concurrency"]["group"]


def documents() -> tuple[dict, dict]:
    return tuple(parse((ROOT / path).read_text(encoding="utf-8")) for path in (CALLER, CALLED))


def test_both_proofs_are_required_in_one_pr_chain():
    assert_coordination(*documents())


@pytest.mark.parametrize("fault", [
    "duplicate-trigger", "no-call", "mutable-ref", "no-dependency",
    "ignore-store-failure", "allow-skipped-pr", "allow-fork", "no-consumer",
    "write-permissions", "persist-credentials", "suppressed-test",
    "missing-dependency-install", "self-hosted", "missing-path",
    "same-lock",
])
def test_coordination_rejects_false_positive_or_duplicate_dispatch(fault):
    caller, called = copy.deepcopy(documents())
    jobs = caller["jobs"]
    if fault == "duplicate-trigger":
        called["on"]["pull_request"] = {}
    elif fault == "no-call":
        jobs.pop("store_rehearsal")
    elif fault == "mutable-ref":
        jobs["store_rehearsal"]["uses"] += "@main"
    elif fault == "no-dependency":
        jobs["producer"]["needs"] = ""
    elif fault == "ignore-store-failure":
        jobs["producer"]["if"] = "always()"
    elif fault == "allow-skipped-pr":
        jobs["producer"]["if"] = jobs["producer"]["if"].replace(
            "needs.store_rehearsal.result == 'success'",
            "needs.store_rehearsal.result == 'skipped'",
        )
    elif fault == "allow-fork":
        jobs["store_rehearsal"]["if"] = "github.event_name == 'pull_request'"
    elif fault == "no-consumer":
        jobs.pop("consumer")
    elif fault == "write-permissions":
        caller["permissions"]["contents"] = "write"
    elif fault == "persist-credentials":
        jobs["consumer"]["steps"][0]["with"]["persist-credentials"] = "true"
    elif fault == "suppressed-test":
        jobs["producer"]["steps"][-1]["continue-on-error"] = "true"
    elif fault == "missing-dependency-install":
        jobs["consumer"]["steps"] = [
            step for step in jobs["consumer"]["steps"]
            if step.get("name") != "Install repository test dependencies"
        ]
    elif fault == "self-hosted":
        jobs["consumer"]["runs-on"] = "self-hosted"
    elif fault == "missing-path":
        caller["on"]["pull_request"]["paths"].remove("scripts/hosted_rehearsal.py")
    else:
        called["concurrency"]["group"] = caller["concurrency"]["group"]
    with pytest.raises((AssertionError, KeyError)):
        assert_coordination(caller, called)


def test_parser_line_endings_and_validation_are_idempotent():
    caller_text, called_text = (
        (ROOT / path).read_text(encoding="utf-8") for path in (CALLER, CALLED)
    )
    for ending in ("\n", "\r\n", "\n"):
        parsed = tuple(parse(text.replace("\n", ending)) for text in (caller_text, called_text))
        before = copy.deepcopy(parsed)
        assert_coordination(*parsed)
        assert parsed == before
