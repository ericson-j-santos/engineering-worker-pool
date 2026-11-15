"""Contract tests: not an operational PC/Gateway/Ollama E2E."""
from __future__ import annotations

import pytest

from app.native_one_shot import NativeExecutorBlocked
from app.next_action_bridge import continue_once, decide_next_action

TASK = "cwp-" + "1" * 24
BASE = "a" * 40
PRODUCED = "b" * 40
REPO = "ericson-j-santos/engineering-worker-pool"
BINDING = dict(task_id=TASK, expected_repository=REPO,
               expected_issue=62, expected_request_id="issue-62", expected_base_sha=BASE)


def task(state="queued", generation=0, **changes):
    return {
        "task_id": TASK, "repository": REPO, "issue_number": 62,
        "request_id": "issue-62", "base_sha": BASE,
        "fencing_required": True, "lease_generation": generation,
        "state": state, "leased_by": None,
        "produced_sha": PRODUCED if state in {"validating", "completed"} else None,
        **changes,
    }


def checkpoint(stage, action, effect="confirmed", generation=1):
    return {
        "task_id": TASK, "lease_generation": generation, "sequence": 1,
        "stage": stage, "next_action": action, "effect_state": effect,
        "artifact_sha256": "d" * 64,
        "effect_result_ref": "cas://verified" if effect == "confirmed" else None,
    }


class Api:
    def __init__(self):
        self.task = task()
        self.checkpoint = None
        self.calls = []

    def get_task(self, task_id):
        self.calls.append("task")
        assert task_id == TASK
        return dict(self.task)

    def get_latest_checkpoint(self, task_id):
        self.calls.append("checkpoint")
        assert task_id == TASK
        return None if self.checkpoint is None else dict(self.checkpoint)


def test_two_real_stages_are_selected_and_read_back_without_replay():
    api = Api()
    invoked = []

    def builder():
        invoked.append("builder")
        api.task = task("validating", generation=1)
        api.checkpoint = checkpoint("builder_result", "submit_validation")

    def validator():
        invoked.append("validator")
        api.task = task("completed", generation=2)
        api.checkpoint = checkpoint("validator_result", "complete",
                                    effect="none", generation=2)

    handlers = {"builder": builder, "validator": validator}
    first = continue_once(api, handlers=handlers, **BINDING)
    assert first == {"state": "validator", "task_id": TASK,
                     "executed": True, "executed_role": "builder"}
    second = continue_once(api, handlers=handlers, **BINDING)
    assert second == {"state": "done", "task_id": TASK,
                      "executed": True, "executed_role": "validator"}
    assert continue_once(api, handlers=handlers, **BINDING) == {
        "state": "done", "task_id": TASK, "executed": False,
    }
    assert invoked == ["builder", "validator"]
    assert api.calls.count("task") == api.calls.count("checkpoint") == 7


@pytest.mark.parametrize("field,value", [
    ("repository", "untrusted/repo"),
    ("issue_number", 63),
    ("request_id", "foreign-task"),
    ("base_sha", "c" * 40),
    ("fencing_required", False),
    ("lease_generation", -1),
    ("task_id", "cwp-" + "2" * 24),
])
def test_bad_task_binding_is_rejected_before_execution(field, value):
    api = Api()
    api.task[field] = value
    with pytest.raises(NativeExecutorBlocked):
        continue_once(api, handlers={"builder": lambda: pytest.fail("called"),
                                    "validator": lambda: pytest.fail("called")}, **BINDING)


@pytest.mark.parametrize("field,value", [
    ("expected_base_sha", "INVALID"),
    ("expected_issue", True),
    ("expected_request_id", ""),
    ("expected_repository", "not-a-repository"),
])
def test_bad_trusted_request_is_rejected(field, value):
    args = {**BINDING, field: value}
    with pytest.raises(NativeExecutorBlocked, match="continuation_binding_invalid"):
        decide_next_action(task(), None, **args)


@pytest.mark.parametrize("effect", ["pending", "unknown"])
def test_unreconciled_external_effect_blocks_without_retry(effect):
    api = Api()
    api.task = task("validating", generation=1)
    api.checkpoint = checkpoint("artifact_publish", "publish_artifact", effect)
    with pytest.raises(NativeExecutorBlocked, match="continuation_effect_unresolved"):
        continue_once(api, handlers={"builder": lambda: None,
                                    "validator": lambda: None}, **BINDING)


