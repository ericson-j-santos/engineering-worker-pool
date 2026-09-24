#!/usr/bin/env python3
"""Avalia prontidão de cutover do Engineering Worker Pool sem executar mutações."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

SHA40 = re.compile(r"^[0-9a-f]{40}$")
READY = "CUTOVER_READY"
BLOCKED = "CUTOVER_BLOCKED"


class CutoverPolicyError(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CutoverPolicyError("json_root_must_be_object")
    return payload


def _identity_blockers(policy: dict[str, Any], evidence: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for field in policy.get("identity_fields") or []:
        value = evidence.get(field)
        if not isinstance(value, str) or not value.strip():
            blockers.append(f"identity_missing:{field}")
    for field in ("reqsys_sha", "worker_pool_sha"):
        value = str(evidence.get(field) or "").strip().lower()
        if value and not SHA40.fullmatch(value):
            blockers.append(f"identity_invalid:{field}")
    expected_contract = str(policy.get("expected_contract_version") or "")
    if evidence.get("contract_version") != expected_contract:
        blockers.append("contract_version_mismatch")
    return blockers


def evaluate(policy: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    if policy.get("schema_version") != "1.0.0":
        raise CutoverPolicyError("unsupported_policy_schema")
    if policy.get("decision_mode") != "fail_closed":
        raise CutoverPolicyError("policy_must_fail_closed")

    required = policy.get("required_evidence")
    if not isinstance(required, dict) or not required:
        raise CutoverPolicyError("required_evidence_missing")

    blockers = _identity_blockers(policy, evidence)
    for key, expected in required.items():
        if key not in evidence:
            blockers.append(f"evidence_missing:{key}")
            continue
        if evidence.get(key) is not expected:
            blockers.append(f"evidence_mismatch:{key}")

    blockers = sorted(set(blockers))
    status = READY if not blockers else BLOCKED
    rollback = policy.get("rollback") or {}
    legacy_present = evidence.get("legacy_reqsys_present") is True

    return {
        "schema_version": "1.0.0",
        "policy_name": policy.get("policy_name"),
        "status": status,
        "blockers": blockers,
        "legacy_removal_allowed": status == READY,
        "rollback_available": bool(
            rollback.get("available_while_legacy_present") is True and legacy_present
        ),
        "rollback_scope": rollback.get("allowed_fallback_scope"),
        "reqsys_sha": evidence.get("reqsys_sha"),
        "worker_pool_sha": evidence.get("worker_pool_sha"),
        "contract_version": evidence.get("contract_version"),
        "correlation_id": evidence.get("correlation_id"),
        "production_touched": False,
        "secrets_mutated": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Avaliar cutover/rollback do Worker Pool")
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        result = evaluate(_load_json(args.policy), _load_json(args.evidence))
    except (OSError, json.JSONDecodeError, CutoverPolicyError) as exc:
        result = {
            "schema_version": "1.0.0",
            "status": BLOCKED,
            "blockers": [f"policy_evaluation_error:{type(exc).__name__}"],
            "legacy_removal_allowed": False,
            "rollback_available": False,
            "production_touched": False,
            "secrets_mutated": False,
        }

    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result.get("status") == READY else 2


if __name__ == "__main__":
    raise SystemExit(main())
