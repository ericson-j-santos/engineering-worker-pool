from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_cutover_readiness.py"
POLICY_PATH = ROOT / "migration" / "cutover-policy-v1.json"
RUNBOOK = ROOT / "docs" / "CUTOVER_ROLLBACK.md"

SPEC = importlib.util.spec_from_file_location("cutover_readiness", SCRIPT)
assert SPEC and SPEC.loader
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def policy() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def ready_evidence() -> dict:
    return {
        "reqsys_sha": "1" * 40,
        "worker_pool_sha": "2" * 40,
        "contract_version": "v1",
        "correlation_id": "cutover-test-1",
        "dedicated_repo_ci_green": True,
        "public_contract_v1_compatible": True,
        "reqsys_consumer_e2e_passed": True,
        "physical_runtime_validated": True,
        "replay_idempotent": True,
        "independent_readback": True,
        "secrets_migrated_to_git": False,
        "legacy_reqsys_present": True,
    }


def test_policy_requires_physical_evidence_and_preserved_rollback() -> None:
    p = policy()
    assert p["decision_mode"] == "fail_closed"
    assert p["required_evidence"]["physical_runtime_validated"] is True
    assert p["required_evidence"]["legacy_reqsys_present"] is True
    assert p["required_evidence"]["secrets_migrated_to_git"] is False
    assert p["rollback"]["allowed_fallback_scope"] == "temporary_404_only"
    assert p["legacy_removal"]["separate_increment_required"] is True


def test_current_migration_state_blocks_legacy_removal_without_physical_e2e() -> None:
    evidence = ready_evidence()
    evidence["physical_runtime_validated"] = False

    result = m.evaluate(policy(), evidence)

    assert result["status"] == "CUTOVER_BLOCKED"
    assert "evidence_mismatch:physical_runtime_validated" in result["blockers"]
    assert result["legacy_removal_allowed"] is False
    assert result["rollback_available"] is True


def test_all_required_evidence_makes_cutover_ready() -> None:
    result = m.evaluate(policy(), ready_evidence())

    assert result["status"] == "CUTOVER_READY"
    assert result["blockers"] == []
    assert result["legacy_removal_allowed"] is True
    assert result["rollback_available"] is True
    assert result["production_touched"] is False
    assert result["secrets_mutated"] is False


def test_secret_migration_to_git_blocks_cutover() -> None:
    evidence = ready_evidence()
    evidence["secrets_migrated_to_git"] = True

    result = m.evaluate(policy(), evidence)

    assert result["status"] == "CUTOVER_BLOCKED"
    assert "evidence_mismatch:secrets_migrated_to_git" in result["blockers"]


def test_missing_or_invalid_identity_fails_closed() -> None:
    evidence = ready_evidence()
    evidence.pop("correlation_id")
    evidence["reqsys_sha"] = "not-a-sha"

    result = m.evaluate(policy(), evidence)

    assert result["status"] == "CUTOVER_BLOCKED"
    assert "identity_missing:correlation_id" in result["blockers"]
    assert "identity_invalid:reqsys_sha" in result["blockers"]


def test_runbook_forbids_destructive_rollback_and_premature_legacy_removal() -> None:
    raw = RUNBOOK.read_text(encoding="utf-8")
    assert "CUTOVER_BLOCKED" in raw
    assert "physical_runtime_validated=false" in raw
    assert "não remover" in raw
    assert "reverter a mudança versionada de cutover" in raw
    assert "não excluir" in raw
