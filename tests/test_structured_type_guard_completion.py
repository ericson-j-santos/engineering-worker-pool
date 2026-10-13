"""Real acceptance-driven AST guard completion; synthetic proposals are not E2E."""
from __future__ import annotations

import json

from app import structured_code_worker as worker
from app.structured_type_guard_completion import (
    MAX_GUARD_TRIALS, _guard_sites, complete_checked_type_guard,
)
from test_structured_code_worker import FIXED, task, repository
from test_structured_observations import REPLY


GOOD_SELECT = ('        value = selected.get("name")\n'
               '        if not isinstance(value, str):\n'
               '            return ""\n')
BAD_SELECT = '        value = selected.get("name", "").strip()\n'
BROKEN = FIXED.replace(GOOD_SELECT, BAD_SELECT)
assert BROKEN != FIXED


def test_guard_candidates_are_ast_bound_and_have_no_embedded_solution():
    from app.structured_code_worker import extracted
    function = extracted(BROKEN, task())
    sites = _guard_sites(function)
    assert 1 <= len(sites) <= MAX_GUARD_TRIALS
    assert any("selected.get('name')" in condition
               and "not isinstance(" in condition for _, condition in sites)
    assert all(anchor.startswith("L") and "return " not in condition
               for anchor, condition in sites)
    assert _guard_sites(function) == sites


def test_rejected_model_like_candidate_becomes_42_by_a_validated_type_guard_only():
    contract = task()
    baseline = worker.validate(BROKEN, contract)
    assert baseline["passed"] < baseline["total"] == 42
    assert baseline["errors"] > 0
    result = complete_checked_type_guard(BROKEN, contract, baseline)
    assert result is not None
    corrected, checked, proof = result
    assert checked == {"passed": 42, "total": 42, "errors": 0, "failures": []}
    assert proof["result"] == "AST_GUARD_COMPLETED"
    assert proof["before_sha256"] == worker.sha256(BROKEN.encode())
    assert proof["after_sha256"] == worker.sha256(corrected.encode())
    assert 1 <= proof["trials"] <= MAX_GUARD_TRIALS
    assert 'if not isinstance(selected.get("name"), str):' in corrected or (
        "if not isinstance(selected.get('name'), str):" in corrected)
    assert complete_checked_type_guard(BROKEN, contract, baseline) == result
    assert GOOD_SELECT not in BROKEN
    assert BAD_SELECT in BROKEN


def test_unrepairable_wrong_candidate_stays_blocked_and_unchanged():
    bad = "def _property(properties, name):\n    return ''\n"
    contract = task()
    baseline = worker.validate(bad, contract)
    assert baseline["passed"] < baseline["total"]
    assert complete_checked_type_guard(bad, contract, baseline) is None
    assert bad.endswith("    return ''\n")


def test_trusted_worker_commits_and_replays_only_after_validator_proves_42(tmp_path):
    root, state, contract = repository(tmp_path)
    replies = [REPLY, json.dumps({"function": BROKEN})]
    calls = []
    def propose(_task, _before):
        calls.append(1)
        return replies.pop(0)
    result = worker.repair(root, state, contract, propose)
    assert len(calls) == result["model_calls"] == 2
    assert result["validation"] == {"passed": 42, "total": 42, "errors": 0, "failures": []}
    proof = result["attempts"][-1]["ast_guard_completion"]
    assert proof["result"] == "AST_GUARD_COMPLETED"
    assert proof["after_sha256"] == result["after_sha256"]
    assert result["replayed"] is False
    replay = worker.repair(root, state, contract, lambda *_: (_ for _ in ()).throw(
        AssertionError("model must not be called during replay")))
    assert replay["model_calls"] == 0 and replay["replayed"] is True
    assert replay["produced_sha"] == result["produced_sha"]
    assert "OTHER = 17" in (root / contract["path"]).read_text()
