"""The successful replacements below are SYNTHETIC unit controls, never production fallback."""
from __future__ import annotations
import ast
import json
from pathlib import Path
import pytest
from app import structured_code_worker as worker
from app.structured_revision import apply_local_edits
from test_structured_observations import candidate, message_builder
from test_structured_code_worker import task, BEFORE

def proposal():
    source = candidate()
    contract = task()
    return source, {**contract, "feedback": worker.semantic_feedback(
        contract, source, worker.validate(source, contract))}

def edit(old, new):
    return json.dumps({"edits": [{"old":old, "new":new}]})

def test_model_selected_local_edit_preserves_other_source_and_cases():
    source, spec = proposal()
    old = "return select_value.get('name').strip()"
    new = "return select_value.get('name').strip() if isinstance(select_value.get('name'), str) else ''"
    assembled, edits = apply_local_edits(edit(old,new), source, spec)
    after = worker.render(BEFORE, task(), assembled)
    assert worker.validate(after,task())["passed"] == 39
    assert edits == [{"old":old,"new":new}]
    assert task()["cases"] == spec["cases"]

@pytest.mark.parametrize("old,new,reason",[
    ("missing exact string","return ''","revision_edit_not_unique"),
    ("return ''","return name","revision_edit_not_unique"),
    ("return select_value.get('name').strip()","return select_value.get('name').strip()","candidate_repeated"),
    ("def _property(properties, name):","def _property(properties, name): # unchanged","revision_signature_edit"),
    ("return select_value.get('name').strip()","return open('PRIVATE_VALUE')","candidate_call"),
    ("return select_value.get('name').strip()","return properties[name]","candidate_grammar"),
    ("return select_value.get('name').strip()","not python at all!!!","candidate_syntax"),
])
def test_invalid_or_unsafe_edits_are_blocked(old,new,reason):
    source,spec=proposal()
    with pytest.raises(worker.StructuredBlocked,match="^"+reason+"$"):
        apply_local_edits(edit(old,new),source,spec)

def test_overlapping_edits_rejected():
    source,spec=proposal()
    reply=json.dumps({"edits":[
      {"old":"return select_value.get('name').strip()","new":"return ''"},
      {"old":"select_value.get('name').strip()","new":"name"},
    ]})
    with pytest.raises(worker.StructuredBlocked,match="revision_edits_overlap"):
        apply_local_edits(reply,source,spec)

@pytest.mark.parametrize("value",[{}, [], {"edits":[]},{"edits":[{"old":"x"}]},
    {"edits":[{"old":True,"new":""}]},{"edits":[{"old":"x","new":""}]*7},
    {"function":"not accepted"}])
def test_exact_revision_schema(value):
    source,spec=proposal()
    with pytest.raises(worker.StructuredBlocked):
        apply_local_edits(json.dumps(value),source,spec)

def test_feedback_not_applied_to_different_candidate():
    source,spec=proposal()
    with pytest.raises(worker.StructuredBlocked,match="feedback_candidate_mismatch"):
        apply_local_edits(edit("x","y"),source.replace("return ''","return 'different'"),spec)

def test_revision_asks_for_bounded_edits_not_whole_function():
    source,spec=proposal()
    messages=message_builder()(spec,source)
    assert "Return ONLY JSON with key edits" in messages[0]["content"]
    assert "at most six" in messages[-1]["content"]
    assert spec["instruction"] in messages[1]["content"]
    assert spec["cases"] == task()["cases"]
    # Passing examples are not duplicated in the revision context.
    assert "Trusted input/output examples:" not in messages[1]["content"]
    assert "VALIDATOR_OBSERVATIONS_V1" in messages[-1]["content"]

def test_provider_limits_revision_output_and_preserves_initial_contract():
    text=(Path(__file__).resolve().parents[1]/"app/structured_ollama.py").read_text()
    assert '"format": REVISION_SCHEMA if is_revision else' in text
    assert '"num_predict": 768 if is_revision else 1400' in text
    assert "assembled, edits = apply_local_edits(reply, before, task)" in text
    assert "self.last_revision = edits" in text
    ast.parse(text)

def test_six_localized_edits_fix_all_original_cases_synthetic_control():
    source,spec=proposal()
    edits=[{'old': "return select_value.get('name').strip()", 'new': "return select_value.get('name').strip() if isinstance(select_value.get('name'), str) else ''"}, {'old': "text_content = fragment.get('text', {}).get('content')", 'new': "text_value = fragment.get('text')\n                        text_content = text_value.get('content') if isinstance(text_value, dict) else None"}, {'old': 'for fragment in rich_text_value:\n                    if isinstance(fragment, dict):', 'new': "for fragment in rich_text_value:\n                    if not isinstance(fragment, dict):\n                        return ''\n                    if isinstance(fragment, dict):"}, {'old': 'result.append(plain_text.strip())', 'new': 'result.append(plain_text)'}, {'old': 'result.append(text_content.strip())', 'new': 'result.append(text_content)'}, {'old': "return ''.join(result)", 'new': "return ''.join(result).strip()"}]
    assembled, observed_edits=apply_local_edits(json.dumps({"edits":edits}),source,spec)
    after=worker.render(source,task(),assembled)
    assert worker.validate(after,task())=={"passed":42,"total":42,"errors":0,"failures":[]}
    assert observed_edits == edits
    # Source-bound edits cannot be replayed against another function version.
    with pytest.raises(worker.StructuredBlocked,match="feedback_candidate_mismatch"):
        apply_local_edits(json.dumps({"edits":edits}),after,spec)
