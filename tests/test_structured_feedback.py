"""One semantic feedback cycle, never retries a security rejection."""
import json
import pytest
from test_structured_code_worker import repository, FIXED, BEFORE, task
from app import structured_code_worker as worker


def test_semantic_feedback_converges_with_same_cases_and_one_commit(tmp_path):
    root, state, contract = repository(tmp_path)
    observed = []
    def propose(spec, source):
        observed.append((spec, source))
        if len(observed) == 1:
            return json.dumps({"function": "def _property(properties, name):\n    return ''\n"})
        assert spec["instruction"] == contract["instruction"]
        assert spec["feedback"]["failed_count"] > 0
        assert sorted(json.dumps(c, sort_keys=True) for c in spec["cases"]) == sorted(
            json.dumps(c, sort_keys=True) for c in contract["cases"])
        assert "return ''" in source
        return json.dumps({"function": FIXED})
    result = worker.repair(root, state, contract, propose)
    assert result["model_calls"] == len(observed) == len(result["attempts"]) == 2
    assert result["attempts"][0]["validation"]["passed"] < len(contract["cases"])
    assert result["attempts"][1]["validation"]["passed"] == len(contract["cases"])
    assert worker.git(root, "rev-list", "--count", contract["base_sha"] + "..HEAD") == "1"
    again = worker.repair(root, state, contract, lambda *_: pytest.fail("replay called model"))
    assert again["model_calls"] == 0 and again["produced_sha"] == result["produced_sha"]


def test_second_failed_proposal_is_terminal_and_does_not_write(tmp_path):
    root, state, contract = repository(tmp_path)
    calls = []
    def propose(*_):
        calls.append(1)
        return json.dumps({"function": "def _property(properties, name):\n    return ''\n"})
    with pytest.raises(worker.StructuredBlocked, match="^candidate_repeated$"):
        worker.repair(root, state, contract, propose)
    assert len(calls) == 2
    assert worker.git(root, "rev-parse", "HEAD") == contract["base_sha"]
    assert worker.git(root, "status", "--porcelain") == ""


def test_security_rejection_has_no_feedback_retry(tmp_path):
    root, state, contract = repository(tmp_path)
    calls = []
    def propose(*_):
        calls.append(1)
        return json.dumps({"function": "def _property(properties, name):\n    import os\n    return ''\n"})
    with pytest.raises(worker.StructuredBlocked):
        worker.repair(root, state, contract, propose)
    assert len(calls) == 1
    assert worker.git(root, "rev-parse", "HEAD") == contract["base_sha"]


def test_concurrent_change_after_first_failure_blocks_second_proposal(tmp_path):
    root, state, contract = repository(tmp_path)
    calls = []
    real_validate = worker.validate
    def propose(*_):
        calls.append(1)
        return json.dumps({"function": "def _property(properties, name):\n    return ''\n"})
    # The first failing candidate evaluation is followed by a concurrent external write.
    def validate(source, spec):
        result = real_validate(source, spec)
        if calls:
            (root / "external.txt").write_text("preserve")
        return result
    from unittest.mock import patch
    with patch.object(worker, "validate", validate), pytest.raises(
        worker.StructuredBlocked, match="^concurrent_change$"):
        worker.repair(root, state, contract, propose)
    assert len(calls) == 1
    assert (root / "external.txt").read_text() == "preserve"
