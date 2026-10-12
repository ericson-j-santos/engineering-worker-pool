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
    from app.structured_anchors import line_catalog
    scope = {"json": json, "proposal_spec": worker.proposal_spec, "need": worker.need,
             "candidate_fingerprint": worker.candidate_fingerprint, "extracted": worker.extracted,
             "line_catalog": line_catalog}
    exec(compile(module, "<production-message-builder>", "exec"), scope)
    return scope["proposal_messages"]


def test_recorded_five_failures_have_actual_values_and_exception_categories():
    assert worker.sha256(REPLY.encode()) == RECORDED["response_sha256"]
    report = worker.validate(candidate(), task())
    assert (report["passed"], report["total"], report["errors"]) == (37, 42, 3)
    assert [f["case_index"] for f in report["failures"]] == [7, 20, 21, 36, 41]
    observed = [f["observed"] for f in report["failures"]]
    assert [{k: v for k, v in item.items() if k != "candidate_line"} for item in observed[:3]] == [
        {"kind": "exception", "type": "AttributeError"}] * 3
    numbered = worker.extracted(candidate(), task()).splitlines()
    assert all(type(item.get("candidate_line")) is int
               and 1 <= item["candidate_line"] <= len(numbered)
               for item in observed[:3])
    assert ".get(" in numbered[observed[0]["candidate_line"] - 1]
    assert all(".strip()" in numbered[item["candidate_line"] - 1]
               for item in observed[1:3])
    assert [item["value"] for item in observed[3:]] == ["safe", "açãoválida"]
    feedback = worker.semantic_feedback(task(), candidate(), report)
    assert [f["expected"] for f in feedback["failures"]] == ["", "", "", "", "ação válida"]
    assert feedback["failed_count"] == len(feedback["failures"]) == 5


def test_revision_uses_failing_source_as_data_and_binds_candidate():
    contract = task()
    original = copy.deepcopy(contract)
    feedback = worker.semantic_feedback(contract, candidate(), worker.validate(candidate(), contract))
    proposal = {**contract, "feedback": feedback}
    messages = message_builder()(proposal, candidate())
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages == message_builder()(proposal, candidate())
    assert "REJECTED_SOURCE_IS_NOT_AN_ANSWER_V1" in messages[1]["content"]
    assert worker.extracted(candidate(), contract) not in messages[1]["content"]
    assert "PATCH THE PREVIOUS CODE AT THE EXACT LISTED LINES" in messages[-1]["content"]
    assert "IMMUTABLE_SOURCE_ANCHORS_V1" in messages[1]["content"]
    assert "L1:" not in messages[1]["content"]
    assert "L2:" in messages[1]["content"]
    assert "Return ONLY JSON edits" in messages[-1]["content"]
    assert "full function" in messages[0]["content"]
    actual = json.loads(messages[-1]["content"].split("VALIDATOR_OBSERVATIONS_V1\n", 1)[1])
    assert actual == feedback
    revision_rules = messages[1]["content"]
    for requirement in (
        "dict.get only after verifying dict",
        "str.strip only after verifying str",
        "Reject invalid fragments instead of ignoring them",
        "Preserve valid empty plain_text",
        "not one strip per fragment",
        "all previously passing cases",
    ):
        assert requirement in revision_rules
    assert "candidate_line" in revision_rules
    assert "one-based" in revision_rules
    assert "IMMUTABLE_SOURCE_ANCHORS_V1" in messages[1]["content"]
    assert not any(message["role"] == "assistant" for message in messages)
    assert original == contract
    assert "feedback" not in contract
    with pytest.raises(worker.StructuredBlocked, match="feedback_candidate_mismatch"):
        message_builder()(proposal, FIXED)


