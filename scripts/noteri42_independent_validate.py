#!/usr/bin/env python3
"""Noteri #42: independent, bounded Validator for the actual diagnostic repair.

Runs only in an already admitted independent Validator worktree with its own
lease/Session Launcher/Gateway. This script neither reads CAS credentials nor
certifies CAS, cost, host identity, model calls or READY_FOR_PR.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
BASE_SHA = "ce2be12647f27220a40d2ae39188d52f517f31df"
SOURCE = ".github/workflows/noteri-control-plane-probe.yml"
TESTS = (
    "tests/test_noteri_control_plane_fallback.py",
    "tests/test_noteri_rules219_readonly_admission.py",
    "tests/test_noteri_gateway_failure_readback.py",
)
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
TASK = re.compile(r"cwp-[0-9a-f]{24}\Z")
MAX_OUTPUT = 2_000_000


class Blocked(RuntimeError):
    pass


def require(value: bool, reason: str) -> None:
    if not value:
        raise Blocked(reason)


def git(root: Path, *args: str) -> bytes:
    try:
        outcome = subprocess.run(
            ["git", *args], cwd=root, check=False,
            capture_output=True, timeout=15,
            env={
                "PATH": os.environ.get("PATH", os.defpath),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "LC_ALL": "C",
            },
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Blocked("NOTERI42_VALIDATOR_GIT_UNAVAILABLE") from None
    require(outcome.returncode == 0 and len(outcome.stdout) <= MAX_OUTPUT,
            "NOTERI42_VALIDATOR_GIT_FAILED")
    return outcome.stdout


def verify_git(root: Path, *, base: str, produced: str) -> dict:
    """Real Git/bytes checks; does NOT assert model provenance or test success."""
    require(type(base) is str and base == BASE_SHA
            and type(produced) is str and SHA40.fullmatch(produced) is not None
            and base != produced, "NOTERI42_VALIDATOR_SHA_UNTRUSTED")
    require(isinstance(root, Path) and root.is_absolute()
            and root.is_dir() and not root.is_symlink()
            and root.resolve() == root, "NOTERI42_VALIDATOR_WORKTREE_UNTRUSTED")
    require(Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
            == root, "NOTERI42_VALIDATOR_GIT_ROOT_CHANGED")
    require(git(root, "rev-parse", "HEAD").decode().strip() == produced
            and git(root, "status", "--porcelain", "--untracked-files=all") == b"",
            "NOTERI42_VALIDATOR_CHECKOUT_DIRTY_OR_HEAD_CHANGED")
    parents = git(root, "rev-list", "--parents", "-n", "1", produced).decode().split()
    require(parents == [produced, base], "NOTERI42_VALIDATOR_PARENT_CHANGED")
    paths = [p.decode("utf-8") for p in git(
        root, "diff", "--no-renames", "--name-only", "-z", base, produced,
    ).split(b"\0") if p]
    require(paths == [SOURCE], "NOTERI42_VALIDATOR_SCOPE_CHANGED")
    original = git(root, "show", base + ":" + SOURCE)
    new = git(root, "show", produced + ":" + SOURCE)
    target = root / SOURCE
    require(target.is_file() and not target.is_symlink()
            and target.resolve().is_relative_to(root)
            and target.read_bytes() == new
            and original != new
            and 0 < len(new) <= 150_000,
            "NOTERI42_VALIDATOR_BYTES_CHANGED")
    # This repair must improve the formerly opaque Gateway failure without
    # emitting unbounded raw output or certifying a Builder from a Risk1 probe.
    require(b"NOTERI_219_GATEWAY_BLOCKED" in original
            and b"NOTERI_219_GATEWAY_POLICY_EXIT_" in new
            and b"NOTERI_219_GATEWAY_FAILURE_UNCLASSIFIED" in new
            and b"state_changed" in new and b"secrets_read" in new
            and b"ready_for_builder = $true" not in new
            and b"Write-Output $raw" not in new
            and b"Write-Host $raw" not in new,
            "NOTERI42_VALIDATOR_DIAGNOSTIC_REGRESSION")
    return {
        "base_sha": base, "produced_sha": produced,
        "source_sha256": hashlib.sha256(new).hexdigest(),
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "changed_paths": [SOURCE],
        "git_commit_verified": True,
        "tests_executed": False, "model_provenance_verified": False,
        "cas_verified": False, "ready_for_pr": False,
    }


def run_tests(root: Path) -> tuple[str, int]:
    """Fixed affected regression files, never shell or an issue-supplied path."""
    require(all((root / path).is_file() and not (root / path).is_symlink()
                for path in TESTS), "NOTERI42_VALIDATOR_TESTS_MISSING")
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
             *TESTS], cwd=root, check=False, capture_output=True,
            timeout=180,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                 "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise Blocked("NOTERI42_VALIDATOR_TEST_UNAVAILABLE") from None
    require(result.returncode == 0
            and len(result.stdout) <= 262144
            and len(result.stderr) <= 262144
            and b"failed" not in result.stdout.lower()
            and b"skipped" not in result.stdout.lower()
            and b"deselected" not in result.stdout.lower(),
            "NOTERI42_VALIDATOR_TEST_FAILED")
    match = re.search(rb"(\d+) passed in ", result.stdout)
    require(match is not None and int(match.group(1)) >= 10,
            "NOTERI42_VALIDATOR_TEST_COUNT_UNVERIFIED")
    return "NOTERI42_AFFECTED_TESTS_PASSED", int(match.group(1))


def main() -> int:
    require(len(sys.argv) == 1 and os.environ.get("NOTERI42_VALIDATOR_ROLE") == "validator"
            and os.environ.get("NOTERI42_SOURCE_MODE") == "model_builder"
            and TASK.fullmatch(os.environ.get("NOTERI42_TASK_ID", "")) is not None,
            "NOTERI42_VALIDATOR_CONTEXT_MISSING")
    root = Path.cwd().resolve()
    producer_worktree = os.environ.get("NOTERI42_BUILDER_WORKTREE", "")
    require(bool(producer_worktree)
            and Path(producer_worktree).is_absolute()
            and Path(producer_worktree).resolve() != root,
            "NOTERI42_VALIDATOR_NOT_INDEPENDENT")
    produced = os.environ.get("NOTERI42_PRODUCED_SHA", "")
    proof = verify_git(root, base=os.environ.get("NOTERI42_BASE_SHA", ""),
                       produced=produced)
    result, passed = run_tests(root)
    # This is intentionally not a READY_FOR_PR/transport receipt.
    require(verify_git(root, base=BASE_SHA, produced=produced) == proof,
            "NOTERI42_VALIDATOR_REPLAY_DRIFT")
    print(json.dumps({
        "result": result, "task_id": os.environ["NOTERI42_TASK_ID"],
        "base_sha": proof["base_sha"], "produced_sha": proof["produced_sha"],
        "source_sha256": proof["source_sha256"], "tests_passed": passed,
        "independent_validator": True, "model_provenance_verified": False,
        "cas_verified": False, "ready_for_pr": False,
        "state_changed": False, "secrets_read": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Blocked, OSError, UnicodeError, ValueError) as exc:
        reason = str(exc) if type(exc) is Blocked else "NOTERI42_VALIDATOR_UNKNOWN_BLOCK"
        print(json.dumps({
            "result": "NOTERI42_VALIDATOR_BLOCKED", "reason_code": reason,
            "ready_for_pr": False, "state_changed": False,
            "secrets_read": False,
        }, sort_keys=True))
        raise SystemExit(2) from None
