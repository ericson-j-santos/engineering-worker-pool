"""Repair a real repository function in a disposable governed checkout.

This validates the structured executor. It does not dispatch a live TODO,
publish the resulting patch, deploy a service or alter a physical host.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.local_code_worker import WorkerBlocked, atomic_json, process
from app.structured_code_worker import repair, validate, git, sha256, need, StructuredBlocked
from app.structured_ollama import MODEL, StructuredOllama
from local_worker_e2e import context, launch_session, RULES_SHA
from structured_acceptance import cases, INSTRUCTION

ARTIFACT = Path("artifacts/structured-code-e2e")
SOURCE_PATH = "app/portfolio_bridge.py"


def verify_evidence(folder: Path, sha: str, correlation: str) -> dict:
    data = json.loads((folder / "evidence.json").read_text())
    task = json.loads((folder / "task.json").read_text())
    before = (folder / "before.py").read_bytes()
    after = (folder / "after.py").read_bytes()
    patch = (folder / "change.patch").read_bytes()
    need(data.get("result") == "STRUCTURED_CODE_E2E_PASSED"
         and data.get("expected_sha") == sha and data.get("correlation_id") == correlation
         and data.get("source_repository") == "ericson-j-santos/engineering-worker-pool"
         and data.get("source_path") == SOURCE_PATH
         and data.get("source_sha") == task.get("base_sha") == sha
         and data.get("source_is_existing_repository_file") is True
         and data.get("live_todo_used") is False
         and data.get("push_performed") is False and data.get("merge_performed") is False
         and data.get("cloud_disabled") is True and data.get("local_process_verified") is True
         and data.get("independent_validator") is True and data.get("replayed") is True,
         "evidence_identity")
    result = data["worker_result"]
    need(result["before_sha256"] == task["before_sha256"] == sha256(before)
         and result["after_sha256"] == sha256(after)
         and result["patch_sha256"] == sha256(patch)
         and result["base_sha"] == sha and result["produced_sha"] != sha
         and result["model_calls"] == 1
         and result["baseline"]["passed"] < result["baseline"]["total"]
         and result["validation"]["passed"] == result["validation"]["total"]
         and result["validation"]["errors"] == 0, "evidence_content")
    need(validate(before.decode(), task) == result["baseline"]
         and validate(after.decode(), task) == result["validation"], "evidence_tests")
    return data


def inner() -> None:
    root, sha, original_correlation = context()
    correlation = "structured-" + original_correlation
    session = json.loads(Path(os.environ["LOCAL_WORKER_SESSION_RECEIPT"]).read_text())
    need(session.get("result") == "SESSION_LAUNCH_OK" and session.get("state_validated") is True
         and Path(session["target_path"]).resolve() == root and session.get("head") == sha,
         "governed_session")
    artifact = root / ARTIFACT
    artifact.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="structured-task-", dir=root) as temporary:
        area = Path(temporary)
        clone, state = area / "repo", area / "state"
        git(area, "clone", "--no-hardlinks", str(root), str(clone))
        git(clone, "remote", "remove", "origin")
        git(clone, "switch", "-c", "worker/structured-property")
        need(git(clone, "rev-parse", "HEAD") == sha, "source_sha")
        before = (clone / SOURCE_PATH).read_bytes()
        need(before == (root / SOURCE_PATH).read_bytes(), "source_bytes")
        task = {
            "task_id": correlation, "path": SOURCE_PATH, "function": "_property",
            "parameters": ["properties", "name"], "instruction": INSTRUCTION,
            "base_sha": sha, "before_sha256": sha256(before), "cases": cases(),
        }
        atomic_json(artifact / "task.json", task)
        (artifact / "before.py").write_bytes(before)
        provider = StructuredOllama()
        def propose(spec, source):
            reply = provider(spec, source)
            # Public-source proposal only; never contains runtime credentials.
            atomic_json(artifact / "proposal.json", {
                "expected_sha": sha, "correlation_id": correlation,
                "response_sha256": sha256(reply.encode()), "reply": reply,
            })
            return reply
        result = repair(clone, state, task, propose)
        need(provider.process_verified is True, "inference_not_proved")
        replay = repair(clone, state, task, provider)
        need(replay["replayed"] is True and replay["model_calls"] == 0
             and replay["produced_sha"] == result["produced_sha"], "replay")
        independent = area / "validator"
        git(area, "clone", "--no-hardlinks", str(clone), str(independent))
        git(independent, "checkout", "--detach", result["produced_sha"])
        after = (independent / SOURCE_PATH).read_bytes()
        need(sha256(after) == result["after_sha256"], "validator_sha")
        need(validate(after.decode(), task) == result["validation"], "independent_validation")
        # Existing integration tests import the corrected module in another process.
        regression = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/test_portfolio_bridge.py",
             "tests/test_portfolio_admission_revalidation.py"],
            cwd=independent, capture_output=True, text=True, timeout=35, check=False,
            env={"PATH": os.environ.get("PATH", os.defpath), "PY_COLORS": "0"},
        )
        need(regression.returncode == 0, "existing_bridge_regression")
        need(git(root, "rev-parse", "HEAD") == sha and git(root, "diff", "--name-only") == "",
             "original_repository_modified")
        evidence = {
            "result": "STRUCTURED_CODE_E2E_PASSED", "expected_sha": sha,
            "correlation_id": correlation, "rules_sha": RULES_SHA,
            "session_id": session["session_id"], "state_validated": True,
            "source_repository": "ericson-j-santos/engineering-worker-pool",
            "source_path": SOURCE_PATH, "source_sha": sha,
            "source_is_existing_repository_file": True,
            "live_todo_used": False, "physical_hosts_touched": False,
            "push_performed": False, "merge_performed": False,
            "cloud_disabled": True, "local_process_verified": True,
            "model": MODEL, "model_digest": provider.model_digest,
            "independent_validator": True, "replayed": True,
            "existing_bridge_regression": True, "worker_result": result,
        }
        (artifact / "after.py").write_bytes(after)
        shutil.copyfile(state / "change.patch", artifact / "change.patch")
        atomic_json(artifact / "evidence.json", evidence)
        verify_evidence(artifact, sha, correlation)


def outer() -> None:
    root, sha, original_correlation = context()
    correlation = "structured-" + original_correlation
    rules = root / "_rules"
    need(git(rules, "rev-parse", "HEAD") == RULES_SHA, "rules_sha")
    sandbox = Path(tempfile.mkdtemp(prefix="structured-code-ci-", dir=os.environ["RUNNER_TEMP"]))
    policy = json.loads((rules / "config/command-gateway.policy.json").read_text())
    policy.update(allowed_roots=[str(root), str(sandbox), str(sandbox / "*")],
                  worktree_root=str(sandbox / "worktrees"), state_dir=str(sandbox / "gateway-state"))
    policy_path = sandbox / "policy.json"
    atomic_json(policy_path, policy)
    raw = launch_session(root, rules, policy_path, sha, correlation)
    session = json.loads(raw.splitlines()[-1])
    need(session.get("result") == "SESSION_LAUNCH_OK" and session.get("state_validated") is True
         and session.get("head") == sha, "session")
    receipt = sandbox / "session.json"
    atomic_json(receipt, session)
    os.environ["LOCAL_WORKER_SESSION_RECEIPT"] = str(receipt)
    worktree = Path(session["target_path"])
    try:
        process([
            sys.executable, str(rules / "scripts/command_gateway.py"), "--policy", str(policy_path),
            "--correlation-id", correlation, "run", "--cwd", str(worktree),
            "--session-id", session["session_id"], "--risk", "2", "--timeout", "240",
            "--expected-head", sha, "--", "python", "scripts/structured_worker_e2e.py", "--inner",
        ], root, timeout=255)
    finally:
        target = root / ARTIFACT
        target.mkdir(parents=True, exist_ok=True)
        for name in ("evidence.json", "task.json", "before.py", "after.py", "change.patch",
                     "failure.json", "proposal.json"):
            source = worktree / ARTIFACT / name
            if source.is_file():
                destination = "inner-failure.json" if name == "failure.json" else name
                shutil.copyfile(source, target / destination)
        atomic_json(target / "bootstrap.json", {
            "result": session["result"], "head": session["head"],
            "state_validated": session["state_validated"], "session_id": session["session_id"],
            "snapshot_sha256": session["snapshot_sha256"],
        })
    verify_evidence(target, sha, correlation)
    print("STRUCTURED_CODE_E2E_INDEPENDENTLY_VERIFIED")


def main() -> int:
    try:
        if sys.argv[1:] == ["--inner"]:
            inner()
        else:
            need(not sys.argv[1:], "arguments")
            outer()
        return 0
    except Exception as error:
        reason = str(error) if isinstance(error, (StructuredBlocked, WorkerBlocked)) else type(error).__name__
        folder = Path.cwd() / ARTIFACT
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json(folder / "failure.json", {
            "result": "STRUCTURED_CODE_E2E_BLOCKED", "expected_sha": os.environ.get("EXPECTED_SHA", ""),
            "reason_code": reason,
        })
        print("STRUCTURED_CODE_E2E_BLOCKED " + reason)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
