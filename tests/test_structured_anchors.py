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
    variants = item["oneOf"]
    assert len(variants) == 3
    replacement_variants = [var for var in variants if "new" in var["properties"]]
    branches = {key: var for var in replacement_variants
                for key in var["properties"]["anchor"]["enum"]}
    guard_variant = next(var for var in variants if "guard" in var["properties"])
    assert guard_variant["properties"]["anchor"]["enum"] == ["L2", "L4"]
    assert guard_variant["required"] == ["anchor", "guard", "return_expression"]
    assert list(sorted(branches, key=lambda x:int(x[1:]))) == ["L2", "L3", "L4", "L5", "L6"]
    assert "L1" not in line_catalog(SOURCE)
    for var in replacement_variants:
        assert var["required"] == ["anchor", "new"]
    for var in variants:
        assert var["additionalProperties"] is False
    assert not re.fullmatch(branches["L2"]["properties"]["new"]["pattern"], "return 1")
    assert re.fullmatch(branches["L2"]["properties"]["new"]["pattern"], "if not x:\n    return 1\nif x is None:")
    assert not re.fullmatch(branches["L6"]["properties"]["new"]["pattern"], "return x[0]")
    assert re.fullmatch(branches["L6"]["properties"]["new"]["pattern"], "return x.get('name')")
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


def test_noop_entry_does_not_discard_an_independent_real_change():
    raw=json.dumps({"edits":[
        {"anchor":"L2","new":"if x is None:"},
        {"anchor":"L6","new":"return 0"},
    ]})
    assembled, _=assemble_anchors(raw,SOURCE)
    assert assembled==SOURCE.replace("    return x\n","    return 0\n")
    assert assemble_anchors(raw,SOURCE)[0]==assembled


def test_integration_cosmetic_only_revision_is_still_rejected_by_ast():
    from app.structured_anchors import apply_anchored_edits
    from app.structured_code_worker import StructuredBlocked
    from test_structured_diagnostics import revision_contract
    before, proposal=revision_contract()
    seen=[]
    with pytest.raises(StructuredBlocked, match="candidate_repeated"):
        apply_anchored_edits(reply("L2","return (None)"),before,proposal,observer=seen.append)
    assert seen[-1]["stage"]=="rejected"
    assert seen[-1]["reason_code"]=="candidate_repeated"

def test_model_strict_guard_and_single_line_repairs_all_42_synthetically():
    # Positive control only. This does not supply a production fallback answer.
    from app.structured_anchors import apply_anchored_edits
    from app import structured_code_worker as worker
    from test_structured_local_revision import proposal
    source, spec = proposal()
    replacements = {
        "return select_value.get('name').strip()":
            "return select_value.get('name').strip() if isinstance(select_value.get('name'), str) else ''",
        "text_content = fragment.get('text', {}).get('content')":
            "text_content = fragment.get('text', {}).get('content') if isinstance(fragment.get('text', {}), dict) else None",
        "if isinstance(fragment, dict):":
            {"guard": "not isinstance(fragment, dict)", "return_expression": "''"},
        "result.append(plain_text.strip())": "result.append(plain_text)",
        "result.append(text_content.strip())": "result.append(text_content)",
        "return ''.join(result)": "return ''.join(result).strip()",
    }
    edits = []
    for number, line in enumerate(source.splitlines(), 1):
        replacement = replacements.get(line.strip())
        if replacement is not None:
            edits.append({"anchor": f"L{number}",
                          **(replacement if isinstance(replacement, dict)
                             else {"new": replacement})})
    assert len(edits) == 6
    raw = json.dumps({"edits": edits})
    generated, used = apply_anchored_edits(raw, source, spec, model_strict=True)
    assert used == edits
    assert apply_anchored_edits(raw, source, spec, model_strict=True)[0] == generated
    report = worker.validate(json.loads(generated)["function"], worker.proposal_spec(spec))
    assert report == {"passed": 42, "total": 42, "errors": 0, "failures": []}
    # Synthetic positive control of the real split-schema assembly, not a
    # model E2E: the trusted 42 cases must still reject a regression.
    split = json.dumps({
        "edits": [e for e in edits if "new" in e],
        "guards": [e for e in edits if "guard" in e],
    })
    split_result, split_edits = apply_anchored_edits(
        split, source, spec, model_strict=True, model_split=True)
    assert split_edits == [e for e in edits if "new" in e] + [
        e for e in edits if "guard" in e]
    assert split_result == generated
    assert worker.validate(json.loads(split_result)["function"],
                           worker.proposal_spec(spec)) == report


@pytest.mark.parametrize("bad_new", ["", "if x:", "if x:\n    return 2",
                                     "return x\nreturn x"])
