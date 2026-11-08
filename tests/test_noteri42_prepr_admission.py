"""Synthetic contract only. No real model, queue, GitHub token or host."""
from __future__ import annotations

from copy import deepcopy

import pytest

from app.native_one_shot import NativeExecutorBlocked
from app.noteri42_prepr_admission import (
    BASE_SHA, BRANCH, CORRELATION_ID, EXPECTED_FILES, ISSUE,
    REPO, REQUEST_ID, RULES_SHA, SOURCE_SHA, TODO_ID,
    admit_exact,
)


def official() -> dict:
    return {
        "repository": REPO, "issue_number": ISSUE,
        "issue_state": "open", "main_sha": BASE_SHA,
        "branch_head_sha": SOURCE_SHA,
        "base_is_ancestor": True,
        "changed_paths": list(EXPECTED_FILES),
    }


def todo() -> dict:
    return {
        "todo_id": TODO_ID, "external_id": REPO + "#42",
        "correlation_id": CORRELATION_ID,
        "status": "AUTORIZADO_PARA_EXECUCAO",
        "source_sha": SOURCE_SHA, "canonical": True,
    }


def approval() -> dict:
    return {
        "repository": REPO, "issue_number": ISSUE,
        "source_sha": SOURCE_SHA, "approved": True,
        "authority": "authenticated_owner_approval",
    }


def cost() -> dict:
    return {
        "incremental_brl": "0.00",
        "overage_hard_blocked": True,
        "runtime_covered": True,
    }


def host() -> dict:
    return {
        "source_sha": SOURCE_SHA, "rules_sha": RULES_SHA,
        "host_id": "Noteri",
        "physical_identity_verified": True,
        "profile": "NORMAL",
        "session_launcher": "SESSION_LAUNCH_OK",
        "state_validated": True,
        "gateway_verified": True,
        "exclusive_worktree": True,
        "exclusive_lease": True,
        "native_deps_ready": True,
        "model_name": "qwen2.5-coder:7b",
        "model_digest": "a" * 64,
        "model_local_only": True,
        "ready_for_work": True,
    }


class Pool:
    def __init__(self, *, lanes=None):
        self.lanes = lanes if lanes is not None else [{
            "repository": REPO, "enabled": True, "max_in_flight": 1,
        }]
        self.posts = []
        self.list_calls = 0

    def list_repositories(self):
        self.list_calls += 1
        return deepcopy(self.lanes)

    def enqueue_fenced_noteri42(self, payload):
        self.posts.append(deepcopy(payload))
        task = {
            **{key: payload[key] for key in (
                "repository", "issue_number", "request_id",
                "correlation_id", "priority", "base_sha",
            )},
            "task_id": "cwp-" + "a" * 24,
            "branch": payload["target_branch"],
            "fencing_required": True, "max_attempts": 1,
            "state": "queued",
        }
        return {"task_id": task["task_id"], "created": True, "task": task}


def fixtures(*, pool=None):
    counts = {"source": 0, "todo": 0, "approval": 0,
              "host": 0, "cost": 0}
    values = {"source": official(), "todo": todo(),
              "approval": approval(), "host": host(), "cost": cost()}

    def read(name):
        counts[name] += 1
        return deepcopy(values[name])

    def source_reader(repo, number):
        assert (repo, number) == (REPO, ISSUE)
        return read("source")

    def todo_reader(todo_id):
        assert todo_id == TODO_ID
        return read("todo")

    def approval_reader(repo, number):
        assert (repo, number) == (REPO, ISSUE)
        return read("approval")

    def cost_reader():
        return read("cost")

    def host_reader():
        return read("host")

    services = {
        "source_reader": source_reader, "todo_reader": todo_reader,
        "approval_reader": approval_reader, "cost_reader": cost_reader,
        "host_reader": host_reader,
        "pool": pool or Pool(), "environment": "dev",
    }
    return services, values, counts


def test_enqueues_one_fenced_task_only_after_two_independent_readbacks():
    args, _values, counts = fixtures()
    result = admit_exact(**args)
    assert result == {
        "result": "NOTERI42_FENCED_ENQUEUED",
        "task_id": "cwp-" + "a" * 24,
        "created": True,
        "repository": REPO,
        "source_sha": SOURCE_SHA,
        "todo_id": TODO_ID,
        "ready_for_pr": False,
        "model_executed": False,
        "cas_materialized": False,
    }
    assert counts == dict.fromkeys(counts, 2)
    assert args["pool"].list_calls == 1
    assert args["pool"].posts == [{
        "repository": REPO, "issue_number": ISSUE,
        "request_id": REQUEST_ID, "correlation_id": CORRELATION_ID,
        "priority": 10, "base_sha": SOURCE_SHA,
        "target_branch": BRANCH, "max_attempts": 1,
        "fencing_required": True,
    }]