@pytest.mark.parametrize("state", ["leased", "running", "blocked", "failed"])
def test_active_and_terminal_unsafe_states_do_not_dispatch(state):
    api = Api()
    api.task = task(state, generation=1)
    called = []
    result = continue_once(api, handlers={
        "builder": lambda: called.append("builder"),
        "validator": lambda: called.append("validator"),
    }, **BINDING)
    assert result["executed"] is False
    assert result["state"] == ("wait" if state in {"leased", "running"} else "blocked")
    assert called == []


def test_checkpoint_text_cannot_select_an_arbitrary_command():
    api = Api()
    api.task = task("validating", generation=1)
    api.checkpoint = checkpoint("builder_result", "rm -rf /")
    with pytest.raises(NativeExecutorBlocked, match="continuation_validator_evidence_missing"):
        continue_once(api, handlers={"builder": lambda: None,
                                    "validator": lambda: pytest.fail("called")}, **BINDING)


def test_empty_claimed_result_does_not_count_as_effective_progress():
    api = Api()
    with pytest.raises(NativeExecutorBlocked, match="continuation_effect_not_confirmed"):
        continue_once(api, handlers={"builder": lambda: {"status": 200},
                                    "validator": lambda: None}, **BINDING)


def test_snapshot_changed_just_before_dispatch_blocks():
    class Changing(Api):
        def get_task(self, task_id):
            result = super().get_task(task_id)
            if self.calls.count("task") == 2:
                result["base_sha"] = "f" * 40
            return result

    api = Changing()
    with pytest.raises(NativeExecutorBlocked, match="continuation_snapshot_changed"):
        continue_once(api, handlers={"builder": lambda: pytest.fail("called"),
                                    "validator": lambda: None}, **BINDING)


def test_stale_checkpoint_and_missing_completion_do_not_pass():
    for state, cp in [
        ("validating", None),
        ("completed", None),
        ("validating", checkpoint("builder_result", "submit_validation",
                                  generation=2)),
    ]:
        api = Api()
        api.task = task(state, generation=1)
        api.checkpoint = cp
        with pytest.raises(NativeExecutorBlocked):
            continue_once(api, handlers={"builder": lambda: None,
                                        "validator": lambda: pytest.fail("called")}, **BINDING)


def test_queued_recovery_delegates_to_existing_exact_fenced_claim():
    api = Api()
    api.task = task("queued", generation=3)
    api.checkpoint = checkpoint("builder_result", "submit_validation",
                                generation=2)
    assert decide_next_action(api.get_task(TASK), api.get_latest_checkpoint(TASK),
                              **BINDING) == "builder"
    # This selector never advances the lease; the installed native handler
    # must claim exact-fenced and reconcile CAS independently.
    assert api.task["lease_generation"] == 3


def test_bad_handler_registry_is_rejected_before_any_read():
    api = Api()
    with pytest.raises(NativeExecutorBlocked, match="continuation_handlers_invalid"):
        continue_once(api, handlers={"shell": lambda: None}, **BINDING)
    assert api.calls == []


def test_issue62_self_declared_flags_must_not_authorize_execution(monkeypatch, capsys):
    """A repository variable is not a Session Launcher or Gateway receipt."""
    import json
    from scripts import issue62_continuation_gate as gate

    environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_SHA": BASE,
        "EWP_CHECKED_OUT_SHA": BASE,
        "EWP_TASK_ID": TASK,
        "EWP_EXPECTED_REPOSITORY": REPO,
        "EWP_EXPECTED_ISSUE": "62",
        "EWP_EXPECTED_REQUEST_ID": "issue-62",
        "EWP_EXPECTED_BASE_SHA": BASE,
        "EWP_EXPECTED_COST_ZERO_SHA": BASE,
        "EWP_COST_ZERO_CERTIFIED_SHA": BASE,
        "EWP_HANDLER_ATTESTED": "1",
        "EWP_GATEWAY_ATTESTED": "1",
        "EWP_HANDLER_ID": "issue-qwen7b-native-v1",
        "EWP_HANDLER_ENTRYPOINT": "scripts/native_live_driver.py",
        "EWP_SESSION_LAUNCH_OK": "1",
        "EWP_STATE_VALIDATED": "true",
        # A secret in the environment may never authorize, or leak to, a URL.
        "EWP_WORKER_POOL_URL": "https://attacker.example.invalid",
        "EWP_WORKER_POOL_TOKEN": "never-expose-this-value",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    class Canonical:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def get_task(self, task_id):
            assert task_id == TASK
            return task()

        def get_latest_checkpoint(self, task_id):
            assert task_id == TASK
            return None

    monkeypatch.setattr(gate, "_connect", lambda: Canonical())
    assert gate.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["result"] == "CONTINUATION_BLOCKED"
    assert result["code"] == "native_session_gateway_and_handler_not_attested"
    assert result["next_action"] == "builder"
    assert result["executed"] is False
    assert "never-expose-this-value" not in json.dumps(result)


