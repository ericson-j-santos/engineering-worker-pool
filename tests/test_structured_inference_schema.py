"""Bounded local inference grammar; all edit safety remains in the assembler."""
from __future__ import annotations

import json
import re
import pytest

from app.structured_anchors import (
    inference_schema, lightweight_anchor_schema, replacement_only_schema, anchored_schema, assemble_anchors, AnchorRejected,
)

SOURCE = ("def f(x):\n"
          "    if isinstance(x, dict):\n"
          "        return x.get('name')\n"
          "    return ''\n")


def test_compact_generation_schema_is_bounded_without_nested_oneof():
    schema = inference_schema(SOURCE)
    edit = schema["properties"]["edits"]["items"]
    assert "oneOf" not in edit and "anyOf" not in edit
    assert edit["required"] == ["anchor"]
    assert edit["additionalProperties"] is False
    assert edit["properties"]["anchor"]["enum"] == ["L2", "L3", "L4"]
    assert schema["properties"]["edits"]["maxItems"] == 6
    assert schema["properties"]["edits"]["minItems"] == 1
    assert set(edit["properties"]) == {"anchor", "new", "guard", "return_expression"}
    assert edit["properties"]["new"]["maxLength"] == 6000
    assert edit["properties"]["guard"]["maxLength"] == 2000
    assert edit["properties"]["return_expression"]["maxLength"] == 2000
    assert schema["additionalProperties"] is False
    # The original authoritative metadata contract is intentionally untouched.
    assert "oneOf" in anchored_schema(SOURCE)["properties"]["edits"]["items"]


def test_compact_generation_still_rejects_subscript_characters():
    item = inference_schema(SOURCE)["properties"]["edits"]["items"]
    assert re.fullmatch(item["properties"]["new"]["pattern"],
                        "return x.get('name')")
    assert not re.fullmatch(item["properties"]["new"]["pattern"], "return x[0]")
    assert not re.fullmatch(item["properties"]["guard"]["pattern"], "x[0]")
    assert not re.fullmatch(item["properties"]["guard"]["pattern"], "x\nreturn None")



def test_lightweight_schema_enumerates_source_anchors_without_regex_cost():
    strict = inference_schema(SOURCE)
    lite = lightweight_anchor_schema(SOURCE)
    assert lite is not strict
    assert lite["properties"]["edits"]["minItems"] == 1
    assert lite["properties"]["edits"]["maxItems"] == 6
    items = lite["properties"]["edits"]["items"]
    assert items["properties"]["anchor"]["enum"] == ["L2", "L3", "L4"]
    assert items["required"] == ["anchor"]
    assert items["additionalProperties"] is False
    assert "oneOf" not in items and "anyOf" not in items
    for name in ("new", "guard", "return_expression"):
        assert items["properties"][name] == {"type": "string"}
        assert "pattern" in strict["properties"]["edits"]["items"]["properties"][name]
    assert lightweight_anchor_schema(SOURCE) == lite
    assert "L1" not in items["properties"]["anchor"]["enum"]



def test_model_only_uses_anchor_line_replacements_not_guard_shortcuts():
    schema = replacement_only_schema(SOURCE)
    items = schema["properties"]["edits"]["items"]
    assert items["required"] == ["anchor", "new"]
    assert set(items["properties"]) == {"anchor", "new"}
    assert items["properties"]["anchor"]["enum"] == ["L2", "L3", "L4"]
    assert items["additionalProperties"] is False
    assert schema["properties"]["edits"]["maxItems"] == 6
    assert replacement_only_schema(SOURCE) == schema
    # Existing trusted callers can still use typed guards; only model generation is restricted.
    old = json.dumps({"edits": [{"anchor": "L2", "guard": "not isinstance(x, dict)",
                                "return_expression": "''"}]})
    parsed, _ = assemble_anchors(old, SOURCE)
    assert "if not isinstance(x, dict):" in parsed


def test_valid_literal_and_typed_guard_edits_remain_pure_and_replayable():
    original = SOURCE
    data = json.dumps({"edits": [
        {"anchor": "L2", "guard": "not isinstance(x, dict)",
         "return_expression": "''"},
        {"anchor": "L4", "new": "return x"},
    ]})
    first, edits = assemble_anchors(data, SOURCE)
    assert edits == json.loads(data)["edits"]
    assert first == assemble_anchors(data, SOURCE)[0]
    assert "    if not isinstance(x, dict):\n        return ''\n" in first
    assert first.endswith("    return x\n")
    assert SOURCE == original


@pytest.mark.parametrize("edits,reason", [
    ([{"anchor": "L2", "new": "return 1"}], "revision_block_header"),
    ([{"anchor": "L3", "guard": "True", "return_expression": "''"}],
     "revision_anchor_invalid"),
    ([{"anchor": "L1", "new": "return 1"}], "revision_anchor_invalid"),
    ([{"anchor": "L2", "new": "if x:", "guard": "True"}],
     "revision_edit_schema"),
    ([{"anchor": "L2", "guard": "x[0]", "return_expression": "''"}],
     "revision_indentation"),
])
def test_compact_schema_does_not_bypass_runtime_guards(edits, reason):
    with pytest.raises(AnchorRejected, match="^" + reason + "$"):
        assemble_anchors(json.dumps({"edits": edits}), SOURCE)


def test_same_source_and_schema_deterministic():
    assert inference_schema(SOURCE) == inference_schema(SOURCE)
    assert "L1" not in inference_schema(SOURCE)["properties"]["edits"]["items"]["properties"]["anchor"]["enum"]
