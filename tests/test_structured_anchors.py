"""Deterministic anchor controls; no real model response is substituted here."""
from __future__ import annotations

import copy
import json
import re

import pytest

from app.structured_anchors import AnchorRejected, assemble_anchors, anchored_schema, line_catalog

SOURCE = "def f(x):\n    if x is None:\n        return ''\n    if x:\n        return ''\n    return x\n"


def reply(anchor, new):
    return json.dumps({"edits": [{"anchor": anchor, "new": new}]})


def test_duplicate_source_lines_have_distinct_immutable_anchors():
    assert SOURCE.count("        return ''\n") == 2
    result, _ = assemble_anchors(reply("L5", "return 'second'"), SOURCE)
    assert result == SOURCE.replace("    if x:\n        return ''",
                                     "    if x:\n        return 'second'")
    assert SOURCE.splitlines()[2] == "        return ''"


def test_anchor_schema_excludes_signature_and_free_paths():
    schema = anchored_schema(SOURCE)
    item = schema["properties"]["edits"]["items"]
    assert item["properties"]["anchor"]["enum"] == ["L2", "L3", "L4", "L5", "L6"]
    assert "L1" not in line_catalog(SOURCE)
    assert item["required"] == ["anchor", "new"]
    assert not re.fullmatch(item["properties"]["new"]["pattern"], "return x[0]")
    assert re.fullmatch(item["properties"]["new"]["pattern"], "return x.get('name')")
    assert item["additionalProperties"] is False
    assert schema["properties"]["edits"]["maxItems"] == 6


def test_multiple_edits_bind_to_original_lines_and_replay_is_pure():
    raw = json.dumps({"edits": [
        {"anchor": "L2", "new": "if not x:\n    return None\nif x is None:"},
        {"anchor": "L6", "new": "return 'end'"},
    ]})
    before = SOURCE
    first = assemble_anchors(raw, SOURCE)
    second = assemble_anchors(raw, SOURCE)
    assert first == second
    assert first[0].endswith("    return 'end'\n")
    assert first[0].startswith("def f(x):\n    if not x:\n        return None\n")
    assert SOURCE == before


@pytest.mark.parametrize("anchor", ["L1", "L0", "L7", "L-1", "../file", "", 2, True, None])
def test_unknown_anchor_fails_closed(anchor):
    with pytest.raises(AnchorRejected):
        assemble_anchors(reply(anchor, "return None"), SOURCE)


@pytest.mark.parametrize("new", [" return None", "\nreturn None", "\treturn None", "return\rNone", "return\0None"])
def test_relative_indentation_is_explicit(new):
    with pytest.raises(AnchorRejected, match="revision_indentation"):
        assemble_anchors(reply("L6", new), SOURCE)


def test_same_anchor_cannot_be_applied_twice():
    raw = json.dumps({"edits": [{"anchor": "L6", "new": "return None"},
                                {"anchor": "L6", "new": "return 0"}]})
    with pytest.raises(AnchorRejected, match="revision_anchor_duplicate"):
        assemble_anchors(raw, SOURCE)


@pytest.mark.parametrize("raw", [
    "not json", "[]", "{}", '{"edits":[]}', '{"edits":[{"old":"return x","new":"return 0"}]}',
    json.dumps({"edits":[{"anchor":"L6","new":"return 0"}]*7}),
    json.dumps({"edits":[{"anchor":"L6","new":None}]}),
])
def test_unrecognized_or_oversized_contract_rejected(raw):
    with pytest.raises(AnchorRejected):
        assemble_anchors(raw, SOURCE)


def test_noop_rejected_and_removal_is_bounded():
    with pytest.raises(AnchorRejected, match="candidate_repeated"):
        assemble_anchors(reply("L6", "return x"), SOURCE)
    result, _ = assemble_anchors(reply("L6", ""), SOURCE)
    assert result == "".join(SOURCE.splitlines(keepends=True)[:-1])


def test_input_objects_are_not_modified():
    record={"edits":[{"anchor":"L6","new":"return 0"}]}
    saved=copy.deepcopy(record)
    assemble_anchors(json.dumps(record), SOURCE)
    assert record==saved


def test_integration_real_guard_blocks_anchored_unsafe_code_and_preserves_diagnosis():
    from app.structured_anchors import apply_anchored_edits
    from app.structured_code_worker import StructuredBlocked
    from test_structured_diagnostics import revision_contract
    before, proposal = revision_contract()
    seen=[]
    with pytest.raises(StructuredBlocked, match="candidate_grammar"):
        apply_anchored_edits(reply("L2","return x[0]"), before, proposal, observer=seen.append)
    assert seen[-1]["stage"]=="rejected"
    assert seen[-1]["rejected_ast_types"]==["Subscript"]


def test_integration_real_positive_and_source_binding():
    from app.structured_anchors import apply_anchored_edits
    from app.structured_code_worker import StructuredBlocked, validate, proposal_spec
    from test_structured_diagnostics import revision_contract
    before, proposal=revision_contract()
    raw=reply("L2","return x")
    assembled,_=apply_anchored_edits(raw,before,proposal)
    assert validate(json.loads(assembled)["function"],proposal_spec(proposal))["passed"]==2
    with pytest.raises(StructuredBlocked, match="feedback_candidate_mismatch"):
        apply_anchored_edits(raw,before.replace("None","False"),proposal)


def test_integration_original_42_case_control_uses_model_shaped_anchors():
    # Synthetic known-good edits test assembly; not a production/model fallback.
    from app.structured_anchors import apply_anchored_edits
    from app import structured_code_worker as worker
    from test_structured_local_revision import proposal
    source,spec=proposal()
    replacements={
        "return select_value.get('name').strip()":
            "return select_value.get('name').strip() if isinstance(select_value.get('name'), str) else ''",
        "text_content = fragment.get('text', {}).get('content')":
            "text_value = fragment.get('text')\ntext_content = text_value.get('content') if isinstance(text_value, dict) else None",
        "if isinstance(fragment, dict):":
            "if not isinstance(fragment, dict):\n    return ''\nif isinstance(fragment, dict):",
        "result.append(plain_text.strip())":"result.append(plain_text)",
        "result.append(text_content.strip())":"result.append(text_content)",
        "return ''.join(result)":"return ''.join(result).strip()",
    }
    edits=[{"anchor":f"L{i}","new":replacements[line.strip()]}
           for i,line in enumerate(source.splitlines(),1) if line.strip() in replacements]
    assert len(edits)==6
    assembled,_=apply_anchored_edits(json.dumps({"edits":edits}),source,spec)
    checked=worker.validate(json.loads(assembled)["function"],worker.proposal_spec(spec))
    assert checked=={"passed":42,"total":42,"errors":0,"failures":[]}
