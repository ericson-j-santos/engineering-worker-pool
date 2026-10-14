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

# A script invoked by pathname starts with scripts/ on sys.path. Resolve the
# versioned repository root explicitly before importing the Worker Pool package.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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



def exercise_isolated_native_chain(
    *, root: Path, source_repo: Path, builder_root: Path,
    builder_boundary, source_sha: str, policy: dict, policy_path: Path,
    gateway, preflight, authority: dict,
) -> dict[str, object]:
    """Exercise the real API/SQLite plus native Builder -> independent Validator.

    This is a synthetic, GitHub-hosted fixture; product tasks remain untouched.
    """
    import importlib
    import runpy
    from fastapi.testclient import TestClient
    from app.native_one_shot import (
        AdapterDescriptor, AdapterResult, LocalWorkspace,
        NativeOneShotExecutor, ReceiptStore,
    )
    from app.native_pool_http import LocalWorkerApi
    from app.native_gateway_commands import (
        GovernedCommand, NativeGatewayCommandRegistry,
    )

    source_path = "src/calculator.py"
    repaired_source = "def add(a, b):\n    return a + b\n"
    original_source = "def add(a, b):\n    return a - b\n"
    expected_worker_sha = os.environ.get("EXPECTED_SHA", "")
    if (os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_REPOSITORY")
            != "ericson-j-santos/engineering-worker-pool"
            or len(expected_worker_sha) != 40
            or git(ROOT, "rev-parse", "HEAD") != expected_worker_sha):
        raise AssertionError("native_chain_ci_context_invalid")

    def require(condition: bool, reason: str) -> None:
        if not condition:
            raise AssertionError(reason)

    def code_value(path: Path, left: int, right: int) -> int:
        # Deterministic source in a private synthetic CI fixture.
        return runpy.run_path(str(path))["add"](left, right)

    class FixtureAdapter:
        descriptor = AdapterDescriptor(
            "ci.native.patch.v1", (source_path,),
            ("ci.commit", "ci.fix", "ci.stage"), ("ci.validate",),
        )

        def __init__(self) -> None:
            self.calls: list[str] = []

        def build(self, context):
            self.calls.append("build")
            target = context.root / source_path
            require(target.read_text(encoding="utf-8") == original_source,
                    "native_chain_baseline_drift")
            require(code_value(target, 2, 3) == -1,
                    "native_chain_not_reproduced")
            # All write/commit commands go through the installed Gateway.
            context.invoke("ci.fix")
            context.invoke("ci.stage")
            context.invoke("ci.commit")
            produced = git(context.root, "rev-parse", "HEAD")
            require(target.read_text(encoding="utf-8") == repaired_source,
                    "native_chain_gateway_patch_not_applied")
            return AdapterResult(
                produced, (source_path,), tuple(context.invoked),
                hashlib.sha256(target.read_bytes()).hexdigest(),
            )

        def validate(self, context, produced_sha):
            self.calls.append("validate")
            context.invoke("ci.validate")
            target = context.root / source_path
            require(target.read_text(encoding="utf-8") == repaired_source
                    and code_value(target, 2, 3) == 5
                    and code_value(target, -5, 7) == 2,
                    "native_chain_functional_regression")
            return AdapterResult(
                produced_sha, (source_path,), tuple(context.invoked),
                hashlib.sha256(target.read_bytes()).hexdigest(),
            )

    token = root / "isolated-pool-token"
    token.write_text("disposable-native-pool-token-2026", encoding="ascii")
    token.chmod(0o600)
    settings = {
        "CODEX_WORKER_POOL_DB": str(root / "pool.db"),
        "CODEX_WORKER_POOL_API_TOKEN_FILE": str(token),
        "CODEX_WORKER_POOL_EXPECTED_RULES_SHA": PINNED_RULES,
        "CODEX_WORKER_POOL_HEARTBEAT_TTL_SECONDS": "600",
        "CODEX_WORKER_POOL_LEASE_SECONDS": "300",
        "CODEX_WORKER_POOL_MAX_ATTEMPTS": "2",
        "CODEX_WORKER_POOL_RECONCILE_INTERVAL_SECONDS": "10",
    }
    old_settings = {key: os.environ.get(key) for key in settings}
    try:
        os.environ.update(settings)
        sys.modules.pop("app.main", None)
        service = importlib.import_module("app.main")
        with TestClient(service.app, base_url="http://127.0.0.1:18097") as client:
            headers = {
                "Authorization": "Bearer disposable-native-pool-token-2026",
            }

            def post(path: str, data: dict, status: int = 200) -> dict:
                response = client.post(path, headers=headers, json=data)
                require(response.status_code == status,
                        "native_chain_post_contract")
                return response.json()

            require(client.get("/health").status_code == 200,
                    "native_chain_health")
            require(client.get("/v1/contract").status_code == 401,
                    "native_chain_negative_auth")
            lane = post("/v1/repositories", {
                "repository": "ci-isolated/native-fixture",
                "enabled": True, "max_in_flight": 1,
                "correlation_id": "ci-native-chain-20261014",
            })
            require(lane["enabled"] is True and lane["max_in_flight"] == 1,
                    "native_chain_lane")
            for worker_id, role in (
                ("native-fixture-builder", "builder"),
                ("native-fixture-validator", "validator"),
            ):
                response = post("/v1/workers", {
                    "worker_id": worker_id, "host": "github-hosted-isolated",
                    "role": role, "profile": "NORMAL", "capacity_score": 80,
                    "rules_sha": PINNED_RULES,
                    "gateway_ok": True, "state_validated": True,
                    "worktree_root": str(builder_root),
                    "correlation_id": "ci-native-chain-20261014",
                })
                require(response["worker_id"] == worker_id,
                        "native_chain_worker_identity")

            payload = {
                "repository": "ci-isolated/native-fixture", "issue_number": 1,
                "request_id": "native-chain-" + os.environ.get("GITHUB_RUN_ID", "fixture"),
                "base_sha": source_sha,
                "correlation_id": "ci-native-chain-20261014",
                "max_attempts": 2,
            }
            first = post("/v1/tasks", payload, 201)
            repeated_enqueue = post("/v1/tasks", payload, 200)
            task_id = first["task"]["task_id"]
            require(first["created"] is True
                    and repeated_enqueue["created"] is False
                    and repeated_enqueue["task"]["task_id"] == task_id,
                    "native_chain_enqueue_replay")
            adapter = FixtureAdapter()
            specs = {
                "ci.fix": GovernedCommand(
                    "ci.fix", "builder", ("python", "scripts/ci_fix.py"), 2,
                    script_sha256=hashlib.sha256(
                        (builder_root / "scripts/ci_fix.py").read_bytes(),
                    ).hexdigest(),
                ),
                "ci.stage": GovernedCommand(
                    "ci.stage", "builder",
                    ("git", "add", "--", source_path), 2, allow_dirty=True,
                ),
                "ci.commit": GovernedCommand(
                    "ci.commit", "builder",
                    ("git", "commit", "-m", "fix(ci-fixture): restore addition"),
                    2, allow_dirty=True, allow_head_change=True,
                ),
                "ci.validate": GovernedCommand(
                    "ci.validate", "validator",
                    ("python", "scripts/ci_validate.py"), 1,
                    script_sha256=hashlib.sha256(
                        (builder_root / "scripts/ci_validate.py").read_bytes(),
                    ).hexdigest(),
                ),
            }
            fixed_gateway_args = {
                "gateway": gateway, "policy_path": policy_path,
                "session_id": builder_boundary.session_id,
                "correlation_id": builder_boundary.correlation_id,
                "expected_head": source_sha,
                "trusted_gateway_sha256": authority["trusted_gateway_sha256"],
                "trusted_policy_sha256": authority["trusted_policy_sha256"],
                "specs": specs,
            }
            trusted = NativeGatewayCommandRegistry(
                **fixed_gateway_args, workspace=builder_root,
            )
            require(trusted.boundary == builder_boundary,
                    "native_chain_gateway_boundary_mismatch")
            registry = trusted.for_role(adapter.descriptor, "builder")
            receipt_store = ReceiptStore(root / "native-receipts")
            manifest = {
                "schema_version": 1,
                "task_id": task_id,
                "repository": "ci-isolated/native-fixture",
                "base_sha": source_sha,
                "workspace_sha": source_sha,
                "adapter_id": adapter.descriptor.adapter_id,
                "adapter_contract_sha256": adapter.descriptor.contract_sha256,
                "before_sha256": {
                    source_path: hashlib.sha256(
                        (builder_root / source_path).read_bytes()
                    ).hexdigest(),
                },
            }
            with LocalWorkerApi(
                port=18097, token_file=token, client=client,
            ) as api:
                builder = NativeOneShotExecutor(
                    api=api, workspace=LocalWorkspace(builder_root),
                    receipts=receipt_store,
                    adapters={adapter.descriptor.adapter_id: adapter},
                    command_registries={adapter.descriptor.adapter_id: registry},
                    boundary=builder_boundary,
                )
                try:
                    builder.run({**manifest, "workspace_sha": "f" * 40},
                                role="builder", worker_id="native-fixture-builder")
                except NativeExecutorBlocked as exc:
                    require(str(exc) == "boundary_manifest_drift",
                            "native_chain_negative_wrong_reason")
                else:
                    raise AssertionError("native_chain_missing_negative")
                require(api.get_task(task_id)["state"] == "queued",
                        "native_chain_negative_mutated_task")
                built = builder.run(
                    manifest, role="builder", worker_id="native-fixture-builder",
                )
                produced = built["produced_sha"]
                require(built["result"] == "NATIVE_ONE_SHOT_BUILT"
                        and produced != source_sha
                        and api.get_task(task_id)["state"] == "validating",
                        "native_chain_builder")
                replayed = builder.run(
                    manifest, role="builder", worker_id="native-fixture-builder",
                )
                require(replayed["replayed"] is True
                        and replayed["produced_sha"] == produced
                        and adapter.calls == ["build"],
                        "native_chain_builder_replay")

            git(source_repo, "branch", "ci-native-result", produced)
            validator_source = root / "validator-source"
            git(root, "clone", "--quiet", "--no-hardlinks",
                str(source_repo), str(validator_source))
            git(validator_source, "remote", "remove", "origin")
            git(validator_source, "checkout", "--detach", produced)
            with contextlib.redirect_stdout(io.StringIO()):
                v_snapshot = preflight.preflight(
                    validator_source, policy_path,
                    "native-fixture-validator", "native-gateway-proof-20261014", True,
                )
            validator_root = Path(v_snapshot["reserved_worktree"])
            gateway.enforce_session(
                policy, "native-fixture-validator", validator_root, 2,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                require(gateway.inspect(
                    validator_root, policy, "native-gateway-proof-20261014",
                    "native-fixture-validator",
                ) == 0, "native_chain_validator_inspect")
            v_boundary = attest_execution_boundary(
                **{**authority, "workspace": validator_root,
                   "session_id": "native-fixture-validator",
                   "expected_head": produced},
            )
            require(validator_root != builder_root
                    and git(validator_root, "remote") == ""
                    and git(validator_root, "diff", "--name-only",
                            "--no-renames", source_sha, produced) == source_path
                    and code_value(validator_root / source_path, 2, 3) == 5,
                    "native_chain_validator_isolation")
            validator_gateway = NativeGatewayCommandRegistry(
                **{**fixed_gateway_args, "session_id": "native-fixture-validator",
                   "expected_head": produced},
                workspace=validator_root,
            )
            require(validator_gateway.boundary == v_boundary,
                    "native_chain_validator_gateway_boundary")
            validator_registry = validator_gateway.for_role(
                adapter.descriptor, "validator",
            )
            v_manifest = {
                **manifest,
                "workspace_sha": produced,
                "before_sha256": {
                    source_path: hashlib.sha256(
                        (validator_root / source_path).read_bytes()
                    ).hexdigest(),
                },
            }
            with LocalWorkerApi(
                port=18097, token_file=token, client=client,
            ) as api:
                validator = NativeOneShotExecutor(
                    api=api, workspace=LocalWorkspace(validator_root),
                    receipts=receipt_store,
                    adapters={adapter.descriptor.adapter_id: adapter},
                    command_registries={
                        adapter.descriptor.adapter_id: validator_registry,
                    },
                    boundary=v_boundary,
                )
                validation = validator.run(
                    v_manifest, role="validator", worker_id="native-fixture-validator",
                )
                require(validation["result"] == "NATIVE_ONE_SHOT_VALIDATED"
                        and adapter.calls == ["build", "validate"],
                        "native_chain_validator")
                final = api.get_task(task_id)
                require(final["state"] == "completed"
                        and final["produced_sha"] == produced
                        and "lease_token" not in final,
                        "native_chain_terminal_readback")
                require(api.claim_exact(
                    "native-fixture-builder", task_id,
                    "ci-native-chain-20261014",
                )["claimed"] is False, "native_chain_claim_after_completion")
            independent = client.get(
                f"/v1/tasks/{task_id}", headers=headers,
            )
            require(independent.status_code == 200
                    and independent.json()["state"] == "completed"
                    and independent.json()["produced_sha"] == produced
                    and git(source_repo, "rev-parse", "HEAD") == source_sha,
                    "native_chain_final_readback")
    finally:
        sys.modules.pop("app.main", None)
        for key, previous in old_settings.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
    return {
        "native_chain_result": "NATIVE_CHAIN_ISOLATED_PASSED",
        "native_chain_asgi_http": True,
        "native_chain_sqlite": True,
        "native_chain_builder_validator": True,
        "native_chain_gateway_commands_executed": True,
        "native_chain_queue_replay": True,
        "native_chain_independent_readback": True,
        "native_chain_real_product_issue": False,
        "native_chain_external_pr_created": False,
        "native_chain_physical_host_touched": False,
    }


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
        (repo / "src").mkdir()
        (repo / "src" / "calculator.py").write_text(
            "def add(a, b):\n    return a - b\n", encoding="utf-8",
        )
        (repo / "scripts").mkdir()
        fix_script = (
            "from pathlib import Path\n"
            "p = Path('src/calculator.py')\n"
            "before = 'def add(a, b):\\n    return a - b\\n'\n"
            "after = 'def add(a, b):\\n    return a + b\\n'\n"
            "if p.read_text(encoding='utf-8') != before:\n"
            "    raise SystemExit(3)\n"
            "p.write_text(after, encoding='utf-8')\n"
        )
        validate_script = (
            "from pathlib import Path\n"
            "import runpy\n"
            "p = Path('src/calculator.py')\n"
            "fun = runpy.run_path(str(p))['add']\n"
            "assert fun(2, 3) == 5 and fun(-5, 7) == 2\n"
        )
        (repo / "scripts" / "ci_fix.py").write_text(
            fix_script, encoding="utf-8",
        )
        (repo / "scripts" / "ci_validate.py").write_text(
            validate_script, encoding="utf-8",
        )
        git(repo, "add", "src/calculator.py", "scripts/ci_fix.py",
            "scripts/ci_validate.py")
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
        integration = exercise_isolated_native_chain(
            root=root, source_repo=repo, builder_root=workspace,
            builder_boundary=verified, source_sha=expected_head,
            policy=policy, policy_path=policy_path, gateway=cg, preflight=sp,
            authority=args,
        )
        print(json.dumps({
            **integration,
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
