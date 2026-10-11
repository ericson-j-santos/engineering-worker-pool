"""Regression of the observed Subscript in run 38108465920.

These are generation-schema controls, not proof of convergence of the model.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re

import pytest

from app.structured_revision import REVISION_SCHEMA, REVISION_NEW_PATTERN

@pytest.mark.parametrize("text", [
    "return properties[name]",
    "return selected['name'] if isinstance(selected.get('name'), str) else ''",
    "return items[0]",
    "return items[:1]",
    "parts = []",
    "value = props.get('name')\n    return value[0]",
])
def test_revision_generation_blocks_square_bracket_constructs(text):
    constraint = REVISION_SCHEMA["properties"]["edits"]["items"]["properties"]["new"]
    assert constraint["pattern"] == REVISION_NEW_PATTERN
    assert re.fullmatch(constraint["pattern"], text) is None

@pytest.mark.parametrize("text", [
    "",
    "return selected.get('name') if isinstance(selected.get('name'), str) else ''",
    "parts = list()",
    "parts.append(value)\n    return ''.join(parts).strip()",
    "value = item.get('text')\n    if not isinstance(value, dict):\n        return ''",
    "return 'ação válida'",
])
def test_revision_generation_keeps_allowed_replacements(text):
    constraint = REVISION_SCHEMA["properties"]["edits"]["items"]["properties"]["new"]
    assert re.fullmatch(constraint["pattern"], text)

def test_revision_schema_does_not_restrict_old_text_or_change_edit_budget():
    item = REVISION_SCHEMA["properties"]["edits"]["items"]
    assert "pattern" not in item["properties"]["old"]
    assert REVISION_SCHEMA["properties"]["edits"]["maxItems"] == 6
    assert item["properties"]["new"]["maxLength"] == 6000
    assert item["required"] == ["old", "new"]
    assert item["additionalProperties"] is False

def test_schema_contract_negative_control_detects_missing_restriction():
    import copy
    schema = copy.deepcopy(REVISION_SCHEMA)
    pattern = schema["properties"]["edits"]["items"]["properties"]["new"]["pattern"]
    assert not re.fullmatch(pattern, "return properties[name]")
    schema["properties"]["edits"]["items"]["properties"]["new"]["pattern"] = ".*"
    assert re.fullmatch(schema["properties"]["edits"]["items"]["properties"]["new"]["pattern"],
                        "return properties[name]")
    assert not re.fullmatch(REVISION_NEW_PATTERN, "return properties[name]")

def test_real_execution_guard_still_rejects_subscript_even_without_schema():
    from app import structured_code_worker as worker
    from test_structured_code_worker import BEFORE, task
    assert ast.Subscript not in worker.NODES
    with pytest.raises(worker.StructuredBlocked, match="^candidate_grammar$"):
        worker.render(BEFORE, task(), json.dumps(
            {"function": "def _property(properties, name):\n    return properties[name]\n"}))
    assert ast.Subscript not in worker.NODES

def test_provider_supplies_same_revision_schema_and_no_extra_attempt():
    root = Path(__file__).resolve().parents[1]
    text = (root / "app/structured_ollama.py").read_text()
    assert '"format": inference_schema(before) if is_revision else' in text
    assert '"num_predict": 768 if is_revision else 1400' in text
    assert "Never use square brackets in replacement text." in text
    assert "observer=self.revision_observer" in text
