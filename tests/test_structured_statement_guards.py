"""AST-bound model-split type guards; no generated fallback or trusted examples."""
from __future__ import annotations

import ast
import json

import pytest

from app.structured_anchors import (
    AnchorRejected, assemble_anchors, model_split_schema, statement_anchors,
    validated_type_guard,
)

SOURCE = ("def f(x):\n"
          "    if not isinstance(x, dict):\n"
          "        return ''\n"
          "    name = x.get('name')\n"
          "    return name.strip()\n")


def reply(anchor: str, guard: str, result: str = "''") -> str:
    return json.dumps({"edits": [], "guards": [{
        "anchor": anchor, "guard": guard, "return_expression": result,
    }]})


def test_statement_anchor_catalog_includes_return_and_assignment_not_signature():
    assert statement_anchors(SOURCE) == ["L2", "L3", "L4", "L5"]
    allowed = model_split_schema(SOURCE)["properties"]["guards"]["items"]["properties"]["anchor"]["enum"]
    assert allowed == statement_anchors(SOURCE)
    assert "L1" not in allowed
    assert statement_anchors(SOURCE) == statement_anchors(SOURCE)


def test_type_guard_on_return_line_preserves_source_and_replays_without_coercion():
    proposal = reply("L5", "not isinstance(name, str)")
    first, edits = assemble_anchors(proposal, SOURCE, model_split=True)
    assert edits == json.loads(proposal)["guards"]
    assert first.endswith("    if not isinstance(name, str):\n"
                          "        return ''\n    return name.strip()\n")
    assert assemble_anchors(proposal, SOURCE, model_split=True)[0] == first
    assert SOURCE.endswith("    return name.strip()\n")
    scope = {}
    exec(compile(ast.parse(first), "<typed-guard-test>", "exec"), scope)
    assert scope["f"]({"name": 13}) == ""
    assert scope["f"]({"name": " valid "}) == "valid"
    assert scope["f"]("not-a-dict") == ""


def test_nullable_guard_is_ast_checked_and_preserves_none_fallback_path():
    source = ("def f(fragment):\n"
              "    if not isinstance(fragment, dict):\n"
              "        return ''\n"
              "    plain = fragment.get('plain_text')\n"
              "    return plain if isinstance(plain, str) else ''\n")
    guard = ("fragment.get('plain_text') is not None and "
             "not isinstance(fragment.get('plain_text'), str)")
    modified, _ = assemble_anchors(reply("L4", guard), source, model_split=True)
    assert "    if fragment.get('plain_text') is not None and " in modified
    assert "        return ''\n    plain = fragment.get('plain_text')" in modified
    scope = {}
    exec(compile(ast.parse(modified), "<nullable-guard-test>", "exec"), scope)
    assert scope["f"]({"plain_text": 42}) == ""
    assert scope["f"]({"plain_text": None}) == ""
    assert scope["f"]({"plain_text": "valid"}) == "valid"


@pytest.mark.parametrize("expression", [
    "str(name) != ''",
    "not isinstance(str(name), str)",
    "not isinstance(x.get('a').get('b'), str)",
    "not isinstance(name, type(None))",
    "name is not None and not isinstance(other, str)",
    "name is not None or not isinstance(name, str)",
    "not isinstance(name, str) or True",
    "name.strip()",
    "__import__('os')",
    "not isinstance(name, str) and __import__('os')",
])
def test_untrusted_guards_rejected_before_execution(expression):
    with pytest.raises(AnchorRejected, match="^revision_guard_expression$"):
        assemble_anchors(reply("L5", expression), SOURCE, model_split=True)


def test_non_statement_and_legacy_non_if_guard_fail_closed():
    source = ("def f(x):\n"
              "    if x:\n"
              "        return ''\n"
              "    else:\n"
              "        return ''\n")
    assert "L4" not in statement_anchors(source)
    with pytest.raises(AnchorRejected, match="^revision_anchor_invalid$") as captured:
        assemble_anchors(reply("L4", "not isinstance(x, str)"), source, model_split=True)
    assert captured.value.anchor_failure_kind == "guard_not_statement"
    with pytest.raises(AnchorRejected, match="^revision_anchor_invalid$") as legacy:
        assemble_anchors(json.dumps({"edits": [{
            "anchor": "L5", "guard": "not isinstance(x, str)",
            "return_expression": "''",
        }]}), source)
    assert legacy.value.anchor_failure_kind == "guard_not_if_header"


def test_real_guarded_validation_improves_observed_failure_without_writing_patch():
    from app.structured_anchors import apply_anchored_edits
    from app.structured_code_worker import proposal_spec, validate
    from test_structured_local_revision import proposal
    candidate, spec = proposal()
    task = proposal_spec(spec)
    baseline = validate(candidate, task)
    lines = candidate.splitlines()
    target = next(f"L{i}" for i, line in enumerate(lines, 1)
                  if "return select_value.get('name').strip()" in line)
    raw = reply(target, "not isinstance(select_value.get('name'), str)")
    repaired, edits = apply_anchored_edits(
        raw, candidate, spec, model_strict=True, model_split=True)
    checked = validate(json.loads(repaired)["function"], task)
    assert checked["total"] == baseline["total"] == 42
    assert checked["passed"] > baseline["passed"]
    assert checked["errors"] < baseline["errors"]
    assert len(edits) == 1 and edits[0]["anchor"] == target


def test_safe_guard_expression_cannot_be_derived_from_code_execution():
    guard = ast.parse("not isinstance(x.get('name'), str)", mode="eval").body
    assert validated_type_guard(guard) == "not isinstance(x.get('name'), str)"
    assert "L5" in statement_anchors(SOURCE)
