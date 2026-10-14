"""Real pinned Command Gateway session-readback canary without external effects.

Executed in GitHub-hosted CI only after immutable checkout of canonical rules.
It proves that bare booleans and residual/stale snapshots are insufficient.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.native_gateway_preflight import attest_execution_boundary
from app.native_one_shot import NativeExecutorBlocked

PINNED_RULES = "ddd09dc616181c0aaf46d33a194e9b00a6989332"
SESSION_ID = "native-gateway-proof-001"
CORRELATION = "native-gateway-proof-20261014"


def git(directory: Path, *args: str) -> str:
    run = subprocess.run(["git", *args], cwd=directory, capture_output=True,
                         text=True, check=True, timeout=20)
    return run.stdout.strip()


def expected_block(reason: str, **kwargs: object) -> None:
    try:
        attest_execution_boundary(**kwargs)
    except NativeExecutorBlocked as exc:
        if str(exc) != reason:
            raise AssertionError(f"expected {reason}, observed {str(exc)}") from None
    else:
        raise AssertionError(f"negative-control-missed:{reason}")


def run(rules_scripts: Path) -> None:
    rule_root = rules_scripts.resolve().parent
    if git(rule_root, "rev-parse", "HEAD") != PINNED_RULES:
        raise AssertionError("canonical-rules-sha-mismatch")
    sys.path.insert(0, str(rules_scripts.resolve()))
    import command_gateway as cg
    import session_preflight as sp
    if Path(cg.__file__).resolve() != (rules_scripts / "command_gateway.py").resolve():
        raise AssertionError("canonical-gateway-origin-mismatch")

    with tempfile.TemporaryDirectory(prefix="native-gateway-proof-") as temporary:
        root = Path(temporary).resolve()
        repo = root / "source"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "native-e2e@example.invalid")
        git(repo, "config", "user.name", "Native E2E")
        (repo / "source.txt").write_text("source\n", encoding="utf-8")
        git(repo, "add", "source.txt")
        git(repo, "commit", "-m", "native E2E source")
        expected_head = git(repo, "rev-parse", "HEAD")
        policy = {
            "version": 1, "rules_version": "1.6.10",
            "allowed_roots": [str(root)],
            "denied_roots": [str(root / "blocked")],
            "denied_segments": [".ssh"], "denied_names": [".env"],
            "allowed_executables": ["git", "python", "python3"],
            "blocked_executables": ["cmd", "powershell", "bash", "sh"],
            "deny_inline_code": True, "require_git_repo": True,
            "risk2_requires_clean_tree": True, "max_timeout_seconds": 120,
            "state_dir": str(root / "state"), "git_untracked_excludes": [],
            "require_session_bootstrap": True,
            "require_preflight_snapshot": True,
            "worktree_root": str(root), "worktree_prefix": "wt-native-",
        }
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            snapshot = sp.preflight(repo, policy_path, SESSION_ID, CORRELATION, True)
        workspace = Path(snapshot["reserved_worktree"])
        cg.enforce_session(policy, SESSION_ID, workspace, 2)
        with contextlib.redirect_stdout(io.StringIO()):
            assert cg.inspect(workspace, policy, CORRELATION, SESSION_ID) == 0
        digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        args = {
            "gateway": cg, "policy_path": policy_path, "workspace": workspace,
            "session_id": SESSION_ID, "correlation_id": CORRELATION,
            "expected_head": expected_head,
            "trusted_gateway_sha256": digest(Path(cg.__file__)),
            "trusted_policy_sha256": digest(policy_path),
        }
        verified = attest_execution_boundary(**args)
        assert verified.gateway_verified is True and verified.state_validated is True
        assert verified.expected_head == expected_head
        assert attest_execution_boundary(**args) == verified  # idempotent read.

        expected_block("native_identity_invalid", **{**args, "expected_head": "a" * 39})
        expected_block("native_session_not_attested", **{**args, "expected_head": "f" * 40})
        expected_block("native_session_not_attested", **{**args, "correlation_id": "native-wrong-correlation"})
        expected_block("native_authority_changed", **{**args, "trusted_gateway_sha256": "f" * 64})
        expected_block("native_gateway_verification_failed", **{**args, "workspace": repo})
        expected_block("native_window_invalid", **{**args, "max_age_seconds": 301})

        state_file = Path(sp.snapshot_path(policy, SESSION_ID))
        original = state_file.read_bytes()
        altered = json.loads(original)
        altered["timestamp"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        altered["snapshot_sha256"] = cg.snapshot_sha256(altered)
        state_file.write_text(json.dumps(altered), encoding="utf-8")
        expected_block("native_attestation_expired", **args)
        state_file.write_bytes(original)

        altered = json.loads(original)
        altered["gateway_sha256"] = "0" * 64
        altered["snapshot_sha256"] = cg.snapshot_sha256(altered)
        state_file.write_text(json.dumps(altered), encoding="utf-8")
        expected_block("native_session_not_attested", **args)
        state_file.write_bytes(original)

        (workspace / "drift.txt").write_text("drift", encoding="utf-8")
        expected_block("native_workspace_drift", **args)
        (workspace / "drift.txt").unlink()

        log = Path(cg.state_dir(policy)) / "events.jsonl"
        log_backup = log.read_bytes()
        log.write_bytes(b"")
        expected_block("native_gateway_event_missing", **args)
        log.write_bytes(log_backup)

        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({
                "timestamp": datetime.now(UTC).isoformat(),
                "session_id": SESSION_ID, "correlation_id": CORRELATION,
                "action": "run", "result": "blocked",
            }) + "\n")
        expected_block("native_gateway_inspection_unverified", **args)
        log.write_bytes(log_backup)

        policy_raw = policy_path.read_bytes()
        policy_path.write_bytes(policy_raw + b" ")
        expected_block("native_authority_changed", **args)
        policy_path.write_bytes(policy_raw)

        assert attest_execution_boundary(**args) == verified
        assert git(workspace, "status", "--porcelain") == ""
        assert git(workspace, "rev-parse", "HEAD") == expected_head
        print(json.dumps({
            "result": "NATIVE_GATEWAY_PREFLIGHT_E2E_PASSED",
            "rules_sha": PINNED_RULES,
            "source_sha": expected_head,
            "positive_control": True,
            "negative_controls": 10,
            "replay_idempotent": True,
            "runtime_mutated": False,
            "task_claim_performed": False,
        }, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules-scripts", type=Path, required=True)
    args = parser.parse_args()
    run(args.rules_scripts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
