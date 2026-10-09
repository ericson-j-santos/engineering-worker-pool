"""Recorded failed model output and synthetic success are separate controls."""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest

from app import structured_code_worker as worker
from test_structured_code_worker import BEFORE, FIXED, repository, task

ROOT = Path(__file__).resolve().parents[1]
RECORDED = json.loads((ROOT / "tests/fixtures/structured_repeated_proposal.json").read_text())
REPLY = RECORDED["reply"]


def candidate():
    return worker.render(BEFORE, task(), REPLY)


def message_builder():
    # Exercise the pure production builder without initializing a provider/network.
    source = (ROOT / "app/structured_ollama.py").read_text()
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == "proposal_messages")
    module = ast.Module(body=[node], type_ignores=[])
    scope = {"json": json, "proposal_spec": worker.proposal_spec, "need": worker.need,
             "candidate_fingerprint": worker.candidate_fingerprint, "extracted": worker.extracted}
    exec(compile(module, "<production-message-builder>", "exec"), scope)
    return scope["proposal_messages"]


def test_recorded_five_failures_have_actual_values_and_exception_categories():
    assert worker.sha256(REPLY.encode()) == RECORDED["response_sha256"]
    report = worker.validate(candidate(), task())
    assert (report["passed"], report["total"], report["errors"]) == (37, 42, 3)
    assert [f["case_index"] for f in report["failures"]] == [7, 20, 21, 36, 41]
    observed = [f["observed"] for f in report["failures"]]
    assert observed[:3] == [{"kind": "exception", "type": "AttributeError"}] * 3
    assert [item["value"] for item in observed[3:]] == ["safe", "açãoválida"]
    feedback = worker.semantic_feedback(task(), candidate(), report)
    assert [f["expected"] for f in feedback["failures"]] == ["", "", "", "", "ação válida"]
    assert feedback["failed_count"] == len(feedback["failures"]) == 5


def test_revision_is_actual_assistant_user_turn_and_binds_candidate():
    contract = task()
    original = copy.deepcopy(contract)
    feedback = worker.semantic_feedback(contract, candidate(), worker.validate(candidate(), contract))
    proposal = {**contract, "feedback": feedback}
    messages = message_builder()(proposal, candidate())
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert "LOCALIZED EDITS" in messages[-1]["content"]
    actual = json.loads(messages[-1]["content"].split("VALIDATOR_OBSERVATIONS_V1\n", 1)[1])
    assert actual == feedback
    assert json.loads(messages[-2]["content"])["function"] == worker.extracted(candidate(), contract)
    assert original == contract
    assert "feedback" not in contract
    with pytest.raises(worker.StructuredBlocked, match="feedback_candidate_mismatch"):
        message_builder()(proposal, FIXED)


def test_initial_prompt_has_no_previous_candidate_or_fake_feedback():
    messages = message_builder()(task(), BEFORE)
    assert len(messages) == 2
    assert "VALIDATOR_OBSERVATIONS" not in json.dumps(messages)
    assert "return str(" not in json.dumps(messages)


@pytest.mark.parametrize("variant", ["exact", "whitespace", "comments", "json_spacing"])
def test_repeated_ast_is_blocked_before_second_validation_or_write(tmp_path, monkeypatch, variant):
    root, state, contract = repository(tmp_path)
    calls, validations = [], []
    real = worker.validate
    def validate(source, spec):
        validations.append(1)
        return real(source, spec)
    monkeypatch.setattr(worker, "validate", validate)
    code = json.loads(REPLY)["function"]
    variants = {
        "exact": code, "whitespace": code.replace("    ", "        "),
        "comments": "# comment only\n" + code + "\n# still identical\n",
        "json_spacing": code,
    }
    def propose(spec, source):
        calls.append(1)
        if len(calls) == 1:
            return REPLY
        assert worker.proposal_spec(spec) == contract
        return json.dumps({"function": variants[variant]}, indent=4)
    with pytest.raises(worker.StructuredBlocked, match="^candidate_repeated$"):
        worker.repair(root, state, contract, propose)
    assert len(calls) == 2  # A second inference is necessary to discover a repeated answer.
    assert len(validations) == 2  # Baseline once, first candidate once; no per-case reruns.
    assert worker.git(root, "rev-parse", "HEAD") == contract["base_sha"]
    assert worker.git(root, "status", "--porcelain") == ""
    assert not (state / "change.patch").exists()


def test_different_second_failed_candidate_is_not_misclassified(tmp_path):
    root, state, contract = repository(tmp_path)
    replies = iter([REPLY, json.dumps({"function": "def _property(properties, name):\n    return ''\n"})])
    with pytest.raises(worker.StructuredBlocked, match="^candidate_tests_failed$"):
        worker.repair(root, state, contract, lambda *_: next(replies))
    assert worker.git(root, "status", "--porcelain") == ""


