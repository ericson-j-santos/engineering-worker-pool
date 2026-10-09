"""Real disposable CI E2E: queue -> local model -> Git patch -> validator.

Not a deployment, live TODO admission or a general-purpose coding agent.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.local_code_worker import (
    MODEL, LocalOllama, WorkerBlocked, atomic_json, digest, git, process, repair, require,
    validate_independently,
)

RULES_SHA = "6cd74b32a24810cae7213744b697564e00be8b25"
REPOSITORY = "ericson-j-santos/engineering-worker-pool"
ARTIFACT = Path("artifacts/local-code-e2e")
BASELINE = "def clamp(value, lower, upper):\n    return value\n"


def context() -> tuple[Path, str, str]:
    root = Path.cwd().resolve()
    sha = os.environ.get("EXPECTED_SHA", "")
    correlation = "local-code-" + os.environ.get("GITHUB_RUN_ID", "") + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "")
    require(os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
            and len(sha) == 40 and git(root, "rev-parse", "HEAD") == sha,
            "hermetic_ci_identity")
    return root, sha, correlation


def run_inner() -> None:
    root, sha, correlation = context()
    session = json.loads(Path(os.environ["LOCAL_WORKER_SESSION_RECEIPT"]).read_text())
    require(session.get("result") == "SESSION_LAUNCH_OK"
            and session.get("state_validated") is True
            and Path(session["target_path"]).resolve() == root
            and session.get("head") == sha, "governed_session_missing")
    artifact = root / ARTIFACT
    artifact.mkdir(parents=True, exist_ok=True)
    evidence = {"result": "LOCAL_CODE_E2E_BLOCKED", "expected_sha": sha,
                "rules_sha": RULES_SHA, "correlation_id": correlation,
                "environment": "github-hosted-hermetic",
                "physical_hosts_touched": False, "production_touched": False,
                "live_todo_used": False, "merge_performed": False, "push_performed": False}
    atomic_json(artifact / "evidence.json", evidence)
    with tempfile.TemporaryDirectory(prefix="coding-task-", dir=root) as folder:
        area = Path(folder)
        repo, state = area / "repo", area / "state"
        (repo / "src").mkdir(parents=True)
        (repo / "src/clamp.py").write_text(BASELINE)
        git(repo, "init", "-b", "worker/local-code-e2e")
        git(repo, "add", "src/clamp.py")
        git(repo, "-c", "user.name=E2E Fixture", "-c", "user.email=fixture@localhost",
            "commit", "-m", "test: intentionally failing numeric fixture")
        base_sha = git(repo, "rev-parse", "HEAD")
        token = secrets.token_urlsafe(32)
        token_file = area / "api-token"
        token_file.write_text(token)
        token_file.chmod(0o600)
        env = dict(os.environ)
        env.update(CODEX_WORKER_POOL_DB=str(area / "pool.db"),
                   CODEX_WORKER_POOL_API_TOKEN_FILE=str(token_file),
                   CODEX_WORKER_POOL_EXPECTED_RULES_SHA=RULES_SHA,
                   CODEX_WORKER_POOL_HEARTBEAT_TTL_SECONDS="600",
                   CODEX_WORKER_POOL_LEASE_SECONDS="300",
                   CODEX_WORKER_POOL_MAX_ATTEMPTS="1")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with (area / "server.log").open("w") as log:
            server = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                 "--port", str(port), "--no-access-log"], cwd=root, env=env,
                stdout=log, stderr=log,
            )
            try:
                url = f"http://127.0.0.1:{port}"
                with httpx.Client(base_url=url, headers={"Authorization": "Bearer " + token},
                                  timeout=10, trust_env=False, follow_redirects=False) as client:
                    deadline = time.monotonic() + 12
                    while True:
                        require(server.poll() is None, "pool_process_exited")
                        try:
                            if client.get("/health").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        require(time.monotonic() < deadline, "pool_start_timeout")
                        time.sleep(0.2)

                    def request(method, path, payload=None, status=200):
                        result = client.request(method, path, json=payload)
                        require(result.status_code == status, "pool_contract")
                        return result.json()

                    negative_auth = httpx.get(url + "/v1/snapshot", timeout=10, trust_env=False)
                    require(negative_auth.status_code == 401, "negative_auth_failed")
                    for identifier, role in (("ci-builder", "builder"), ("ci-validator", "validator")):
                        request("POST", "/v1/workers", {
                            "worker_id": identifier, "host": "github-hosted-hermetic",
                            "role": role, "gateway_ok": True, "state_validated": True,
                            "rules_sha": RULES_SHA, "worktree_root": str(root),
                            "correlation_id": correlation,
                        })
                    request("POST", "/v1/repositories", {
                        "repository": "local-fixture/numeric-repair", "enabled": True,
                        "max_in_flight": 1, "correlation_id": correlation,
                    })
                    payload = {"repository": "local-fixture/numeric-repair", "issue_number": 1,
                               "request_id": correlation, "base_sha": base_sha,
                               "correlation_id": correlation, "max_attempts": 1}
                    first = request("POST", "/v1/work", payload, status=201)
                    replay = request("POST", "/v1/work", payload)
                    work = first["work"]
                    require(first["created"] is True and replay["created"] is False
                            and replay["work"]["work_id"] == work["work_id"], "queue_replay")
                    task_id = work["task_id"]
                    claim = request("POST", "/v1/claims", {
                        "worker_id": "ci-builder", "correlation_id": correlation,
                        "lease_seconds": 300,
                    })
                    require(claim["claimed"] is True and claim["task"]["task_id"] == task_id,
                            "builder_claim")
                    builder = {"worker_id": "ci-builder", "lease_token": claim["lease"]["lease_token"],
                               "correlation_id": correlation}
                    request("POST", f"/v1/tasks/{task_id}/start", builder)
                    task = {
                        "task_id": task_id, "path": "src/clamp.py", "function": "clamp",
                        "parameters": ["value", "lower", "upper"],
                        "instruction": "Clamp value between lower and upper, inclusive. Assume lower <= upper.",
                        "base_sha": base_sha, "before_sha256": digest(BASELINE.encode()),
                        "cases": [{"args": [v, lo, hi], "expected": min(max(v, lo), hi)}
                                  for lo, hi in [(-3, 3), (0, 10), (2, 2)]
                                  for v in [-10, -3, 0, 2, 3, 10, 20]],
                    }
                    provider = LocalOllama()
                    result = repair(repo, state, task, provider)
                    require(provider.process_verified, "model_execution_not_proved")
                    replay_result = repair(repo, state, task, provider)
                    require(replay_result["replayed"] is True and replay_result["model_calls"] == 0
                            and replay_result["produced_sha"] == result["produced_sha"], "repair_replay")
                    produced = result["produced_sha"]
                    request("POST", f"/v1/tasks/{task_id}/validation",
                            {**builder, "produced_sha": produced})
                    vclaim = request("POST", "/v1/claims", {
                        "worker_id": "ci-validator", "correlation_id": correlation,
                        "lease_seconds": 300,
                    })
                    require(vclaim["claimed"] is True and vclaim["task"]["task_id"] == task_id,
                            "validator_claim")
                    # Separate checkout and process; the model does not choose or write tests.
                    validator_root = area / "validator"
                    process(["git", "clone", "--no-hardlinks", str(repo), str(validator_root)], area)
                    git(validator_root, "checkout", "--detach", produced)
                    require(git(validator_root, "rev-parse", "HEAD") == produced, "validator_sha")
                    validation_state = area / "validator-state"
                    validation_state.mkdir()
                    validate_independently(validator_root, task, validation_state)
                    require(digest((validator_root / task["path"]).read_bytes()) == result["after_sha256"],
                            "validator_readback")
                    validator = {"worker_id": "ci-validator",
                                 "lease_token": vclaim["lease"]["lease_token"],
                                 "correlation_id": correlation}
                    request("POST", f"/v1/tasks/{task_id}/complete", validator)
                    record = {"validation_run_id": os.environ["GITHUB_RUN_ID"],
                              "validation_sha": "f" * 40, "evidence_reference": "local-code-e2e",
                              "independent_readback": True, "positive_control": True,
                              "negative_control": True, "correlation_id": correlation}
                    request("POST", f"/v1/work/{work['work_id']}/evidence", record, status=409)
                    record["validation_sha"] = produced
                    request("POST", f"/v1/work/{work['work_id']}/evidence", record)
                    readback = request("GET", f"/v1/work/{work['work_id']}")
                    snapshot = request("GET", "/v1/snapshot")
                    require(readback["task"]["state"] == "completed"
                            and readback["task"]["produced_sha"] == produced
                            and readback["evidence"]["validation_sha"] == produced
                            and len(snapshot["tasks"]) == 1, "final_pool_readback")
                    require(git(root, "rev-parse", "HEAD") == sha and git(root, "diff", "--name-only") == "", "source_repo_changed")
                    evidence.update(result="LOCAL_CODE_E2E_PASSED", task_id=task_id,
                                    work_id=work["work_id"], model_digest=provider.model_digest,
                                    model=MODEL, local_process_verified=True,
                                    cloud_disabled=True, queue_replay=True, code_replay=True,
                                    independent_validator=True, negative_auth_status=401,
                                    negative_sha_status=409, worker_result=result,
                                    session_id=session["session_id"], state_validated=True)
                    atomic_json(artifact / "evidence.json", evidence)
                    atomic_json(artifact / "task.json", task)
                    (artifact / "before.py").write_text(BASELINE)
                    shutil.copyfile(repo / task["path"], artifact / "after.py")
                    shutil.copyfile(state / "change.patch", artifact / "change.patch")
                    print("LOCAL_CODE_E2E_PASSED", flush=True)
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)


def launch_session(root: Path, rules: Path, policy_path: Path, sha: str, correlation: str) -> str:
    """GitHub workspace sources require a verified remote ref, even when allowlisted."""
    branch = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME", "")
    require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch))
            and ".." not in branch and "//" not in branch
            and not branch.endswith(("/", ".", ".lock")), "source_ref_invalid")
    argv = [
        sys.executable, str(rules / "scripts/session_launcher.py"), "--policy", str(policy_path),
        "--repo", str(root), "--session-prefix", "worker-code-ci",
        "--correlation-id", correlation, "--expected-head", sha,
        "--sync-ref", "origin/" + branch,
    ]
    try:
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True,
                                timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise WorkerBlocked("bootstrap_process_unavailable") from None
    if result.returncode:
        # Only the canonical launcher's structured, already-redacted error is eligible.
        # Never relay arbitrary process output, environment or provider responses.
        try:
            failure = json.loads(result.stderr.strip())
            if failure.get("result") == "SESSION_LAUNCH_BLOCKED":
                code = failure.get("gateway_exit_code")
                if type(code) is int:
                    print("SESSION_LAUNCH_BLOCKED code=" + str(code), flush=True)
        except (ValueError, AttributeError):
            pass
        raise WorkerBlocked("bootstrap_process_failed")
    require(len(result.stdout) <= 65536, "bootstrap_output_size")
    return result.stdout.strip()


def run_outer() -> None:
    root, sha, correlation = context()
    rules = root / "_rules"
    require(git(rules, "rev-parse", "HEAD") == RULES_SHA, "rules_sha_changed")
    # Disposable test policy only: preserves all guards; changes only sandbox paths.
    sandbox = Path(tempfile.mkdtemp(prefix="worker-code-ci-", dir=os.environ["RUNNER_TEMP"]))
    policy = json.loads((rules / "config/command-gateway.policy.json").read_text())
    policy.update(allowed_roots=[str(root), str(sandbox), str(sandbox / "*")],
                  worktree_root=str(sandbox / "worktrees"), state_dir=str(sandbox / "gateway-state"))
    policy_path = sandbox / "policy.json"
    atomic_json(policy_path, policy)
    raw = launch_session(root, rules, policy_path, sha, correlation)
    try:
        session = json.loads(raw.splitlines()[-1])
    except (ValueError, IndexError):
        raise WorkerBlocked("session_result_invalid") from None
    require(session.get("result") == "SESSION_LAUNCH_OK" and session.get("state_validated") is True
            and session.get("head") == sha, "session_blocked")
    receipt = sandbox / "session.json"
    atomic_json(receipt, session)
    os.environ["LOCAL_WORKER_SESSION_RECEIPT"] = str(receipt)
    worktree = Path(session["target_path"])
    try:
        process([
            sys.executable, str(rules / "scripts/command_gateway.py"), "--policy", str(policy_path),
            "--correlation-id", correlation, "run", "--cwd", str(worktree),
            "--session-id", session["session_id"], "--risk", "2", "--timeout", "240",
            "--expected-head", sha, "--", "python", "scripts/local_worker_e2e.py", "--inner",
        ], root, timeout=255)
    except WorkerBlocked:
        failure = worktree / ARTIFACT / "failure.json"
        if failure.is_file():
            value = json.loads(failure.read_text())
            if value.get("expected_sha") == sha:
                destination = root / ARTIFACT
                destination.mkdir(parents=True, exist_ok=True)
                atomic_json(destination / "inner-failure.json", value)
                print("INNER_E2E_BLOCKED " + str(value.get("reason_code", "unknown")), flush=True)
        raise
    evidence_path = worktree / ARTIFACT / "evidence.json"
    evidence = json.loads(evidence_path.read_text())
    require(evidence["result"] == "LOCAL_CODE_E2E_PASSED" and evidence["expected_sha"] == sha
            and evidence["correlation_id"] == correlation and evidence["state_validated"] is True,
            "independent_e2e_readback")
    target = root / ARTIFACT
    target.mkdir(parents=True, exist_ok=True)
    for name in ("evidence.json", "task.json", "before.py", "after.py", "change.patch"):
        shutil.copyfile(worktree / ARTIFACT / name, target / name)
    atomic_json(target / "bootstrap.json", {
        "result": session["result"], "head": session["head"],
        "state_validated": session["state_validated"], "session_id": session["session_id"],
        "snapshot_sha256": session["snapshot_sha256"],
    })
    print("LOCAL_CODE_E2E_INDEPENDENTLY_VERIFIED", flush=True)


def main() -> int:
    try:
        if sys.argv[1:] == ["--inner"]:
            run_inner()
        else:
            require(not sys.argv[1:], "arguments_not_allowed")
            run_outer()
        return 0
    except Exception as error:
        # Keep failure visible without printing arbitrary provider/process content.
        reason = str(error) if isinstance(error, WorkerBlocked) else type(error).__name__
        root = Path.cwd() / ARTIFACT
        root.mkdir(parents=True, exist_ok=True)
        atomic_json(root / "failure.json", {"result": "LOCAL_CODE_E2E_BLOCKED",
                    "reason_code": reason, "expected_sha": os.environ.get("EXPECTED_SHA", "")})
        print("LOCAL_CODE_E2E_BLOCKED " + reason, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
