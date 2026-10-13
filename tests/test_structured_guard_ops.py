"""Typed guard edits: model chooses expressions; Python block syntax is fixed.

All proposals in this test file are synthetic controls, not local LLM success.
"""
from __future__ import annotations

import ast
import json
import pytest

from app.structured_anchors import assemble_anchors, anchored_schema, AnchorRejected
from app.structured_code_worker import StructuredBlocked


SOURCE = "def f(x):\n    if isinstance(x, dict):\n        return x.get('name')\n    return ''\n"


def guard(anchor, condition, result):
    return json.dumps({"edits": [{"anchor": anchor, "guard": condition,
                                  "return_expression": result}]})


def test_guard_adds_one_safe_block_without_replacing_the_old_header():
    raw = guard("L2", "not isinstance(x, dict)", "''")
    candidate, edits = assemble_anchors(raw, SOURCE)
    assert candidate == ("def f(x):\n"
                         "    if not isinstance(x, dict):\n"
                         "        return ''\n"
                         "    if isinstance(x, dict):\n"
                         "        return x.get('name')\n"
                         "    return ''\n")
    ast.parse(candidate)
    assert edits[0]["anchor"] == "L2"
    assert assemble_anchors(raw, SOURCE)[0] == candidate
    assert SOURCE.startswith("def f(x):\n    if isinstance")


def test_guard_variant_is_explicit_not_a_free_form_code_fallback():
    items = anchored_schema(SOURCE)["properties"]["edits"]["items"]["oneOf"]
    typed = [item for item in items if "guard" in item["properties"]]
    assert len(typed) == 1
    assert typed[0]["properties"]["anchor"]["enum"] == ["L2"]
    assert typed[0]["required"] == ["anchor", "guard", "return_expression"]
    assert typed[0]["additionalProperties"] is False
    assert typed[0]["properties"]["guard"]["maxLength"] == 2000


@pytest.mark.parametrize("anchor, condition, output, error", [
    ("L1", "True", "''", "revision_anchor_invalid"),
    ("L3", "True", "''", "revision_anchor_invalid"),
    ("L9", "True", "''", "revision_anchor_invalid"),
    ("L2", "", "''", "revision_edit_schema"),
    ("L2", "True", "", "revision_edit_schema"),
    ("L2", "x\nreturn None", "''", "revision_indentation"),
    ("L2", "True", "x[0]", "revision_indentation"),
    ("L2", "x[0]", "''", "revision_indentation"),
])
def test_invalid_or_unsafe_guard_inputs_fail_closed(anchor, condition, output, error):
    with pytest.raises(AnchorRejected, match="^" + error + "$"):
        assemble_anchors(guard(anchor, condition, output), SOURCE)


def test_duplicate_guard_and_replacement_on_same_line_is_rejected():
    data = {"edits": [{"anchor": "L2", "guard": "True", "return_expression": "''"},
                      {"anchor": "L2", "new": "if isinstance(x, dict):"}]}
    with pytest.raises(AnchorRejected, match="^revision_anchor_duplicate$"):
        assemble_anchors(json.dumps(data), SOURCE)


def test_model_chosen_guard_cannot_execute_unknown_call():
    from app.structured_anchors import apply_anchored_edits
    from test_structured_local_revision import proposal
    source, spec = proposal()
    anchor = next("L"+str(i) for i, line in enumerate(source.splitlines(), 1)
                  if line.strip() == "if isinstance(fragment, dict):")
    with pytest.raises(StructuredBlocked, match="^candidate_call$"):
        apply_anchored_edits(guard(anchor, "__import__('os')", "''"), source, spec)


def test_six_model_shaped_typed_edits_can_pass_42_synthetic_cases():
    from app.structured_anchors import apply_anchored_edits
    from app.structured_code_worker import proposal_spec, validate
    from test_structured_local_revision import proposal
    source, spec = proposal()
    edits = []
    replacements = {
        "return select_value.get('name').strip()":
            "return select_value.get('name').strip() if isinstance(select_value.get('name'), str) else ''",
        "text_content = fragment.get('text', {}).get('content')":
            "text_value = fragment.get('text')\ntext_content = text_value.get('content') if isinstance(text_value, dict) else None",
        "result.append(plain_text.strip())": "result.append(plain_text)",
        "result.append(text_content.strip())": "result.append(text_content)",
        "return ''.join(result)": "return ''.join(result).strip()",
    }
    for i, line in enumerate(source.splitlines(), 1):
        original = line.strip()
        if original == "if isinstance(fragment, dict):":
            edits.append({"anchor": f"L{i}", "guard": "not isinstance(fragment, dict)",
                          "return_expression": "''"})
        elif original in replacements:
            edits.append({"anchor": f"L{i}", "new": replacements[original]})
    assert len(edits) == 6
    reply = json.dumps({"edits": edits})
    assembled, observed = apply_anchored_edits(reply, source, spec)
    assert observed == edits
    result = validate(json.loads(assembled)["function"], proposal_spec(spec))
    assert result == {"passed": 42, "total": 42, "errors": 0, "failures": []}
    assert apply_anchored_edits(reply, source, spec)[0] == assembled
    assert spec["cases"] == proposal()[1]["cases"]