def test_recorded_failure_then_synthetic_success_preserves_task_and_patch(tmp_path):
    root, state, contract = repository(tmp_path)
    initial = copy.deepcopy(contract)
    calls = []
    def propose(spec, source):
        calls.append(1)
        if len(calls) == 1:
            return REPLY
        assert spec["cases"] == initial["cases"]
        assert spec["instruction"] == initial["instruction"]
        assert [f["case_index"] for f in spec["feedback"]["failures"]] == [7, 20, 21, 36, 41]
        # This is a SYNTHETIC success control. Production has no replacement fallback.
        return json.dumps({"function": FIXED})
    result = worker.repair(root, state, contract, propose)
    assert contract == initial
    assert result["model_calls"] == 2
    assert result["validation"] == {"passed": 42, "total": 42, "errors": 0, "failures": []}
    assert (root / contract["path"]).read_text().endswith("\nOTHER = 17\n")
    assert worker.git(root, "rev-list", "--count", contract["base_sha"] + "..HEAD") == "1"
    replay = worker.repair(root, state, contract, lambda *_: pytest.fail("unexpected inference"))
    assert replay["replayed"] and replay["model_calls"] == 0
    independent = tmp_path / "readback"
    worker.git(tmp_path, "clone", "--no-hardlinks", str(root), str(independent))
    worker.git(independent, "checkout", "--detach", result["produced_sha"])
    assert worker.validate((independent / contract["path"]).read_text(), contract) == result["validation"]


def test_exception_messages_never_leave_validator():
    source = "def _property(properties, name):\n    return int('PRIVATE_VALUE')\n"
    report = worker.validate(source, task())
    assert report["errors"] == 42
    assert "PRIVATE_VALUE" not in json.dumps(report)
    assert {f["observed"]["type"] for f in report["failures"]} == {"ValueError"}


def test_large_observed_values_are_bounded_not_silently_changed():
    value = "x" * 2000
    source = "def _property(properties, name):\n    return " + repr(value) + "\n"
    report = worker.validate(source, task())
    observed = report["failures"][0]["observed"]
    assert observed["truncated"] is True and "value" not in observed
    assert observed["bytes"] == len(json.dumps(value).encode())
    assert observed["sha256"] == worker.sha256(json.dumps(value).encode())


@pytest.mark.parametrize("change", [
    {"passed": True}, {"passed": 43}, {"errors": -1}, {"errors": 0},
    {"total": 41}, {"failures": []}, {"failures": "PRIVATE_VALUE"},
])
def test_bad_child_summary_fails_closed(change):
    report = worker.validate(candidate(), task())
    report.update(change)
    with pytest.raises(worker.StructuredBlocked):
        worker.validate_report(report, task())


@pytest.mark.parametrize("change", [
    {"case_index": True}, {"case_index": 999},
    {"observed": {"kind": "exception", "type": ["PRIVATE_VALUE"]}},
    {"observed": {"kind": "exception", "type": "PrivateException"}},
    {"observed": {"kind": "value", "type": "str", "value": ""}},
    {"observed": {"kind": "value", "type": "int", "value": True}},
])
def test_bad_child_failure_fails_closed(change):
    report = worker.validate(candidate(), task())
    report["failures"][0].update(change)
    with pytest.raises(worker.StructuredBlocked):
        worker.validate_report(report, task())


def test_feedback_rejects_changed_expectation_and_keeps_public_task_schema():
    contract = task()
    feedback = worker.semantic_feedback(contract, candidate(), worker.validate(candidate(), contract))
    proposal = {**contract, "feedback": feedback}
    with pytest.raises(worker.StructuredBlocked, match="task_schema"):
        worker.task_spec(proposal)
    feedback["failures"][0]["expected"] = "FORGED"
    with pytest.raises(worker.StructuredBlocked, match="feedback_case_changed"):
        worker.proposal_spec(proposal)


def test_callback_cannot_mutate_authoritative_acceptance(tmp_path):
    root, state, contract = repository(tmp_path)
    baseline = copy.deepcopy(contract)
    def propose(spec, source):
        spec["cases"][0]["expected"] = "FORGED"
        spec["instruction"] = "FORGED"
        return json.dumps({"function": FIXED})
    result = worker.repair(root, state, contract, propose)
    assert contract == baseline and result["validation"]["passed"] == 42


def test_guard_still_rejects_unsafe_second_candidate(tmp_path):
    root, state, contract = repository(tmp_path)
    calls = []
    def propose(*_):
        calls.append(1)
        if len(calls) == 1:
            return REPLY
        return json.dumps({"function": "def _property(properties, name):\n    return open('PRIVATE_VALUE')\n"})
    with pytest.raises(worker.StructuredBlocked, match="candidate_call"):
        worker.repair(root, state, contract, propose)
    assert len(calls) == 2 and worker.git(root, "status", "--porcelain") == ""
