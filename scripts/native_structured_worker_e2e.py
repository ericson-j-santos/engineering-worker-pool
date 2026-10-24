"""Native Desktop proof: real source bug -> local Qwen -> isolated patch -> validator.

Entry point only under a previously validated Session Launcher + Command Gateway.
No Docker, user data, remote Git writes, arbitrary paths or model-pull operation.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.native_structured_ollama import NativeStructuredOllama
from app.structured_code_worker import (
    StructuredBlocked, git, need, repair, sha256, validate,
)
from scripts.structured_acceptance import INSTRUCTION, cases


SOURCE_FILE = "app/portfolio_bridge.py"
ALLOWED_SOURCE = "ericson-j-santos/engineering-worker-pool"
CORRELATION = re.compile(r"[a-z0-9][a-z0-9-]{5,100}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")


def require_source(source: Path, sha: str, worktree: Path) -> None:
    need(SHA.fullmatch(sha) is not None, "source_sha_invalid")
    need(source.is_dir() and not source.is_symlink(), "source_unavailable")
    need(source == ROOT and source != worktree, "source_path_mismatch")
    workspace = os.environ.get("GITHUB_WORKSPACE", "")
    need(bool(workspace), "github_workspace_missing")
    workspace_path = Path(workspace).resolve()
    need(source == (workspace_path / "external" / "engineering-worker-pool").resolve(),
         "source_not_authorized_checkout")
    need("chatgpt-workers" in str(worktree).lower(), "governed_worktree_required")
    need(git(source, "rev-parse", "HEAD") == sha, "source_sha_changed")
    need(git(source, "status", "--porcelain", "--untracked-files=all") == "",
         "source_tree_dirty")
    need((source / SOURCE_FILE).is_file(), "source_file_missing")


def execute(sha: str, correlation_id: str) -> dict:
    worktree = Path.cwd().resolve()
    require_source(ROOT.resolve(), sha, worktree)
    with tempfile.TemporaryDirectory(prefix="native-qwen7b-", dir=worktree) as name:
        sandbox = Path(name)
        checkout = sandbox / "builder"
        independent = sandbox / "validator"
        state = sandbox / "state"

        # A clone of the reviewed source, with NO remote and NO active hooks.
        git(sandbox, "clone", "--no-hardlinks", str(ROOT), str(checkout))
        git(checkout, "remote", "remove", "origin")
        git(checkout, "switch", "-c", "worker/native-qwen7b")
        need(git(checkout, "rev-parse", "HEAD") == sha, "builder_source_sha_changed")

        before = (checkout / SOURCE_FILE).read_bytes()
        task = {
            "task_id": correlation_id,
            "path": SOURCE_FILE,
            "function": "_property",
            "parameters": ["properties", "name"],
            "instruction": INSTRUCTION,
            "base_sha": sha,
            "before_sha256": sha256(before),
            "cases": cases(),
        }
        baseline = validate(before.decode("utf-8"), task)
        need(baseline["passed"] < baseline["total"] and baseline["total"] == 42,
             "real_baseline_not_failing")

        provider = NativeStructuredOllama()
        result = repair(checkout, state, task, provider)
        need(provider.process_verified is True and provider.model_digest.startswith(
             os.environ["NATIVE_MODEL_DIGEST_PREFIX"]), "local_model_not_verified")

        replay = repair(checkout, state, task, provider)
        need(replay.get("replayed") is True and replay["model_calls"] == 0
             and replay["produced_sha"] == result["produced_sha"],
             "replay_not_idempotent")

        # Separate Git checkout and validator subprocess, same task/SHA.
        git(sandbox, "clone", "--no-hardlinks", str(checkout), str(independent))
        git(independent, "checkout", "--detach", result["produced_sha"])
        after = (independent / SOURCE_FILE).read_bytes()
        need(sha256(after) == result["after_sha256"], "validator_content_changed")
        verification = validate(after.decode("utf-8"), task)
        need(verification["passed"] == verification["total"] == 42
             and verification["errors"] == 0
             and verification == result["validation"], "independent_validation_failed")
        need(git(independent, "rev-parse", "HEAD") == result["produced_sha"],
             "validator_revision_changed")
        need(git(independent, "diff", "--name-only", sha, result["produced_sha"]) == SOURCE_FILE,
             "validator_scope_invalid")
        need(sha256((state / "change.patch").read_bytes()) == result["patch_sha256"],
             "patch_integrity_invalid")
        regression = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/test_portfolio_bridge.py",
             "tests/test_portfolio_admission_revalidation.py"],
            cwd=independent, capture_output=True, text=True, timeout=60,
            check=False, env={"PATH": os.environ.get("PATH", os.defpath),
                              "PY_COLORS": "0", "PYTHONPATH": ""},
        )
        need(regression.returncode == 0 and len(regression.stdout) < 131072
             and re.search(r"\\b[1-9][0-9]* passed\\b", regression.stdout) is not None,
             "existing_bridge_regressions_failed")
        need(git(checkout, "remote") == "", "worker_remote_present")
        need(result["model_calls"] in (1, 2), "model_call_count_invalid")
        need(git(ROOT, "rev-parse", "HEAD") == sha
             and git(ROOT, "status", "--porcelain", "--untracked-files=all") == "",
             "source_changed_by_test")
        return {
            "result": "NATIVE_QWEN7B_CODE_E2E_PASSED",
            "source_repo": ALLOWED_SOURCE, "source_sha": sha,
            "correlation_id": correlation_id,
            "target": SOURCE_FILE, "source_is_real": True,
            "model": "qwen2.5-coder:7b",
            "model_digest": provider.model_digest,
            "baseline_passed": baseline["passed"],
            "cases_total": verification["total"],
            "validated_passed": verification["passed"],
            "model_calls": result["model_calls"],
            "independent_validator": True, "replay_no_model_calls": True,
            "existing_bridge_regressions": True,
            "produced_sha": result["produced_sha"],
            "patch_sha256": result["patch_sha256"],
            "no_remote_push": True, "no_merge": True,
            "no_model_pull": True, "docker_used": False,
            "host_os": os.name,
            "global_server_cloud_mode_verified": False,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--correlation-id", required=True)
    args = parser.parse_args(argv)
    if (CORRELATION.fullmatch(args.correlation_id) is None
            or SHA.fullmatch(args.expected_source_sha) is None):
        raise SystemExit("invalid_fixed_inputs")
    try:
        result = execute(args.expected_source_sha, args.correlation_id)
    except (StructuredBlocked, OSError, ValueError, subprocess.SubprocessError,
            KeyError) as error:
        reason = str(error) if isinstance(error, StructuredBlocked) else type(error).__name__
        result = {
            "result": "NATIVE_QWEN7B_CODE_E2E_BLOCKED",
            "reason_code": reason,
            "source_sha": args.expected_source_sha,
            "correlation_id": args.correlation_id,
        }
        print(json.dumps(result, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