@pytest.mark.parametrize("name,field,corruption,reason", [
    ("source", "issue_state", "closed", "noteri42_source_authority_unverified"),
    ("source", "branch_head_sha", "f" * 40, "noteri42_source_authority_unverified"),
    ("source", "main_sha", "f" * 40, "noteri42_source_authority_unverified"),
    ("source", "changed_paths", [], "noteri42_source_authority_unverified"),
    ("todo", "status", "BLOQUEADO", "noteri42_todo_not_authorized"),
    ("todo", "canonical", False, "noteri42_todo_not_authorized"),
    ("approval", "approved", False, "noteri42_owner_approval_unverified"),
    ("cost", "incremental_brl", None, "noteri42_cost_unverified"),
    ("cost", "overage_hard_blocked", False, "noteri42_cost_unverified"),
    ("host", "profile", "ESTUDO", "noteri42_host_not_ready"),
    ("host", "session_launcher", "BLOCKED", "noteri42_host_not_ready"),
    ("host", "gateway_verified", False, "noteri42_host_not_ready"),
    ("host", "exclusive_lease", False, "noteri42_host_not_ready"),
    ("host", "model_digest", None, "noteri42_host_not_ready"),
    ("host", "ready_for_work", False, "noteri42_host_not_ready"),
])
def test_blocked_authorities_never_post(name, field, corruption, reason):
    args, values, _counts = fixtures()
    values[name][field] = corruption
    with pytest.raises(NativeExecutorBlocked, match=reason):
        admit_exact(**args)
    assert args["pool"].posts == []


def test_current_todo54_blocked_prevents_enqueue():
    args, values, _ = fixtures()
    values["todo"]["status"] = "BLOQUEADO"
    with pytest.raises(NativeExecutorBlocked, match="noteri42_todo_not_authorized"):
        admit_exact(**args)
    assert args["pool"].list_calls == 0
    assert args["pool"].posts == []


def test_branch_or_todo_drifts_at_second_read_must_not_post():
    for failed in ("source", "todo", "host", "cost", "approval"):
        args, _values, counts = fixtures()
        target = args[failed + "_reader"]
        def moved(*a, _name=failed, _target=target):
            reply = _target(*a)
            if counts[_name] >= 2:
                if _name == "source":
                    reply["branch_head_sha"] = "f" * 40
                elif _name == "todo":
                    reply["status"] = "BLOQUEADO"
                elif _name == "host":
                    reply["ready_for_work"] = False
                elif _name == "cost":
                    reply["overage_hard_blocked"] = False
                else:
                    reply["approved"] = False
            return reply
        args[failed + "_reader"] = moved
        with pytest.raises(NativeExecutorBlocked):
            admit_exact(**args)
        assert args["pool"].posts == []


def test_no_lane_and_unknown_cost_block_before_post():
    for lanes in ([], [{"repository": REPO, "enabled": False,
                         "max_in_flight": 1}],
                  [{"repository": REPO, "enabled": True,
                    "max_in_flight": 2}]):
        args, _, _ = fixtures(pool=Pool(lanes=lanes))
        with pytest.raises(NativeExecutorBlocked, match="noteri42_lane_not_ready"):
            admit_exact(**args)
        assert not args["pool"].posts


def test_dev_only_and_reader_missing_block_before_any_read():
    args, _, counts = fixtures()
    args["environment"] = "prod"
    with pytest.raises(NativeExecutorBlocked, match="noteri42_dev_only"):
        admit_exact(**args)
    assert counts == dict.fromkeys(counts, 0)
    args["environment"] = "dev"
    args["host_reader"] = None
    with pytest.raises(NativeExecutorBlocked, match="noteri42_authority_reader_missing"):
        admit_exact(**args)
    assert counts == dict.fromkeys(counts, 0)


def test_replay_reports_existing_task_and_never_calls_model():
    args, _, _ = fixtures()
    class ExistingPool(Pool):
        def enqueue_fenced_noteri42(self, payload):
            reply = super().enqueue_fenced_noteri42(payload)
            reply["created"] = False
            reply["task"]["state"] = "validating"
            return reply
    args["pool"] = ExistingPool()
    a = admit_exact(**args)
    assert a["created"] is False
    assert a["model_executed"] is False
    assert a["ready_for_pr"] is False


def test_untrusted_task_readback_blocks():
    args, _, _ = fixtures()
    class BadPool(Pool):
        def enqueue_fenced_noteri42(self, payload):
            reply = super().enqueue_fenced_noteri42(payload)
            reply["task"]["repository"] = "bad/other"
            return reply
    args["pool"] = BadPool()
    with pytest.raises(NativeExecutorBlocked, match="noteri42_enqueue_readback_mismatch"):
        admit_exact(**args)