def test_issue62_nonlocal_api_and_credential_env_do_not_select_transport(monkeypatch):
    """Even an external URL supplied in GitHub variables cannot route a token."""
    from scripts import issue62_continuation_gate as gate

    monkeypatch.setenv("EWP_WORKER_POOL_URL", "https://attacker.example.invalid")
    monkeypatch.setenv("EWP_WORKER_POOL_TOKEN", "never-expose-this-value")
    monkeypatch.delenv("EWP_WORKER_POOL_PORT", raising=False)
    monkeypatch.delenv("EWP_WORKER_POOL_TOKEN_FILE", raising=False)
    with pytest.raises(NativeExecutorBlocked, match="worker_pool_port_invalid"):
        gate._connect()
    monkeypatch.setenv("EWP_WORKER_POOL_PORT", "8787")
    with pytest.raises(NativeExecutorBlocked, match="worker_pool_private_token_file_missing"):
        gate._connect()


@pytest.mark.parametrize("changed,value,error", [
    ("GITHUB_REPOSITORY", "untrusted/repository", "continuation_github_origin_unverified"),
    ("GITHUB_ACTIONS", "false", "continuation_github_origin_unverified"),
    ("GITHUB_EVENT_NAME", "push", "continuation_github_origin_unverified"),
    ("GITHUB_SHA", "f" * 40, "continuation_code_sha_unverified"),
    ("EWP_EXPECTED_REPOSITORY", "untrusted/repository", "repository_not_allowlisted"),
    ("EWP_COST_ZERO_CERTIFIED_SHA", "f" * 40, "cost_zero_binding_missing_or_mismatched"),
])
def test_issue62_event_source_binding_fails_before_api(monkeypatch, capsys, changed, value, error):
    """Source/sha/cost binding must block before a credential file is touched."""
    import json
    from scripts import issue62_continuation_gate as gate

    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_EVENT_NAME": "repository_dispatch",
        "GITHUB_SHA": BASE,
        "EWP_CHECKED_OUT_SHA": BASE,
        "EWP_TASK_ID": TASK,
        "EWP_EXPECTED_REPOSITORY": REPO,
        "EWP_EXPECTED_ISSUE": "62",
        "EWP_EXPECTED_REQUEST_ID": "issue-62",
        "EWP_EXPECTED_BASE_SHA": BASE,
        "EWP_EXPECTED_COST_ZERO_SHA": BASE,
        "EWP_COST_ZERO_CERTIFIED_SHA": BASE,
    }
    env[changed] = value
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(gate, "_connect", lambda: pytest.fail("API contacted"))
    assert gate.main() == 2
    assert json.loads(capsys.readouterr().out)["code"] == error


def test_issue62_terminal_readback_is_noop_not_dispatch(monkeypatch, capsys):
    import json
    from scripts import issue62_continuation_gate as gate

    env = {
        "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": REPO,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_SHA": BASE, "EWP_CHECKED_OUT_SHA": BASE,
        "EWP_TASK_ID": TASK, "EWP_EXPECTED_REPOSITORY": REPO,
        "EWP_EXPECTED_ISSUE": "62", "EWP_EXPECTED_REQUEST_ID": "issue-62",
        "EWP_EXPECTED_BASE_SHA": BASE, "EWP_EXPECTED_COST_ZERO_SHA": BASE,
        "EWP_COST_ZERO_CERTIFIED_SHA": BASE,
    }
    for key, val in env.items():
        monkeypatch.setenv(key, val)

    class Completed:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def get_task(self, task_id):
            return task("completed", generation=2)

        def get_latest_checkpoint(self, task_id):
            return checkpoint("validator_result", "complete", effect="none",
                              generation=2)

    monkeypatch.setattr(gate, "_connect", Completed)
    assert gate.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"result": "CONTINUATION_NO_EXECUTION",
                      "state": "done", "task_id": TASK, "executed": False}
