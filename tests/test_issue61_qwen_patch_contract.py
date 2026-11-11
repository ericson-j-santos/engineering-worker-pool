"""Fail-closed regression tests for repair code sourced from a real model."""
import json
import pytest
from scripts.issue61_qwen_patch_contract import (
    PatchRejected, ACCEPT_VALID, ACCEPT_INVALID, extract_model_patch,
)


def candidate(line):
    return json.dumps({"replacement_line": line})


@pytest.mark.parametrize("line", [
    'TODO_ID = re.compile(r"TODO-[1-9][0-9]*\\Z")',
    'TODO_ID = re.compile(r"^TODO-[1-9][0-9]*$")',
    'TODO_ID = re.compile(r"\\ATODO-[1-9][0-9]*\\Z")',
])
def test_valid_model_authored_replacement(line):
    result, pattern = extract_model_patch(candidate(line))
    assert result == line
    assert pattern


@pytest.mark.parametrize("payload,code", [
    ('not json', 'MODEL_JSON_INVALID'),
    ('{"pattern": "TODO-.*"}', 'MODEL_SCHEMA_INVALID'),
    (candidate("print('unsafe')"), 'MODEL_CODE_ASSIGNMENT_INVALID'),
    (candidate("OTHER = re.compile('TODO-[1-9][0-9]*')"), 'MODEL_CODE_TARGET_NOT_ALLOWED'),
    (candidate("TODO_ID = __import__('os').system('true')"), 'MODEL_CODE_CALL_NOT_ALLOWED'),
    (candidate("TODO_ID = re.compile('TODO-.*', re.I)"), 'MODEL_CODE_CALL_NOT_ALLOWED'),
    (candidate("TODO_ID = re.compile('TODO-[')"), 'MODEL_PATTERN_SYNTAX_INVALID'),
    (candidate("TODO_ID = re.compile('TODO-.*')"), 'MODEL_REPAIR_ACCEPTANCE_FAILED'),
    (candidate("TODO_ID = re.compile('TODO-[0-9]*')"), 'MODEL_REPAIR_ACCEPTANCE_FAILED'),
    (candidate("TODO_ID = re.compile('TODO-[1-9]')"), 'MODEL_REPAIR_ACCEPTANCE_FAILED'),
    (candidate("TODO_ID = re.compile('TODO-[1-9][0-9]*')\nprint('x')"),
     'MODEL_CODE_FORMAT_INVALID'),
    (candidate("TODO_ID = re.compile('notTODO-[1-9][0-9]*')"),
     'MODEL_PATTERN_SCOPE_INVALID'),
])
def test_negative_model_output_fails_before_materialization(payload, code):
    with pytest.raises(PatchRejected, match=code):
        extract_model_patch(payload)


def test_no_execution_of_model_generated_python(tmp_path):
    marker = tmp_path / "should-not-exist"
    malicious = 'TODO_ID = re.compile(__import__("pathlib").Path(' + repr(str(marker)) + ').touch())'
    with pytest.raises(PatchRejected, match="MODEL_CODE_CALL_NOT_ALLOWED"):
        extract_model_patch(candidate(malicious))
    assert not marker.exists()


def test_contract_positives_and_negatives_disjoint():
    assert not set(ACCEPT_VALID) & set(ACCEPT_INVALID)
    assert "TODO-54" in ACCEPT_VALID
    assert "TODO-54-validation" in ACCEPT_INVALID