def test_rejected_source_focus_uses_real_candidate_failure_lines_only():
    contract = task()
    source = candidate()
    feedback = worker.semantic_feedback(contract, source, worker.validate(source, contract))
    spec = {**contract, "feedback": feedback}
    snapshot = json.dumps(spec, sort_keys=True, ensure_ascii=False)
    messages = message_builder()(spec, source)
    assert [msg["role"] for msg in messages] == ["system", "user"]
    body = messages[1]["content"]
    focused = json.loads(body.split("FOCUSED_EXCEPTION_ANCHORS_V1\n", 1)[1]
                         .split("\nVALIDATOR_OBSERVATIONS_V1\n", 1)[0])
    assert focused == [
        {"anchor": "L10", "failed_case_indices": [20, 21]},
        {"anchor": "L22", "failed_case_indices": [7]},
    ]
    assert "REJECTED_SOURCE_IS_NOT_AN_ANSWER_V1" in body
    assert "unchanged AST" in body
    assert json.loads(body.split("VALIDATOR_OBSERVATIONS_V1\n", 1)[1]) == feedback
    assert json.dumps(spec, sort_keys=True, ensure_ascii=False) == snapshot
    assert message_builder()(spec, source) == messages


def test_focus_does_not_invent_missing_or_spoofed_lines():
    contract = task()
    source = candidate()
    feedback = worker.semantic_feedback(contract, source, worker.validate(source, contract))
    for f in feedback["failures"]:
        f["observed"].pop("candidate_line", None)
    spec = {**contract, "feedback": feedback}
    assert "FOCUSED_EXCEPTION_ANCHORS_V1\n[]\nVALIDATOR_OBSERVATIONS_V1\n" in (
        message_builder()(spec, source)[1]["content"])
    feedback["failures"][0]["observed"]["candidate_line"] = 400
    assert "FOCUSED_EXCEPTION_ANCHORS_V1\n[]\nVALIDATOR_OBSERVATIONS_V1\n" in (
        message_builder()(spec, source)[1]["content"])
    feedback["failures"][0]["observed"]["candidate_line"] = True
    with pytest.raises(worker.StructuredBlocked, match="observation_exception"):
        message_builder()(spec, source)


def test_focused_unchanged_ast_is_still_rejected():
    from app.structured_anchors import apply_anchored_edits
    contract = task()
    source = candidate()
    spec = {**contract, "feedback": worker.semantic_feedback(
        contract, source, worker.validate(source, contract))}
    unchanged_line = worker.extracted(source, contract).splitlines()[9].strip()
    unchanged = json.dumps({"edits": [{"anchor": "L10", "new": unchanged_line}]})
    observed = []
    with pytest.raises(worker.StructuredBlocked, match="^candidate_repeated$"):
        apply_anchored_edits(unchanged, source, spec, observer=observed.append)
    assert observed[-1]["stage"] == "rejected"
    assert observed[-1]["reason_code"] == "candidate_repeated"


def test_focused_synthetic_model_shaped_edit_reaches_42_and_replays(tmp_path):
    # Explicit fixture; production cannot access these replacements as fallback.
    from app.structured_anchors import apply_anchored_edits
    root, state, contract = repository(tmp_path)
    calls = []
    replacements = {
        "return select_value.get('name').strip()":
            "return select_value.get('name').strip() if isinstance(select_value.get('name'), str) else ''",
        "text_content = fragment.get('text', {}).get('content')":
            "text_value = fragment.get('text')\ntext_content = text_value.get('content') if isinstance(text_value, dict) else None",
        "if isinstance(fragment, dict):":
            "if not isinstance(fragment, dict):\n    return ''\nif isinstance(fragment, dict):",
        "result.append(plain_text.strip())": "result.append(plain_text)",
        "result.append(text_content.strip())": "result.append(text_content)",
        "return ''.join(result)": "return ''.join(result).strip()",
    }
    def source_bound_propose(spec, source):
        calls.append("revision" if "feedback" in spec else "initial")
        if "feedback" not in spec:
            return REPLY
        assert spec["feedback"]["failed_count"] == 5
        message = message_builder()(spec, source)[1]["content"]
        focused = json.loads(message.split("FOCUSED_EXCEPTION_ANCHORS_V1\n", 1)[1]
                             .split("\nVALIDATOR_OBSERVATIONS_V1\n", 1)[0])
        assert [item["anchor"] for item in focused] == ["L10", "L22"]
        edits = [{"anchor": f"L{idx}", "new": replacements[line.strip()]}
                 for idx, line in enumerate(source.splitlines(), 1)
                 if line.strip() in replacements]
        assert len(edits) == 6
        result, _ = apply_anchored_edits(json.dumps({"edits": edits}), source, spec)
        return result
    result = worker.repair(root, state, contract, source_bound_propose)
    assert calls == ["initial", "revision"]
    assert result["model_calls"] == 2
    assert result["attempts"][0]["validation"]["passed"] == 37
    assert result["validation"] == {"passed": 42, "total": 42, "errors": 0, "failures": []}
    again = worker.repair(root, state, contract, lambda *_: pytest.fail("must replay"))
    assert again["replayed"] is True
    assert again["model_calls"] == 0
    assert again["produced_sha"] == result["produced_sha"]
    assert worker.git(root, "status", "--porcelain") == ""


