"""Live read-only GitHub source attestation for Noteri #42 from hosted CI.

Uses GitHub Actions' ephemeral read token, passed only through a private file.
No model/CAS publication, host access, PR, merge, or READY_FOR_PR decision.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import tempfile

from app.native_one_shot import NativeExecutorBlocked
from app.pre_pr_gate import validate_request
from app.prepr_github_readback import (
    BRANCH, ISSUE, REPOSITORY, OfficialNoteriReadback,
)

REQUEST_PATH = Path("requests/noteri-42-pre-pr.json")
EXPECTED_PATHS = tuple(sorted((
    ".github/workflows/noteri-control-plane-probe.yml",
    "scripts/noteri_control_plane_watchdog_uac_launcher.py",
    "scripts/noteri_qualification_receipt.py",
    "scripts/noteri_watchdog_readonly_inventory.py",
    "tests/test_noteri_qualification_confirmation.py",
    "tests/test_noteri_qualification_receipt.py",
    "tests/test_noteri_watchdog_readonly_inventory.py",
)))


def _block(reason: str) -> None:
    raise NativeExecutorBlocked(reason)


def _load_request() -> dict:
    if REQUEST_PATH.is_symlink() or not REQUEST_PATH.is_file():
        _block("prepr_source_request_unavailable")
    raw = REQUEST_PATH.read_bytes()
    if not 0 < len(raw) <= 4096:
        _block("prepr_source_request_invalid")
    def no_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _block("prepr_source_request_duplicate")
            value[key] = item
        return value
    try:
        request = json.loads(raw, object_pairs_hook=no_duplicates)
    except (UnicodeError, ValueError):
        _block("prepr_source_request_invalid")
    checked = validate_request(request)
    if (checked["repository"] != REPOSITORY
            or checked["issue_number"] != ISSUE
            or checked["todo_id"] != "TODO-54"
            or checked["source_branch"] != BRANCH):
        _block("prepr_source_target_not_allowlisted")
    return checked


def run_live() -> dict:
    checked = _load_request()
    token = os.environ.get("GH_TOKEN", "")
    if not 16 <= len(token) <= 4096 or not re.fullmatch(r"[!-~]{16,4096}", token):
        _block("prepr_source_token_missing")
    # Private filesystem creation is required by the existing secure-file gate.
    # Never print the token, and never persist it in Actions artifacts or logs.
    with tempfile.TemporaryDirectory(prefix="prepr-github-source-") as folder:
        file_path = Path(folder) / "token"
        fd = os.open(str(file_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            raw_token = token.encode("ascii")
            if os.write(fd, raw_token) != len(raw_token):
                _block("prepr_source_token_write_incomplete")
        finally:
            os.close(fd)
        with OfficialNoteriReadback(
            token_file=file_path, base_sha=checked["base_sha"],
            source_sha=checked["head_sha"],
        ) as reader:
            observed = reader(REPOSITORY, ISSUE)
    if (observed["repository"] != checked["repository"]
            or observed["issue_number"] != checked["issue_number"]
            or observed["issue_state"] != "open"
            or observed["main_sha"] != checked["base_sha"]
            or observed["branch_head_sha"] != checked["head_sha"]
            or observed["base_is_ancestor"] is not True
            or tuple(observed["changed_paths"]) != EXPECTED_PATHS):
        _block("prepr_source_snapshot_not_admitted")
    return {
        "schema_version": "prepr.github-source.v1",
        "status": "GITHUB_SOURCE_ATTESTED_ONLY",
        "repository": REPOSITORY, "issue_number": ISSUE,
        "todo_id": checked["todo_id"],
        "base_sha": checked["base_sha"],
        "source_sha": checked["head_sha"],
        "changed_paths": list(EXPECTED_PATHS),
        "ready_for_pr": False, "model_executed": False,
        "cas_materialized": False, "host_ready": False,
    }


def main() -> int:
    try:
        result = run_live()
    except NativeExecutorBlocked as error:
        print(json.dumps({
            "schema_version": "prepr.github-source.v1",
            "status": "BLOCKED", "reason": str(error),
            "ready_for_pr": False,
        }, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