def test_model_strict_rejects_incomplete_or_multiline_replacements(bad_new):
    from app.structured_anchors import apply_anchored_edits
    from app.structured_code_worker import StructuredBlocked
    from test_structured_diagnostics import revision_contract
    before, proposal = revision_contract()
    seen = []
    with pytest.raises(StructuredBlocked, match="^revision_block_header$"):
        apply_anchored_edits(reply("L2", bad_new), before, proposal,
                             observer=seen.append, model_strict=True)
    assert seen[-1]["stage"] == "rejected"
    assert seen[-1]["reason_code"] == "revision_block_header"


def test_legacy_multiline_assembler_remains_compatible():
    assert assemble_anchors(reply("L6", "if x:\n    return 2\nreturn x"), SOURCE)[0].endswith(
        "    if x:\n        return 2\n    return x\n")


@pytest.mark.parametrize(("change", "expected_kind"), [
    ({"edits": [{"anchor": "L999", "new": "return x"}]}, "unknown_anchor"),
    ({"edits": [{"anchor": "L999", "guard": "not x",
                 "return_expression": "None"}]}, "unknown_anchor"),
    ({"edits": [{"anchor": "L2", "guard": "not x",
                 "return_expression": "None"}]}, "guard_not_if_header"),
])
def test_anchor_rejection_kind_is_bounded_and_replayable(change, expected_kind):
    from app.structured_anchors import apply_anchored_edits
    from app.structured_code_worker import StructuredBlocked
    from test_structured_diagnostics import revision_contract

    before, spec = revision_contract()
    original = json.dumps(spec, sort_keys=True)
    raw = json.dumps(change)
    snapshots = []
    for _ in range(2):
        recorded = []
        with pytest.raises(StructuredBlocked, match="^revision_anchor_invalid$"):
            apply_anchored_edits(raw, before, spec, observer=recorded.append,
                                 model_strict=True)
        assert [event["stage"] for event in recorded] == ["received", "rejected"]
        assert "anchor_failure_kind" not in recorded[0]
        final = recorded[-1]
        assert final["reason_code"] == "revision_anchor_invalid"
        assert final["failed_stage"] == "received"
        assert final["anchor_failure_kind"] == expected_kind
        assert "L999" not in json.dumps(recorded)
        assert "not x" not in json.dumps(recorded)
        assert raw not in json.dumps(recorded)
        assert json.dumps(spec, sort_keys=True) == original
        snapshots.append(recorded)
    assert snapshots[0] == snapshots[1]


def test_valid_guard_retains_legacy_structured_assembly():
    before = SOURCE
    edit = json.dumps({"edits": [{"anchor": "L2",
                                 "guard": "not isinstance(x, dict)",
                                 "return_expression": "None"}]})
    resulting, edits = assemble_anchors(edit, before)
    assert len(edits) == 1
    assert resulting.startswith("def f(x):\n"
                                "    if not isinstance(x, dict):\n"
                                "        return None\n"
                                "    if x is None:\n")
    assert before == SOURCE

def test_model_split_schema_is_source_bound_and_has_disjoint_variants():
    from app.structured_anchors import model_split_schema
    schema = model_split_schema(SOURCE)
    assert schema["required"] == ["edits", "guards"]
    assert schema["additionalProperties"] is False
    assert "oneOf" not in json.dumps(schema) and "anyOf" not in json.dumps(schema)
    guards = schema["properties"]["guards"]["items"]["properties"]["anchor"]["enum"]
    assert guards == ["L2", "L3", "L4", "L5", "L6"]
    assert schema["properties"]["edits"]["items"]["properties"]["anchor"]["enum"] == list(line_catalog(SOURCE))


@pytest.mark.parametrize("raw,reason", [
    (json.dumps({"edits": []}), "revision_schema"),
    (json.dumps({"edits": [], "guards": []}), "revision_schema"),
    (json.dumps({"edits": [{"anchor": "L2", "guard": "not x", "return_expression": "None"}], "guards": []}), "revision_edit_schema"),
    (json.dumps({"edits": [], "guards": [{"anchor": "L6", "guard": "not x", "return_expression": "''"}]}), "revision_guard_expression"),
    (json.dumps({"edits": [], "guards": [{"anchor": "L999", "guard": "not x", "return_expression": "None"}]}), "revision_anchor_invalid"),
    (json.dumps({"edits": [{"anchor": "L6", "new": "return None"}] * 7, "guards": []}), "revision_schema"),
])
def test_model_split_negative_boundaries_fail_closed(raw, reason):
    with pytest.raises(AnchorRejected, match="^" + reason + "$"):
        assemble_anchors(raw, SOURCE, model_split=True)