def test_focused_new_header_without_body_is_rejected_as_missing_block():
    from app.structured_anchors import line_catalog, apply_anchored_edits
    contract = task()
    source = candidate()
    spec = {**contract, "feedback": worker.semantic_feedback(
        contract, source, worker.validate(source, contract))}
    lines = worker.extracted(source, contract).splitlines()
    anchor = next(key for key, index in line_catalog(worker.extracted(source, contract)).items()
                  if lines[index].strip() == "if isinstance(fragment, dict):")
    # An empty inserted if suite must not become a valid patch or bypass AST validation.
    bad_reply = json.dumps({"edits": [{"anchor": anchor,
        "new": "if not isinstance(fragment, dict):\nif isinstance(fragment, dict):"}]})
    events = []
    with pytest.raises(worker.StructuredBlocked, match="^candidate_syntax$"):
        apply_anchored_edits(bad_reply, source, spec, observer=events.append)
    assert events[-1]["stage"] == "rejected"
    assert events[-1]["reason_code"] == "candidate_syntax"
    assert events[-1]["syntax_kind"] == "missing_block"


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
    assert {f["observed"]["candidate_line"] for f in report["failures"]} == {2}
    assert "guarded-json-function" not in json.dumps(report)


def test_candidate_line_is_source_bound_and_present_in_feedback():
    contract = task()
    source = candidate()
    observed = worker.validate(source, contract)
    feedback = worker.semantic_feedback(contract, source, observed)
    assert feedback["failed_count"] == 5
    assert [f["observed"].get("candidate_line") for f in feedback["failures"][:3]] == [
        f["observed"]["candidate_line"] for f in observed["failures"][:3]]
    assert "guarded-json-function" not in json.dumps(feedback)
    assert "PRIVATE_TOKEN" not in json.dumps(feedback)


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 401, "12", 1.5, [], {}])
def test_candidate_line_invalid_or_spoofed_types_fail_closed(value):
    report = worker.validate(candidate(), task())
    report["failures"][0]["observed"]["candidate_line"] = value
    with pytest.raises(worker.StructuredBlocked, match="^observation_exception$"):
        worker.validate_report(report, task())


def test_candidate_line_never_appears_for_validator_internal_exception():
    source = "def _property(properties, name):\\n    return float('nan')\\n".replace("\\n", "\n")
    report = worker.validate(source, task())
    assert report["errors"] == 42
    assert all(item["observed"] == {"kind": "exception", "type": "InvalidResult"}
               for item in report["failures"])
    assert "candidate_line" not in json.dumps(report)


def test_candidate_line_is_forbidden_on_value_observations():
    with pytest.raises(worker.StructuredBlocked, match="^observation_type$|^observation_value$"):
        worker.validate_observation(
            {"kind": "value", "type": "str", "value": "x", "candidate_line": 2})


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
