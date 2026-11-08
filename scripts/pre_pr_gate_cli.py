"""CLI for the generic pre-PR decision; it performs no remote writes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from app.pre_pr_gate import PrePRBlocked, evaluate_pre_pr

MAX_RECEIPT_BYTES = 262_144


def read_object(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise PrePRBlocked("receipt_file_invalid")
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_RECEIPT_BYTES:
        raise PrePRBlocked("receipt_size_invalid")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise PrePRBlocked("receipt_json_invalid") from None
    if type(value) is not dict:
        raise PrePRBlocked("receipt_json_invalid")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--builder", required=True, type=Path)
    parser.add_argument("--validator", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        decision = evaluate_pre_pr(
            read_object(args.request),
            read_object(args.builder),
            read_object(args.validator),
        )
    except PrePRBlocked as error:
        print(json.dumps({
            "schema_version": "pre-pr.decision.v1",
            "status": "BLOCKED",
            "reason": str(error),
            "draft_pr_authorized": False,
            "merge_authorized": False,
            "physical_execution_authorized": False,
        }, sort_keys=True))
        return 2
    except (OSError, ValueError):
        print(json.dumps({
            "schema_version": "pre-pr.decision.v1",
            "status": "BLOCKED",
            "reason": "receipt_read_unverified",
            "draft_pr_authorized": False,
            "merge_authorized": False,
            "physical_execution_authorized": False,
        }, sort_keys=True))
        return 2
    # The CLI reads locally supplied JSON only. Even matching receipts are not
    # authenticated evidence and MUST NOT return a successful readiness exit.
    print(json.dumps(decision, sort_keys=True))
    return 2


if __name__ == "__main__":
    sys.exit(main())
