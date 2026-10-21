"""One positive composition gate in the existing CI, not private-cloud evidence.

Uses the real Gateway, HTTPS API/SQLite, executor, Git and S3 transport. The S3
service is explicitly the existing in-memory fixture. No live task, endpoint,
credential or personal computer is contacted. Only test-owned processes stop.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import runpy
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.native_gateway_commands import GovernedCommand, NativeGatewayCommandRegistry
from app.native_one_shot import (
    AdapterDescriptor, AdapterResult, DurableArtifact, LocalWorkspace,
    NativeExecutorBlocked, ReceiptStore, TaskManifest,
)
from app.native_remote_readback import readback_remote_result
from app.native_remote_runtime import GatewayAuthority, HttpsWorkerRoute, RemoteNativeExecutor, PosixWorkerTokenFile
from app.s3_git_bundle_artifact import S3ArtifactConfig, S3GitBundleArtifactTransport

PINNED_RULES = "ddd09dc616181c0aaf46d33a194e9b00a6989332"
REPOSITORY = "ci-isolated/remote-composition"
SOURCE = "src/calculator.py"
BEFORE = "def add(a, b):\n    return a - b\n"
AFTER = "def add(a, b):\n    return a + b\n"
CORRELATION = "remote-composition-readback-20261020"


def git(directory, *args):
    result = subprocess.run(
        ["git", *args], cwd=directory, capture_output=True, text=True,
        check=True, timeout=20,
    )
    return result.stdout.strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def blocked(code, call):
    try:
        call()
    except NativeExecutorBlocked as exc:
        assert code is None or str(exc) == code, (code, str(exc))
        return str(exc)
    raise AssertionError("negative_control_missed")


class FixtureAdapter:
    descriptor = AdapterDescriptor(
        "ci.remote.patch.v1", (SOURCE,),
        ("ci.commit", "ci.fix", "ci.stage"), ("ci.validate",),
    )

    def __init__(self):
        self.calls = []

    def build(self, context):
        self.calls.append("build")
        assert (context.root / SOURCE).read_text() == BEFORE
        assert runpy.run_path(str(context.root / SOURCE))["add"](2, 3) == -1
        context.invoke("ci.fix")
        context.invoke("ci.stage")
        context.invoke("ci.commit")
        assert (context.root / SOURCE).read_text() == AFTER
        return AdapterResult(
            git(context.root, "rev-parse", "HEAD"), (SOURCE,),
            tuple(context.invoked), digest(context.root / SOURCE),
        )

    def validate(self, context, produced_sha):
        self.calls.append("validate")
        context.invoke("ci.validate")
        assert (context.root / SOURCE).read_text() == AFTER
        return AdapterResult(
            produced_sha, (SOURCE,), tuple(context.invoked),
            digest(context.root / SOURCE),
        )


def run(rules_scripts):
    expected = os.environ.get("EXPECTED_SHA", "")
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert os.environ.get("GITHUB_REPOSITORY") == "ericson-j-santos/engineering-worker-pool"
    assert re.fullmatch(r"[0-9a-f]{40}", expected)
    assert git(ROOT, "rev-parse", "HEAD") == expected
    assert git(rules_scripts.resolve().parent, "rev-parse", "HEAD") == PINNED_RULES
    sys.path.insert(0, str(rules_scripts.resolve()))
    import command_gateway as gateway
    import session_preflight as preflight
    assert Path(gateway.__file__).resolve() == (rules_scripts / "command_gateway.py").resolve()

    tls = fixture_module(ROOT / "tests/test_native_remote_tls.py", "remote_tls_fixture")
    # Set test-server configuration, not a mock or replacement of an attestor.
    tls.RULES = PINNED_RULES
    s3_fixture = fixture_module(ROOT / "tests/test_s3_git_bundle_artifact.py", "remote_s3_fixture")

    with tempfile.TemporaryDirectory(prefix="remote-composition-") as temporary:
        root = Path(temporary).resolve()
        source = root / "source"
        source.mkdir()
        git(source, "init", "-q")
        git(source, "config", "user.name", "Remote Composition Fixture")
        git(source, "config", "user.email", "remote-fixture@example.invalid")
        (source / "src").mkdir()
        (source / SOURCE).write_text(BEFORE)
        (source / "scripts").mkdir()
        (source / "scripts/ci_fix.py").write_text(
            "from pathlib import Path\n"
            "p=Path('src/calculator.py')\n"
            f"assert p.read_text() == {BEFORE!r}\n"
            f"p.write_text({AFTER!r})\n"
        )
        (source / "scripts/ci_validate.py").write_text(
            "import runpy\n"
            "f=runpy.run_path('src/calculator.py')['add']\n"
            "assert f(2,3)==5 and f(-5,7)==2 and f(0,0)==0\n"
        )
        git(source, "add", "--", SOURCE, "scripts/ci_fix.py", "scripts/ci_validate.py")
        git(source, "commit", "-q", "-m", "fixture: initial incorrect addition")
        base = git(source, "rev-parse", "HEAD")
        # This independent repository predates the result. It cannot obtain the
        # result from a branch created later by the Builder.
        receiver = root / "receiver"
        git(root, "clone", "-q", "--no-hardlinks", str(source), str(receiver))
        git(receiver, "remote", "remove", "origin")
        git(receiver, "checkout", "-q", "--detach", base)

        # Same explicitly disposable policy profile as the existing native E2E.
        # This does not claim to qualify the operational POSIX deployment profile.
        policy = {
            "version": 1, "rules_version": "1.6.10",
            "allowed_roots": [str(root)], "denied_roots": [str(root / "blocked")],
            "denied_segments": [".ssh"], "denied_names": [".env"],
            "allowed_executables": ["git", "python", "python3"],
            "blocked_executables": ["cmd", "powershell", "bash", "sh"],
            "deny_inline_code": True, "require_git_repo": True,
            "risk2_requires_clean_tree": True, "max_timeout_seconds": 120,
            "state_dir": str(root / "state"), "git_untracked_excludes": [],
            "require_session_bootstrap": True, "require_preflight_snapshot": True,
            "worktree_root": str(root), "worktree_prefix": "wt-remote-",
        }
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(policy))
        policy_digest = digest(policy_path)
        gateway_digest = digest(Path(gateway.__file__))
        adapter = FixtureAdapter()

        def session(repo, session_id, sha):
            with contextlib.redirect_stdout(io.StringIO()):
                snapshot = preflight.preflight(
                    repo, policy_path, session_id, CORRELATION, True,
                )
                workspace = Path(snapshot["reserved_worktree"])
                assert gateway.inspect(workspace, policy, CORRELATION, session_id) == 0
            assert snapshot["state_validated"] is True
            assert git(workspace, "rev-parse", "HEAD") == sha
            specs = {
                "ci.fix": GovernedCommand(
                    "ci.fix", "builder", ("python", "scripts/ci_fix.py"), 2,
                    script_sha256=digest(workspace / "scripts/ci_fix.py"),
                ),
                "ci.stage": GovernedCommand(
                    "ci.stage", "builder", ("git", "add", "--", SOURCE), 2,
                    allow_dirty=True,
                ),
                "ci.commit": GovernedCommand(
                    "ci.commit", "builder", ("git", "commit", "-m", "fix: fixture addition"),
                    2, allow_dirty=True, allow_head_change=True,
                ),
                "ci.validate": GovernedCommand(
                    "ci.validate", "validator", ("python", "scripts/ci_validate.py"), 1,
                    script_sha256=digest(workspace / "scripts/ci_validate.py"),
                ),
            }
            registry = NativeGatewayCommandRegistry(
                gateway=gateway, policy_path=policy_path, workspace=workspace,
                session_id=session_id, correlation_id=CORRELATION, expected_head=sha,
                trusted_gateway_sha256=gateway_digest,
                trusted_policy_sha256=policy_digest, specs=specs,
            )
            authority = GatewayAuthority(
                gateway, policy_path, session_id, CORRELATION, sha,
                gateway_digest, policy_digest,
            )
            return workspace, registry, authority, snapshot

        b_root, b_registry, b_authority, b_snapshot = session(source, "remote-builder", base)
        snapshots = {Path(preflight.snapshot_path(policy, "remote-builder")): None}
        for path in snapshots:
            snapshots[path] = path.read_bytes()
        receipts = ReceiptStore(root / "receipts")
        s3 = s3_fixture.FakeS3()
        s3_config = S3ArtifactConfig("durable-artifacts", "us-east-2")
        tls_root = root / "tls"
        tls_root.mkdir()
        with tls.tls_authority(tls_root) as (route, token, db, server, admin, env, ca):
            assert env["CODEX_WORKER_POOL_EXPECTED_RULES_SHA"] == PINNED_RULES

            def post(path, body, status=200):
                response = admin.post(path, json=body)
                assert response.status_code == status, (path, response.status_code)
                return response.json()

            def independent():
                with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
                    return connection.execute(
                        "SELECT task_id,state,lease_generation,produced_sha FROM tasks ORDER BY task_id"
                    ).fetchall()

            post("/v1/repositories", {
                "repository": REPOSITORY, "enabled": True, "max_in_flight": 1,
                "correlation_id": CORRELATION,
            })

            def register(worker, role, workspace, registry):
                post("/v1/workers", {
                    "worker_id": worker, "host": "github-hosted-disposable",
                    "role": role, "profile": "NORMAL", "rules_sha": PINNED_RULES,
                    "gateway_ok": registry.boundary.gateway_verified,
                    "state_validated": registry.boundary.state_validated,
                    "worktree_root": str(workspace), "correlation_id": CORRELATION,
                })

            register("remote-builder", "builder", b_root, b_registry)
            intent = {
                "repository": REPOSITORY, "issue_number": 1,
                "request_id": "remote-composition-" + os.environ["GITHUB_RUN_ID"],
                "base_sha": base, "fencing_required": True, "correlation_id": CORRELATION,
            }
            task_id = post("/v1/tasks", intent, 201)["task"]["task_id"]
            manifest = {
                "schema_version": 1, "task_id": task_id, "repository": REPOSITORY,
                "base_sha": base, "workspace_sha": base,
                "adapter_id": adapter.descriptor.adapter_id,
                "adapter_contract_sha256": adapter.descriptor.contract_sha256,
                "before_sha256": {SOURCE: digest(b_root / SOURCE)},
            }

            def runtime(workspace, registry, authority, role):
                return RemoteNativeExecutor(
                    route=route, token_provider=PosixWorkerTokenFile(token),
                    s3_config=s3_config, s3_client=s3,
                    workspace=LocalWorkspace(workspace), receipts=receipts,
                    adapters={adapter.descriptor.adapter_id: adapter},
                    command_registries={
                        adapter.descriptor.adapter_id: registry.for_role(adapter.descriptor, role),
                    },
                    authority=authority,
                    fenced_adapter_ids=frozenset({adapter.descriptor.adapter_id}),
                )

            builder = runtime(b_root, b_registry, b_authority, "builder")
            original = independent()
            bad = runtime(b_root, b_registry,
                          replace(b_authority, gateway_sha256="0" * 64), "builder")
            blocked("native_authority_changed", lambda: bad.run(
                manifest, role="builder", worker_id="remote-builder"))
            assert independent() == original and s3.put_calls == 0 and not adapter.calls
            s3.deny_put = True
            blocked("durable_artifact_preflight_failed", lambda: builder.run(
                manifest, role="builder", worker_id="remote-builder"))
            assert independent() == original and not s3.objects and not adapter.calls
            s3.deny_put = False
            with contextlib.redirect_stdout(io.StringIO()):
                built = builder.run(manifest, role="builder", worker_id="remote-builder")
            produced = built["produced_sha"]
            assert built["result"] == "NATIVE_ONE_SHOT_BUILT" and produced != base
            assert independent() == [(task_id, "validating", 1, produced)]
            puts = s3.put_calls
            artifact_objects = dict(s3.objects)
            blocked("native_workspace_drift", lambda: builder.run(
                manifest, role="builder", worker_id="remote-builder"))

            def b_read():
                return readback_remote_result(
                    builder, manifest, role="builder", worker_id="remote-builder",
                )

            for _ in range(3):
                assert b_read() == {**built, "replayed": True}
            assert s3.put_calls == puts and s3.objects == artifact_objects
            assert adapter.calls == ["build"] and independent() == [(task_id, "validating", 1, produced)]

            receipt_path = receipts.paths(task_id, "builder")[1]
            original_receipt = receipt_path.read_bytes()
            corrupted = json.loads(original_receipt)
            corrupted["manifest_sha256"] = "f" * 64
            try:
                receipt_path.write_text(json.dumps(corrupted))
                blocked("receipt_mismatch", b_read)
            finally:
                receipt_path.write_bytes(original_receipt)
            assert b_read()["replayed"] is True

            saved_receipt = receipt_path.with_suffix(".saved")
            receipt_path.rename(saved_receipt)
            try:
                blocked("readback_receipt_unavailable", b_read)
            finally:
                saved_receipt.rename(receipt_path)
            assert b_read()["replayed"] is True

            drift = b_root / "unexpected.txt"
            drift.write_text("test-only drift")
            try:
                blocked("native_workspace_drift", b_read)
            finally:
                drift.unlink()
            assert b_read()["replayed"] is True

            snapshot_path = next(iter(snapshots))
            expired = json.loads(snapshots[snapshot_path])
            expired["timestamp"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
            expired["snapshot_sha256"] = gateway.snapshot_sha256(expired)
            try:
                snapshot_path.write_text(json.dumps(expired))
                blocked("native_attestation_expired", b_read)
            finally:
                snapshot_path.write_bytes(snapshots[snapshot_path])
            assert b_read()["replayed"] is True

            artifact_key = max(s3.objects, key=lambda key: len(s3.objects[key]))
            artifact_bytes = s3.objects[artifact_key]
            try:
                s3.objects[artifact_key] = b"invalid-fixture-artifact"
                blocked(None, b_read)
            finally:
                s3.objects[artifact_key] = artifact_bytes
            assert b_read()["replayed"] is True
            assert s3.put_calls == puts and s3.objects == artifact_objects
            assert independent() == [(task_id, "validating", 1, produced)]
            assert adapter.calls == ["build"]
            assert receipt_path.read_bytes() == original_receipt
            assert snapshot_path.read_bytes() == snapshots[snapshot_path]

            # The recipient cannot already have the result in its independent DB.
            missing = subprocess.run(
                ["git", "cat-file", "-e", produced + "^{commit}"], cwd=receiver,
                capture_output=True, timeout=20,
            )
            assert missing.returncode != 0
            transport = S3GitBundleArtifactTransport(s3_config, s3)
            recovered = transport.materialize(
                task_id=task_id, manifest=TaskManifest.parse(manifest),
                workspace=LocalWorkspace(receiver),
                artifact=DurableArtifact.parse({key: built[key] for key in (
                    "artifact_kind", "artifact_ref", "artifact_sha256",
                )}),
            )
            assert recovered.produced_sha == produced and git(receiver, "remote") == ""
            v_root, v_registry, v_authority, v_snapshot = session(
                receiver, "remote-validator", produced,
            )
            assert v_root != b_root
            register("remote-validator", "validator", v_root, v_registry)
            validator = runtime(v_root, v_registry, v_authority, "validator")
            v_manifest = {
                **manifest, "workspace_sha": produced,
                "before_sha256": {SOURCE: digest(v_root / SOURCE)},
            }
            with contextlib.redirect_stdout(io.StringIO()):
                validated = validator.run(
                    v_manifest, role="validator", worker_id="remote-validator",
                )
            assert validated["result"] == "NATIVE_ONE_SHOT_VALIDATED"
            assert independent() == [(task_id, "completed", 2, produced)]
            final_puts = s3.put_calls
            for _ in range(3):
                assert readback_remote_result(
                    validator, v_manifest, role="validator", worker_id="remote-validator",
                ) == {**validated, "replayed": True}
            blocked("receipt_lease_stale", b_read)
            assert post("/v1/tasks", intent)["task"]["task_id"] == task_id
            final = admin.get(f"/v1/tasks/{task_id}").json()
            assert final["state"] == "completed" and final["produced_sha"] == produced
            assert independent() == [(task_id, "completed", 2, produced)]
            assert s3.put_calls == final_puts and s3.objects == artifact_objects
            assert adapter.calls == ["build", "validate"]
            assert git(source, "rev-parse", "HEAD") == base
            assert (source / SOURCE).read_text() == BEFORE
            assert not list(receipts.root.glob("*.lock"))
            assert server.poll() is None

        print(json.dumps({
            "result": "REMOTE_COMPOSITION_READBACK_PROVEN",
            "worker_pool_sha": expected, "rules_sha": PINNED_RULES,
            "run_id": os.environ["GITHUB_RUN_ID"],
            "real_gateway_commands": True, "real_tls_sqlite": True,
            "concrete_s3_transport": True, "s3_provider": "in-memory-fixture",
            "private_s3_verified": False, "product_e2e_verified": False,
            "whole_host_failover_verified": False,
            "independent_readback": True, "readback_no_effects_verified": True,
            "original_post_commit_admission_block_reproduced": True,
            "builder_calls": 1, "validator_calls": 1, "task_count": 1,
            "generations": [1, 2], "negative_controls": 9,
            "operational_posix_profile_verified": False,
        }, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules-scripts", type=Path, required=True)
    args = parser.parse_args()
    run(args.rules_scripts)
