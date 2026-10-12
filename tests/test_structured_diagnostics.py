"""Diagnostics tests use synthetic proposals; they are not real model inference."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.structured_diagnostics import RevisionTrace, DiagnosticWriteError

SOURCE = "def f(x):\n    return x\n"
ALLOWED = {type(n) for n in ast.walk(ast.parse(SOURCE))}


def test_trace_records_before_guard_and_persists_rejection(tmp_path):
    events = []
    output = tmp_path / "diagnostic.json"
    def observer(record):
        events.append(record)
        output.write_text(json.dumps(record), encoding="utf-8")
    trace = RevisionTrace('{"edits":"PRIVATE_VALUE"}', observer)
    assert json.loads(output.read_text())["stage"] == "received"
    trace.before_guard("def f(x):\n    return x['PRIVATE_VALUE']\n", ALLOWED)
    assert json.loads(output.read_text())["stage"] == "guard"
    trace.rejected("candidate_grammar")
    saved = json.loads(output.read_text())
    assert saved["stage"] == "rejected" and saved["failed_stage"] == "guard"
    assert saved["reason_code"] == "candidate_grammar"
    assert saved["rejected_ast_types"] == ["Constant", "Subscript"]
    assert saved["rejected_ast_nodes"][0]["line"] == 2
    assert "PRIVATE_VALUE" not in output.read_text()
    assert [e["stage"] for e in events] == ["received", "guard", "rejected"]


def test_trace_positive_replay_and_no_source_capture():
    outputs = []
    for _ in range(2):
        events = []
        trace = RevisionTrace("PRIVATE_VALUE", events.append)
        trace.before_guard(SOURCE, ALLOWED)
        trace.accepted()
        outputs.append(events)
    assert outputs[0] == outputs[1]
    assert outputs[0][-1]["stage"] == "accepted"
    assert outputs[0][-1]["rejected_ast_nodes"] == []
    assert "PRIVATE_VALUE" not in json.dumps(outputs)
    assert SOURCE not in json.dumps(outputs)


@pytest.mark.parametrize("reply", [None, {}, [], 1, "x" * 16385])
def test_trace_invalid_or_oversized_response_is_not_hashed(reply):
    trace = RevisionTrace(reply)
    assert "response_sha256" not in trace.record
    trace.rejected("revision_size")
    assert trace.record["reason_code"] == "revision_size"


@pytest.mark.parametrize("source", ["def SECRET(", "\0PRIVATE_VALUE"])
def test_trace_syntax_failure_never_copies_exception_text(source):
    trace = RevisionTrace("")
    trace.before_guard(source, ALLOWED)
    trace.rejected("candidate_syntax")
    assert trace.record["ast_parse"] == "invalid"
    assert "SECRET" not in json.dumps(trace.record)
    assert "PRIVATE_VALUE" not in json.dumps(trace.record)


def test_trace_oversized_assembly_does_not_parse(monkeypatch):
    trace = RevisionTrace("")
    monkeypatch.setattr(ast, "parse", lambda *_: pytest.fail("must not parse oversized source"))
    trace.before_guard("x" * 65537, ALLOWED)
    assert trace.record["ast_parse"] == "size_blocked"
    assert "assembly_sha256" not in trace.record


def test_trace_caps_node_details():
    trace = RevisionTrace("")
    trace.before_guard("def f(x):\n" + "    x[0]\n" * 500, set())
    assert len(trace.record["rejected_ast_nodes"]) <= 16
    assert len(json.dumps(trace.record)) < 4096


def test_trace_observer_cannot_mutate_internal_state():
    trace = RevisionTrace("", lambda record: record.update(stage="FORGED"))
    trace.before_guard(SOURCE, ALLOWED)
    assert trace.record["stage"] == "guard"


@pytest.mark.parametrize("stage", ["received", "guard", "rejected"])
def test_trace_observer_failure_is_fatal_and_sanitized(stage):
    def bad(_):
        raise OSError("PRIVATE_VALUE")
    if stage == "received":
        action = lambda: RevisionTrace("", bad)
    else:
        trace = RevisionTrace("")
        trace.observer = bad
        action = lambda: (trace.before_guard(SOURCE, ALLOWED) if stage == "guard"
                          else trace.rejected("candidate_grammar"))
    with pytest.raises(DiagnosticWriteError, match="^revision_diagnostic_write_failed$") as error:
        action()
    assert "PRIVATE_VALUE" not in str(error.value)


def test_trace_unknown_reason_is_redacted():
    trace = RevisionTrace("")
    trace.rejected("PRIVATE_VALUE")
    assert trace.record["reason_code"] == "structured_rejection"


def test_trace_e2e_wires_observer_before_inference_and_copies_in_finally():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "scripts/structured_worker_e2e.py").read_text())
    inner = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "inner")
    observer = next(n for n in ast.walk(inner)
                    if isinstance(n, ast.FunctionDef) and n.name == "revision_observer")
    assert any(isinstance(n, ast.Constant) and n.value == "revision-diagnostic.json"
               for n in ast.walk(observer))
    outer = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "outer")
    final = next(n.finalbody for n in ast.walk(outer)
                 if isinstance(n, ast.Try) and n.finalbody)
    assert any(isinstance(n, ast.Constant) and n.value == "revision-diagnostic.json"
               for stmt in final for n in ast.walk(stmt))


def revision_contract():
    from app import structured_code_worker as worker
    before = "def f(x):\n    return None\n"
    task = {"task_id": "diagnostic-test", "path": "app/value.py", "function": "f",
            "parameters": ["x"], "instruction": "Return x without indexing.",
            "base_sha": "b" * 40, "before_sha256": worker.sha256(before.encode()),
            "cases": [{"args": [1], "expected": 1}, {"args": [2], "expected": 2}]}
    report = worker.validate(before, task)
    proposal = {**task, "feedback": worker.semantic_feedback(task, before, report)}
    return before, proposal


def test_integration_real_guard_preserves_diagnostics_on_rejection(tmp_path):
    from app.structured_revision import apply_local_edits
    from app.structured_code_worker import StructuredBlocked
    before, proposal = revision_contract()
    output = tmp_path / "revision-diagnostic.json"
    events = []
    def observer(record):
        events.append(record)
        output.write_text(json.dumps(record), encoding="utf-8")
    reply = json.dumps({"edits": [{"old": "return None", "new": "return x[0]"}]})
    with pytest.raises(StructuredBlocked, match="^candidate_grammar$"):
        apply_local_edits(reply, before, proposal, observer=observer)
    saved = json.loads(output.read_text())
    assert saved["rejected_ast_types"] == ["Subscript"]
    assert saved["reason_code"] == "candidate_grammar"
    assert [e["stage"] for e in events] == ["received", "guard", "rejected"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["revision-diagnostic.json"]


def test_integration_provider_diagnoses_before_failed_return(tmp_path, monkeypatch):
    from app.structured_ollama import StructuredOllama, MODEL
    from app.structured_code_worker import StructuredBlocked
    before, proposal = revision_contract()
    provider = StructuredOllama()
    monkeypatch.setattr(provider, "no_cloud", lambda: None)
    digest = "a" * 64
    reply = json.dumps({"edits": [{"anchor": "L2", "new": "return x[0]"}]})
    def request(client, method, path, **kwargs):
        if path == "/api/tags":
            return {"models": [{"name": MODEL, "digest": digest}]}
        if path == "/api/show":
            return {"details": {"format": "gguf"}}
        if path == "/api/ps":
            return {"models": [{"name": MODEL, "digest": digest, "size": 1}]}
        assert path == "/api/chat"
        return {"done": True, "model": MODEL, "eval_count": 1,
                "message": {"content": reply}}
    monkeypatch.setattr(provider, "request", request)
    output = tmp_path / "revision-diagnostic.json"
    provider.revision_observer = lambda record: output.write_text(json.dumps(record))
    with pytest.raises(StructuredBlocked, match="^candidate_grammar$"):
        provider(proposal, before)
    saved = json.loads(output.read_text())
    assert saved["stage"] == "rejected"
    assert saved["response_sha256"] == provider.last_model_reply_sha256
    assert saved["rejected_ast_types"] == ["Subscript"]


def test_integration_accepted_revision_keeps_validation_and_replay():
    from app.structured_revision import apply_local_edits
    from app.structured_code_worker import validate, proposal_spec
    before, proposal = revision_contract()
    reply = json.dumps({"edits": [{"old": "return None", "new": "return x"}]})
    outputs = []
    for _ in range(2):
        events = []
        assembled, edits = apply_local_edits(reply, before, proposal, observer=events.append)
        actual = validate(json.loads(assembled)["function"], proposal_spec(proposal))
        assert actual["passed"] == actual["total"] == 2 and actual["errors"] == 0
        outputs.append((assembled, edits, events))
    assert outputs[0] == outputs[1]
    assert outputs[0][2][-1]["stage"] == "accepted"


@pytest.mark.parametrize(("reason", "kind"), [
    ("candidate_grammar", "unknown_anchor"),
    ("revision_anchor_invalid", "PRIVATE_SECRET"),
    ("revision_anchor_invalid", 7),
])
def test_diagnostic_rejects_untrusted_anchor_kind(reason, kind):
    events = []
    trace = RevisionTrace("PRIVATE_SECRET", events.append)
    with pytest.raises(DiagnosticWriteError, match="^revision_diagnostic_kind_invalid$"):
        trace.rejected(reason, anchor_failure_kind=kind)
    assert [event["stage"] for event in events] == ["received"]
    assert "PRIVATE_SECRET" not in json.dumps(events)


def test_legacy_rejections_have_no_added_anchor_kind():
    events = []
    trace = RevisionTrace("PRIVATE_SECRET", events.append)
    trace.rejected("candidate_syntax")
    assert events[-1]["reason_code"] == "candidate_syntax"
    assert "anchor_failure_kind" not in events[-1]
    assert "PRIVATE_SECRET" not in json.dumps(events)
