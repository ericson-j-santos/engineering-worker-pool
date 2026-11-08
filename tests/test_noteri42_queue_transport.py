"""HTTP Noteri42 queue transport: exact payload, no hidden dispatch or token access."""
import pytest

from app.native_one_shot import NativeExecutorBlocked
from app.native_pool_http import LocalWorkerApi
from app.noteri42_prepr_admission import (
    BRANCH, CORRELATION_ID, ISSUE, REPO, REQUEST_ID, SOURCE_SHA,
)


def payload():
    return {
        "repository": REPO, "issue_number": ISSUE,
        "request_id": REQUEST_ID,
        "correlation_id": CORRELATION_ID,
        "priority": 10, "base_sha": SOURCE_SHA,
        "target_branch": BRANCH, "max_attempts": 1,
        "fencing_required": True,
    }


def response(p):
    return {
        "created": True,
        "task": {
            "task_id": "cwp-" + "a" * 24,
        },
    }


def observed(p):
    return {
        **{key: p[key] for key in (
            "repository", "issue_number", "request_id",
            "correlation_id", "priority", "base_sha",
        )},
        "task_id": "cwp-" + "a" * 24,
        "branch": BRANCH,
        "fencing_required": True,
        "max_attempts": 1,
        "state": "queued",
    }


def client():
    # Create client object without connecting, credential, server or network.
    instance = object.__new__(LocalWorkerApi)
    effects = []

    def request(method, route, body, *, expected_statuses):
        effects.append((method, route, body, expected_statuses))
        return response(body)

    def read(task_id):
        assert task_id == "cwp-" + "a" * 24
        effects.append(("GET", task_id))
        return observed(payload())

    instance._request = request
    instance.get_task = read
    return instance, effects


def test_exact_payload_posts_once_then_reads_back():
    instance, effects = client()
    accepted = instance.enqueue_fenced_noteri42(payload())
    assert accepted["created"] is True
    assert accepted["task"]["state"] == "queued"
    assert effects[0] == ("POST", "/v1/tasks", payload(), (200, 201))
    assert effects[1] == ("GET", "cwp-" + "a" * 24)
    assert len(effects) == 2


@pytest.mark.parametrize("key,value", [
    ("repository", "other/repo"),
    ("issue_number", 8),
    ("request_id", "issue-" + "f" * 64),
    ("correlation_id", "untrusted"),
    ("priority", 2),
    ("base_sha", "f" * 40),
    ("target_branch", "main"),
    ("max_attempts", 2),
    ("fencing_required", False),
    ("fencing_required", 1),
])
def test_wrong_target_never_contacts_api(key, value):
    instance, effects = client()
    altered = payload()
    altered[key] = value
    with pytest.raises(NativeExecutorBlocked, match="noteri42_worker_payload_invalid"):
        instance.enqueue_fenced_noteri42(altered)
    assert effects == []


def test_missing_field_and_injected_commands_block():
    instance, effects = client()
    bad = payload()
    del bad["target_branch"]
    with pytest.raises(NativeExecutorBlocked, match="noteri42_worker_payload_invalid"):
        instance.enqueue_fenced_noteri42(bad)
    other = payload()
    other["command"] = "run_untrusted"
    with pytest.raises(NativeExecutorBlocked, match="noteri42_worker_payload_invalid"):
        instance.enqueue_fenced_noteri42(other)
    assert effects == []


def test_readback_mismatch_blocks_even_after_single_post():
    instance, effects = client()
    instance.get_task = lambda _id: dict(observed(payload()), branch="main")
    with pytest.raises(NativeExecutorBlocked, match="noteri42_worker_readback_mismatch"):
        instance.enqueue_fenced_noteri42(payload())
    assert len(effects) == 1