def test_model_split_guard_and_replacement_are_deterministic():
    raw = json.dumps({"edits": [{"anchor": "L6", "new": "return None"}],
                      "guards": [{"anchor": "L2", "guard": "not isinstance(x, dict)",
                                  "return_expression": "''"}]})
    first, changes = assemble_anchors(raw, SOURCE, model_split=True)
    assert first == assemble_anchors(raw, SOURCE, model_split=True)[0]
    assert len(changes) == 2
    assert first.splitlines()[1:4] == [
        "    if not isinstance(x, dict):",
        "        return ''",
        "    if x is None:",
    ]
    assert [edit["anchor"] for edit in changes] == ["L6", "L2"]
    assert SOURCE.splitlines() == [
        "def f(x):", "    if x is None:", "        return ''",
        "    if x:", "        return ''", "    return x",
    ]


def test_model_split_keeps_legacy_parser_available():
    legacy = json.dumps({"edits": [{"anchor": "L6", "new": "return None"}]})
    assert assemble_anchors(legacy, SOURCE)[1][0]["anchor"] == "L6"
    with pytest.raises(AnchorRejected, match="^revision_schema$"):
        assemble_anchors(legacy, SOURCE, model_split=True)

def test_model_split_duplicate_anchor_across_edit_kinds_fails():
    raw = json.dumps({
        "edits": [{"anchor": "L2", "new": "if x is None:"}],
        "guards": [{"anchor": "L2", "guard": "not x", "return_expression": "None"}],
    })
    with pytest.raises(AnchorRejected, match="^revision_anchor_duplicate$"):
        assemble_anchors(raw, SOURCE, model_split=True)

def test_guard_return_literals_are_source_bound_constants_not_codegen():
    from app.structured_anchors import guard_return_literals, model_split_schema
    assert guard_return_literals(SOURCE) == ["''"]
    schema = model_split_schema(SOURCE)
    enum = schema["properties"]["guards"]["items"]["properties"]["return_expression"]["enum"]
    assert enum == ["''"]
    assert "return ''" not in enum
    assert guard_return_literals(SOURCE) == guard_return_literals(SOURCE)


def test_model_split_cannot_fabricate_guard_value_without_literal_source():
    from app.structured_anchors import guard_return_literals, model_split_schema
    source = "def f(x):\n    if x:\n        return x\n    return x\n"
    assert guard_return_literals(source) == []
    assert model_split_schema(source)["properties"]["guards"]["maxItems"] == 0


@pytest.mark.parametrize("invalid", [
    "return ''", "return None", "x", "True", "2", "list()",
    "[]", "'';import os", "if x:", "not isinstance(x, dict)",
])
def test_model_split_rejects_nonliteral_return_expression(invalid):
    raw = json.dumps({"edits": [], "guards": [{
        "anchor": "L2", "guard": "not isinstance(x, dict)",
        "return_expression": invalid,
    }]})
    with pytest.raises(AnchorRejected,
                       match="^(revision_edit_schema|revision_indentation)$"):
        assemble_anchors(raw, SOURCE, model_split=True)


@pytest.mark.parametrize("invalid", [
    "if not x:", "return x", "x:", "not (", "x\nreturn None",
])
def test_model_split_rejects_invalid_guard_expression(invalid):
    raw = json.dumps({"edits": [], "guards": [{
        "anchor": "L2", "guard": invalid, "return_expression": "''",
    }]})
    with pytest.raises(AnchorRejected,
                       match="^(revision_edit_schema|revision_indentation)$"):
        assemble_anchors(raw, SOURCE, model_split=True)


def test_model_split_canonicalizes_valid_literal_and_guard_ast():
    raw = json.dumps({"edits": [], "guards": [{
        "anchor": "L2", "guard": "not   isinstance(x, dict)",
        "return_expression": '""',
    }]})
    before = SOURCE
    assembled, edits = assemble_anchors(raw, before, model_split=True)
    assert len(edits) == 1
    assert assembled.splitlines()[1:4] == [
        "    if not isinstance(x, dict):",
        "        return ''",
        "    if x is None:",
    ]
    assert assemble_anchors(raw, before, model_split=True)[0] == assembled
    assert before == SOURCE


def test_original_sanitized_syntax_fingerprint_reproduces_invalid_return():
    import ast
    import hashlib
    # AST error location and digest observed in the exact real CI run.
    bad_line = "                return return ''"
    assert len(bad_line) == 32
    assert (hashlib.sha256(bad_line.encode()).hexdigest()
            == "d250a8c888c84a68dbe938d3a90bcec9ae94e6a814cb8bef1d28c041d7981c45")
    with pytest.raises(SyntaxError) as error:
        ast.parse("def f(x):\n            if x:\n" + bad_line + "\n")
    assert error.value.offset == 24


def test_legacy_guard_keeps_previous_unrestricted_value_contract():
    raw = json.dumps({"edits": [{
        "anchor": "L2", "guard": "not x", "return_expression": "None",
    }]})
    assembled, _ = assemble_anchors(raw, SOURCE)
    assert assembled.splitlines()[2] == "        return None"
