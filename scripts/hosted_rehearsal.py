#!/usr/bin/env python3
"""Disposable cross-job checkpoint rehearsal; never a live pool controller.

Only the run entrypoint launches commands. It requires the exact GitHub-hosted
workspace, bootstraps a canonical Session Launcher, then uses Command Gateway.
The capsule contains a vacuumed, lease-free, synthetic database, not user data.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import zlib

from app.secure_file import (
    SecureFileError,
    is_link_or_reparse,
    open_regular_read,
    path_matches_fd,
    validate_regular_fd,
)

REPOSITORY = "ericson-j-santos/engineering-worker-pool"
RULES_SHA = "a430688c499077f5472841560f224c40766b0b78"
ROOT = Path(__file__).resolve().parents[1]
WORK_ROOT = Path("/workspace/.chatgpt-workers")
POLICY_RELATIVE = "config/command-gateway.policy.posix.json"
POLICY_SHA256 = "50e2da27bcca6c82ae57cabbb5d8f490108fb363265c5f30fd0dcf78d6828bc5"
# Independently read from MANIFEST.json at RULES_SHA; never derived from candidate bytes.
PINNED_RULES_FILES = {
    POLICY_RELATIVE: (2058, POLICY_SHA256),
    "scripts/command_gateway.py": (64252, "14f1389a5c2447e0ec3ae707fd468c224e0f9f492fd7541de65906918c8ac4d1"),
    "scripts/session_launcher.py": (32379, "076c8670fde811233918767ad3fda15584735d38a20be2720951e5516e77b257"),
    "scripts/session_preflight.py": (6957, "57774599e388c034b929686f3d72dbb872f9b97fb19879a6f880a908dbde25d6"),
    "scripts/session_bootstrap.py": (11018, "2148b5e8885b9b5a223007bf0733282199a919cae57e1622b4e107b3d6a83b36"),
    "scripts/runner_version_preflight.py": (12120, "4ec5f4af67d7b8e5b859d7093241c6bcfa2dee9d9e37ec73f593b9c40784e09f"),
    "scripts/host_health_check.py": (2450, "82a12ed90b8f922fb6a44677b3fcf450c9418ca133e9b45441f3fe798b797b3f"),
}
MAGIC = "worker-pool-hosted-rehearsal-v1"
MAX_RAW = 2 * 1024 * 1024
MAX_ENCODED = 128 * 1024
SHA40 = re.compile(r"[0-9a-f]{40}")
SHA64 = re.compile(r"[0-9a-f]{64}")
PUBLIC_INPUT = b"worker-pool disposable portable checkpoint v1"
TEST_REPOSITORY = "example/hosted-continuity-rehearsal"
EXIT_AFTER_CHECKPOINT = 73
CAPSULE_KEYS = {"schema", "source_sha", "rules_sha", "run_id", "run_attempt",
                "producer_host", "producer_runner", "task_id", "old_generation", "old_attempt",
                "artifact", "artifact_sha256", "database", "database_sha256"}
CORR = "hosted-continuity-rehearsal"


class RehearsalError(RuntimeError):
    pass


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise RehearsalError(reason)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True, allow_nan=False) + "\n").encode()


def no_duplicates(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def strict_json(raw: bytes) -> dict:
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise RehearsalError("invalid_json") from exc
    require(isinstance(result, dict), "json_object_required")
    require(canonical(result) == raw, "noncanonical_json")
    return result


def encode_capsule(value: dict) -> tuple[str, str]:
    raw = canonical(value)
    require(len(raw) <= MAX_RAW, "capsule_too_large")
    encoded = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    require(len(encoded) <= MAX_ENCODED, "encoded_capsule_too_large")
    return encoded, digest(raw)


def runner_identity(value: object) -> str:
    require(isinstance(value, str) and
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}", value) is not None,
            "runner_identity_invalid")
    return value


def decode_capsule(encoded: str, expected_digest: str, context: dict) -> dict:
    require(isinstance(encoded, str) and 0 < len(encoded) <= MAX_ENCODED,
            "encoded_capsule_size")
    require(isinstance(expected_digest, str) and
            SHA64.fullmatch(expected_digest) is not None, "digest_invalid")
    try:
        compressed = base64.b64decode(encoded, validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_RAW + 1)
        require(len(raw) <= MAX_RAW and decoder.eof and
                not decoder.unconsumed_tail and not decoder.unused_data,
                "compression_limit_or_trailing_data")
    except (ValueError, zlib.error) as exc:
        raise RehearsalError("capsule_encoding_invalid") from exc
    require(digest(raw) == expected_digest, "capsule_integrity_mismatch")
    value = strict_json(raw)
    require(set(value) == CAPSULE_KEYS and value["schema"] == MAGIC,
            "capsule_schema_invalid")
    for key in ("source_sha", "rules_sha", "run_id", "run_attempt"):
        require(type(value[key]) is str and value[key] == context[key],
                "capsule_context_mismatch")
    require(SHA40.fullmatch(value["source_sha"]) is not None and
            value["rules_sha"] == RULES_SHA, "source_identity_invalid")
    require(type(value["old_generation"]) is int and
            value["old_generation"] == 1, "source_generation_invalid")
    require(isinstance(value["producer_host"], str) and
            re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value["producer_host"]) is not None,
            "producer_identity_invalid")
    runner_identity(value["producer_runner"])
    require(isinstance(value["old_attempt"], str) and
            re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value["old_attempt"]) is not None,
            "source_attempt_invalid")
    require(isinstance(value["task_id"], str) and
            re.fullmatch(r"cwp-[0-9a-f]{24}", value["task_id"]) is not None,
            "task_identity_invalid")
    require(isinstance(value["artifact"], dict) and
            set(value["artifact"]) == {"stage", "result"} and
            value["artifact"]["stage"] == "one" and
            value["artifact"]["result"] == digest(PUBLIC_INPUT),
            "unexpected_artifact")
    require(value["artifact_sha256"] == digest(canonical(value["artifact"])),
            "artifact_integrity_mismatch")
    require(isinstance(value["database"], str), "database_type_invalid")
    try:
        db = base64.b64decode(value["database"], validate=True)
    except ValueError as exc:
        raise RehearsalError("database_encoding_invalid") from exc
    require(0 < len(db) <= MAX_RAW and db.startswith(b"SQLite format 3\x00"),
            "database_format_invalid")
    require(digest(db) == value["database_sha256"], "database_integrity_mismatch")
    return value


def physical_directory(path: Path) -> Path:
    """Read-only: reject aliases before using an Actions workspace or governance source."""
    require(path.is_absolute(), "physical_directory_required")
    try:
        for component in (path, *path.parents):
            require(component.is_dir() and not is_link_or_reparse(component),
                    "physical_directory_required")
        require(path.resolve(strict=True) == path, "physical_directory_required")
    except OSError as exc:
        raise RehearsalError("physical_directory_required") from exc
    return path


def read_regular_bytes(path: Path, limit: int) -> bytes:
    """Bounded reads only; no bootstrap-side mkdir, policy rewrite or subprocess."""
    physical_directory(path.parent)
    try:
        before = path.lstat()
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                "regular_governance_file_required")
        descriptor = open_regular_read(path)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            payload = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
            validate_regular_fd(handle.fileno())
            same_path = path_matches_fd(handle.fileno(), path)
    except (OSError, SecureFileError) as exc:
        raise RehearsalError("regular_governance_file_required") from exc
    identity = lambda value: (value.st_size, value.st_mtime_ns,
                              value.st_ctime_ns, value.st_nlink)
    require(identity(opened) == identity(after) and same_path
            and len(payload) <= limit, "governance_file_changed_or_oversized")
    return payload


def canonical_governance(source: Path) -> tuple[Path, Path]:
    """Verify the immutable checkout and import closure before launching any command."""
    rules_root = physical_directory(source / ".rehearsal-rules")
    head = read_regular_bytes(rules_root / ".git" / "HEAD", 128).decode("ascii").strip()
    require(head == RULES_SHA, "canonical_rules_sha_mismatch")
    manifest = json.loads(read_regular_bytes(rules_root / "MANIFEST.json", 1024 * 1024),
                          object_pairs_hook=no_duplicates)
    require(isinstance(manifest, dict) and manifest.get("version") == "1.6.12"
            and isinstance(manifest.get("files"), list), "canonical_manifest_invalid")
    index = {}
    for entry in manifest["files"]:
        require(isinstance(entry, dict) and isinstance(entry.get("path"), str)
                and entry["path"] not in index, "canonical_manifest_invalid")
        index[entry["path"]] = entry
    for relative, (size, expected) in PINNED_RULES_FILES.items():
        entry = index.get(relative, {})
        require(entry.get("size") == size and entry.get("sha256") == expected,
                "canonical_manifest_mismatch")
        payload = read_regular_bytes(rules_root / relative, size)
        require(len(payload) == size and digest(payload) == expected,
                "canonical_governance_bytes_mismatch")
    return rules_root / "scripts", rules_root / POLICY_RELATIVE


def validated_test_count(payload: dict) -> int:
    """An exit code alone cannot prove that discovery actually executed its controls."""
    require(payload.get("result") == "ok" and payload.get("command_exit_code") == 0,
            "rehearsal_contract_tests_failed")
    report = str(payload.get("stdout", "")) + "\n" + str(payload.get("stderr", ""))
    matches = re.findall(r"^Ran ([0-9]+) tests? in [0-9.]+s\s*$", report, re.MULTILINE)
    require(len(matches) == 1 and int(matches[0]) > 0
            and re.search(r"^OK\s*$", report, re.MULTILINE) is not None
            and re.search(r"^(?:OK|FAILED)\s*\(", report, re.MULTILINE) is None,
            "rehearsal_test_execution_unverified")
    return int(matches[0])


def context() -> dict:
    env = os.environ
    require(env.get("GITHUB_ACTIONS") == "true" and
            env.get("GITHUB_REPOSITORY") == REPOSITORY and
            env.get("RUNNER_ENVIRONMENT") == "github-hosted",
            "github_hosted_only")
    require(env.get("GITHUB_ACTOR") == "ericson-j-santos", "owner_required")
    require(env.get("GITHUB_EVENT_NAME") == "pull_request", "pr_event_required")
    require(env.get("GITHUB_JOB") in ("produce", "resume"), "rehearsal_job_required")
    result = {"source_sha": env.get("REHEARSAL_EXPECTED_SHA", ""),
              "rules_sha": RULES_SHA,
              "run_id": env.get("GITHUB_RUN_ID", ""),
              "run_attempt": env.get("GITHUB_RUN_ATTEMPT", "")}
    require(SHA40.fullmatch(result["source_sha"]) is not None, "sha_required")
    require(all(re.fullmatch(r"[1-9][0-9]{0,19}", result[k]) is not None
                for k in ("run_id", "run_attempt")), "run_identity_invalid")
    return result


def work_path(ctx: dict) -> Path:
    return ROOT / ".tmp" / ("rehearsal-" + ctx["run_id"] + "-" + ctx["run_attempt"])


def store_at(path: Path):
    # The existing public implementation is the system under test.
    from app.store import WorkerPoolStore
    return WorkerPoolStore(path, heartbeat_ttl_seconds=120,
                           default_lease_seconds=10, default_max_attempts=6,
                           progress_stall_seconds=300, expected_rules_sha=RULES_SHA)


def register(store, worker: str, role: str) -> None:
    store.register_worker(worker_id=worker, host=socket.gethostname(), role=role,
                          profile="NORMAL", gateway_ok=True, state_validated=True,
                          rules_sha=RULES_SHA, correlation_id=CORR + "-register-" + role)


def lease_args(lease) -> dict:
    return {key: getattr(lease, key)
            for key in ("lease_token", "lease_generation", "attempt_id")}


def artifact_ref(ctx: dict, sha: str) -> str:
    return "gha-rehearsal://" + ctx["run_id"] + "/" + ctx["run_attempt"] + "/" + sha


def checkpoint_writer(ctx: dict) -> None:
    root = work_path(ctx)
    store = store_at(root / "pool.db")
    store.enable_fenced_cutover(correlation_id=CORR + "-isolated",
                               legacy_claimers_drained=True,
                               shared_queue_exclusive=True)
    register(store, "rehearsal-producer", "builder")
    task, created = store.enqueue_task(
        repository=TEST_REPOSITORY, issue_number=1,
        request_id="rehearsal-" + ctx["run_id"] + "-" + ctx["run_attempt"],
        correlation_id=CORR + "-enqueue", base_sha=ctx["source_sha"],
        fencing_required=True)
    require(created is True, "unexpected_existing_task")
    claimed, lease = store.claim_task(
        worker_id="rehearsal-producer", role="builder", only_task_id=task["task_id"],
        fencing_required=True, correlation_id=CORR + "-claim-a")
    require(claimed is not None and lease is not None, "producer_claim_failed")
    store.start_task(task_id=task["task_id"], worker_id="rehearsal-producer",
                     correlation_id=CORR + "-start-a", **lease_args(lease))
    artifact = {"stage": "one", "result": digest(PUBLIC_INPUT)}
    raw = canonical(artifact)
    with (root / "stage-one.json").open("xb") as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    store.record_checkpoint(
        task_id=task["task_id"], worker_id="rehearsal-producer",
        checkpoint_id="producer-stage-one", sequence=1, stage="stage-one",
        next_action="resume-stage-two", artifact_kind="rehearsal-json",
        artifact_ref=artifact_ref(ctx, digest(raw)), artifact_sha256=digest(raw),
        correlation_id=CORR + "-checkpoint-a", **lease_args(lease))
    observed = store_at(root / "pool.db").get_recovery_context(task["task_id"])
    require(observed["latest_checkpoint"]["artifact_sha256"] == digest(raw),
            "producer_checkpoint_readback_failed")
    # The controller retains no token. This is test-only ownership metadata.
    metadata = {"task_id": task["task_id"], "old_generation": lease.lease_generation,
                "old_attempt": lease.attempt_id,
                "lease_expires_at": lease.lease_expires_at}
    with (root / "producer.json").open("xb") as out:
        out.write(canonical(metadata))
        out.flush()
        os.fsync(out.fileno())
    os._exit(EXIT_AFTER_CHECKPOINT)


def produce(ctx: dict) -> dict:
    producer_runner = runner_identity(os.environ.get("RUNNER_NAME"))
    root = work_path(ctx)
    root.mkdir(parents=True, exist_ok=False)
    result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "writer"],
                            shell=False, capture_output=True, timeout=30)
    require(result.returncode == EXIT_AFTER_CHECKPOINT, "checkpoint_writer_did_not_interrupt")
    metadata = strict_json((root / "producer.json").read_bytes())
    expires = datetime.fromisoformat(metadata["lease_expires_at"].replace("Z", "+00:00"))
    delay = (expires - datetime.now(timezone.utc)).total_seconds() + 0.15
    require(delay <= 11.0, "unexpected_lease_duration")
    if delay > 0:
        time.sleep(delay)  # One bounded real expiry, not a polling/retry loop.
    store = store_at(root / "pool.db")
    recovered = store.reconcile()
    require(recovered["expired_leases_recovered"] == 1, "expired_lease_not_recovered")
    task = store.get_task(metadata["task_id"])
    require(task["state"] == "queued" and task.get("lease_token") is None,
            "source_lease_not_released")
    # VACUUM removes free pages that could retain the expired test lease value.
    with closing(sqlite3.connect(root / "pool.db")) as db:
        require(db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1,
                "not_a_disposable_database")
        require(db.execute("SELECT repository FROM tasks").fetchone()[0] == TEST_REPOSITORY,
                "product_data_forbidden")
        require(db.execute("SELECT count(*) FROM tasks WHERE lease_token IS NOT NULL "
                           "OR leased_by IS NOT NULL").fetchone() == (0,),
                "active_lease_export_forbidden")
        db.execute("VACUUM INTO ?", (str(root / "checkpoint.db"),))
    raw_db = (root / "checkpoint.db").read_bytes()
    artifact = strict_json((root / "stage-one.json").read_bytes())
    capsule = dict(ctx, schema=MAGIC, producer_host=socket.gethostname(),
                   producer_runner=producer_runner, task_id=metadata["task_id"], old_generation=metadata["old_generation"],
                   old_attempt=metadata["old_attempt"], artifact=artifact,
                   artifact_sha256=digest(canonical(artifact)),
                   database=base64.b64encode(raw_db).decode(),
                   database_sha256=digest(raw_db))
    encoded, sha = encode_capsule(capsule)
    require(decode_capsule(encoded, sha, ctx) == capsule, "producer_capsule_readback_failed")
    (root / "transfer.json").write_bytes(canonical({"capsule": encoded, "digest": sha}))
    return dict(ctx, phase="produce", capsule_sha256=sha,
                process_interruption_verified=True, source_lease_released=True,
                capsule_bytes=len(encoded), producer_runner=producer_runner,
                producer_container_hostname=socket.gethostname(),
                product_data_used=False, live_failover_verified=False)


def resume(ctx: dict) -> dict:
    from app.store import ConflictError
    encoded = os.environ.get("REHEARSAL_CAPSULE", "")
    sha = os.environ.get("REHEARSAL_CAPSULE_SHA256", "")
    capsule = decode_capsule(encoded, sha, ctx)
    receiver_runner = runner_identity(os.environ.get("RUNNER_NAME"))
    require(capsule["producer_host"] != socket.gethostname() and
            capsule["producer_runner"] != receiver_runner, "distinct_executor_required")
    root = work_path(ctx)
    require(not root.exists(), "receiver_workspace_not_fresh")
    root.mkdir(parents=True)
    db_path = root / "pool.db"
    with db_path.open("xb") as output:
        output.write(base64.b64decode(capsule["database"], validate=True))
    with closing(sqlite3.connect(db_path)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        require(db.execute("PRAGMA integrity_check").fetchone() == ("ok",),
                "restored_database_integrity_failed")
        rows = db.execute("SELECT task_id,repository,lease_token,state FROM tasks").fetchall()
        require(rows == [(capsule["task_id"], TEST_REPOSITORY, None, "queued")],
                "restored_task_scope_invalid")
    store = store_at(db_path)
    tid = capsule["task_id"]
    before = store.get_recovery_context(tid)["latest_checkpoint"]
    require(before["artifact_sha256"] == capsule["artifact_sha256"] and
            before["artifact_ref"] == artifact_ref(ctx, capsule["artifact_sha256"]),
            "checkpoint_artifact_binding_failed")
    register(store, "rehearsal-resumer", "builder")
    task, lease = store.claim_task(worker_id="rehearsal-resumer", role="builder",
                                  only_task_id=tid, fencing_required=True,
                                  correlation_id=CORR + "-claim-b")
    require(task is not None and lease is not None and lease.lease_generation == 2,
            "receiver_generation_not_advanced")
    store.start_task(task_id=tid, worker_id="rehearsal-resumer",
                     correlation_id=CORR + "-start-b", **lease_args(lease))
    checkpoint = dict(task_id=tid, worker_id="rehearsal-resumer",
                      checkpoint_id="resumed-stage-two", sequence=1,
                      stage="stage-two", next_action="independent-validation",
                      artifact_kind="rehearsal-json",
                      artifact_ref=artifact_ref(ctx, capsule["artifact_sha256"]),
                      artifact_sha256=capsule["artifact_sha256"],
                      correlation_id=CORR + "-checkpoint-b")
    state_before = store.get_task(tid)
    stale = dict(lease_args(lease), lease_generation=capsule["old_generation"],
                 attempt_id=capsule["old_attempt"])
    rejected = False
    try:
        # Valid current worker/token, only the old fence differs.
        store.record_checkpoint(**checkpoint, **stale)
    except ConflictError:
        rejected = True
    require(rejected and store.get_task(tid) == state_before and
            store.get_recovery_context(tid)["latest_checkpoint"] == before,
            "stale_generation_was_not_side_effect_free")
    current, first_created = store.record_checkpoint(**checkpoint, **lease_args(lease))
    replay, replay_created = store.record_checkpoint(**checkpoint, **lease_args(lease))
    require(first_created is True and replay_created is False and replay == current,
            "checkpoint_replay_not_idempotent")
    result = {"stage": "two", "result": digest(
        capsule["artifact"]["result"].encode() + b":resumed")}
    with (root / "stage-two.json").open("xb") as output:
        output.write(canonical(result))
    store.submit_for_validation(task_id=tid, worker_id="rehearsal-resumer",
                                produced_sha=ctx["source_sha"],
                                correlation_id=CORR + "-handoff", **lease_args(lease))
    register(store, "rehearsal-validator", "validator")
    _, validation_lease = store.claim_task(
        worker_id="rehearsal-validator", role="validator", only_task_id=tid,
        fencing_required=True, correlation_id=CORR + "-validator")
    require(validation_lease is not None, "validator_claim_failed")
    require(strict_json((root / "stage-two.json").read_bytes()) ==
            {"stage": "two", "result": digest(digest(PUBLIC_INPUT).encode() + b":resumed")},
            "independent_result_mismatch")
    final = store.complete_task(task_id=tid, worker_id="rehearsal-validator",
                               correlation_id=CORR + "-complete", **lease_args(validation_lease))
    independently_read = store_at(db_path).get_task(tid)
    require(final["state"] == independently_read["state"] == "completed",
            "terminal_readback_failed")
    repeated, created = store.enqueue_task(
        repository=TEST_REPOSITORY, issue_number=1,
        request_id="rehearsal-" + ctx["run_id"] + "-" + ctx["run_attempt"],
        correlation_id=CORR + "-replay", base_sha=ctx["source_sha"], fencing_required=True)
    require(created is False and repeated["task_id"] == tid, "task_replay_duplicated")
    return dict(ctx, phase="resume", capsule_sha256=sha,
                producer_runner=capsule["producer_runner"], receiver_runner=receiver_runner,
                producer_container_hostname=capsule["producer_host"],
                receiver_container_hostname=socket.gethostname(),
                checkpoint_resume_verified=True, stale_generation_rejected=True,
                checkpoint_replay_verified=True, task_replay_verified=True,
                final_state=independently_read["state"], product_data_used=False,
                shared_filesystem_used=False, live_failover_verified=False,
                s3_backend_verified=False, evidence_scope="serial-hosted-snapshot-rehearsal")


def validate_launch(payload: dict, ctx: dict) -> Path:
    require(payload.get("result") == "SESSION_LAUNCH_OK" and
            payload.get("state_validated") is True and payload.get("head") == ctx["source_sha"],
            "session_not_verified")
    require(payload.get("base_sync") == "isolated_session_source" and
            payload.get("source_repo_root") == str(ROOT), "isolated_source_required")
    require(isinstance(payload.get("snapshot_sha256"), str) and
            SHA64.fullmatch(payload["snapshot_sha256"]) is not None,
            "snapshot_digest_required")
    require(isinstance(payload.get("session_id"), str) and
            re.fullmatch(r"[A-Za-z0-9._-]{3,64}", payload["session_id"]) is not None,
            "session_identity_required")
    target = Path(str(payload.get("target_path", "")))
    require(target.is_absolute() and target.parent == WORK_ROOT and
            target.name.startswith("wt-chat-") and not target.is_symlink(),
            "session_workspace_invalid")
    return target


def execute_json(command: list[str], *, timeout: int, cwd: Path) -> dict:
    result = subprocess.run(command, cwd=cwd, shell=False, capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode != 0:
        details = {"result": "GOVERNED_PHASE_FAILED", "exit_code": result.returncode}
        # Launcher failures are structured on stderr; Gateway receipts use stdout.
        # Preserve only bounded diagnostic fields, never entire subprocess streams.
        for stream in (result.stdout, result.stderr):
            try:
                payload = json.loads(stream.strip().splitlines()[-1])
                if not isinstance(payload, dict):
                    continue
                for key in ("result", "phase", "reason"):
                    value = payload.get(key)
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{1,100}", value):
                        details["gateway_" + key] = value
                if (payload.get("action") == "session_launcher"
                        and payload.get("result") == "SESSION_LAUNCH_BLOCKED"
                        and payload.get("gateway_exit_code") == result.returncode):
                    error = payload.get("error")
                    if isinstance(error, str):
                        # The canonical Launcher has already applied cg.redact.
                        details["launcher_error"] = re.sub(r"[\x00-\x1f\x7f]", " ", error)[:300]
                nested = json.loads(str(payload.get("stdout", "")).strip().splitlines()[-1])
                reason = nested.get("reason")
                if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9_]{1,100}", reason):
                    details["inner_reason"] = reason
            except (ValueError, TypeError, AttributeError, IndexError):
                continue
        print(json.dumps(details, sort_keys=True), flush=True)
        raise RehearsalError("governed_command_failed")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    require(bool(lines), "governed_receipt_missing")
    try:
        payload = json.loads(lines[-1])
    except ValueError as exc:
        raise RehearsalError("governed_receipt_invalid") from exc
    require(isinstance(payload, dict), "governed_receipt_invalid")
    return payload


def run_governed(phase: str) -> None:
    ctx = context()
    require(os.name == "posix" and sys.dont_write_bytecode,
            "posix_bytecode_disabled_required")
    require(os.environ.get("REQSYS_WORKSPACE_ROOT") == "/workspace"
            and os.environ.get("REQSYS_GATEWAY_STATE_DIR") ==
            "/workspace/.reqsys-command-gateway-posix", "canonical_path_environment_required")
    source_root = physical_directory(Path(os.environ.get("GITHUB_WORKSPACE", "")))
    require(ROOT == source_root, "actions_workspace_mismatch")
    require(os.environ.get("GIT_CONFIG_COUNT") == "1"
            and os.environ.get("GIT_CONFIG_KEY_0") == "safe.directory"
            and os.environ.get("GIT_CONFIG_VALUE_0") == str(source_root),
            "git_source_trust_binding_required")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = event.get("pull_request", {})
    require(pr.get("head", {}).get("repo", {}).get("full_name") == REPOSITORY and
            pr.get("head", {}).get("sha") == ctx["source_sha"] and
            pr.get("user", {}).get("login") == "ericson-j-santos" and
            event.get("repository", {}).get("private") is False,
            "trusted_owner_pr_required")
    branch = pr.get("head", {}).get("ref", "")
    require(isinstance(branch, str) and
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch) is not None and
            ".." not in branch, "branch_invalid")
    rules, policy = canonical_governance(source_root)
    print("REHEARSAL_SOURCE_DIAGNOSTIC " + canonical({
        "source_sha": ctx["source_sha"],
        "source_owner_matches_process": source_root.stat().st_uid == os.geteuid(),
        "gitdir_owner_matches_process": (source_root / ".git").stat().st_uid == os.geteuid(),
    }).decode().strip(), flush=True)
    launch = execute_json(
        [sys.executable, "-B", str(rules / "session_launcher.py"), "--policy", str(policy),
         "--repo", str(ROOT), "--session-prefix", "rehearsal-" + phase,
         "--correlation-id", CORR + "-" + ctx["run_id"] + "-" + phase,
         "--expected-head", ctx["source_sha"], "--sync-ref", "origin/" + branch],
        timeout=120, cwd=ROOT)
    target = validate_launch(launch, ctx)
    gateway = [sys.executable, "-B", str(rules / "command_gateway.py"),
               "--policy", str(policy), "--correlation-id",
               CORR + "-" + ctx["run_id"] + "-" + phase]
    inspect = execute_json(gateway + ["inspect", "--cwd", str(target), "--session-id",
                                     launch["session_id"]], timeout=30, cwd=ROOT)
    require(inspect.get("result") == "ok" and
            inspect.get("before", {}).get("head") == ctx["source_sha"] and
            inspect.get("before", {}).get("status_count") == 0, "gateway_inspection_failed")
    prefix = gateway + ["run", "--cwd", str(target), "--session-id", launch["session_id"],
                        "--risk", "1", "--timeout", "90", "--expected-head", ctx["source_sha"], "--"]
    tests = execute_json(prefix + [sys.executable, "-B", "-m", "unittest", "discover",
                                   "-s", "tests", "-p", "test_hosted_rehearsal.py"],
                         timeout=110, cwd=ROOT)
    test_count = validated_test_count(tests)
    proof = execute_json(prefix + [sys.executable, "-B", "scripts/hosted_rehearsal.py", phase],
                         timeout=110, cwd=ROOT)
    require(proof.get("result") == "ok" and proof.get("command_exit_code") == 0 and
            proof.get("state_changed") is False, "rehearsal_gateway_failed")
    receipt = strict_json((target / ".tmp" /
        ("rehearsal-" + ctx["run_id"] + "-" + ctx["run_attempt"]) / "receipt.json").read_bytes())
    require(all(receipt.get(k) == v for k, v in ctx.items()), "receipt_context_mismatch")
    if phase == "produce":
        transfer = strict_json((target / ".tmp" /
            ("rehearsal-" + ctx["run_id"] + "-" + ctx["run_attempt"]) / "transfer.json").read_bytes())
        decode_capsule(transfer["capsule"], transfer["digest"], ctx)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write("capsule=" + transfer["capsule"] + "\n")
            output.write("capsule_sha256=" + transfer["digest"] + "\n")
    receipt.update(session_result=launch["result"], session_id=launch["session_id"],
                   snapshot_sha256=launch["snapshot_sha256"], gateway_verified=True,
                   governance_profile="canonical_posix", policy_sha256=POLICY_SHA256,
                   contract_tests_executed=test_count)
    print("REHEARSAL_EVIDENCE " + canonical(receipt).decode().strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["run-produce", "run-resume", "produce", "resume", "writer"])
    args = parser.parse_args()
    try:
        if args.phase.startswith("run-"):
            run_governed(args.phase.removeprefix("run-"))
        else:
            # Internal phases cannot run outside a real hosted job/worktree.
            ctx = context()
            require(ROOT.parent == WORK_ROOT and ROOT.name.startswith("wt-chat-"),
                    "governed_worktree_required")
            if args.phase == "writer":
                checkpoint_writer(ctx)
            receipt = produce(ctx) if args.phase == "produce" else resume(ctx)
            (work_path(ctx) / "receipt.json").write_bytes(canonical(receipt))
            print(canonical(receipt).decode().strip())
        return 0
    except (RehearsalError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        # Do not emit exception text, capsule, paths or subprocess streams.
        reason = str(exc) if isinstance(exc, RehearsalError) else type(exc).__name__
        if re.fullmatch(r"[A-Za-z0-9_]{1,100}", reason) is None:
            reason = "rehearsal_error"
        print(json.dumps({"result": "HOSTED_REHEARSAL_BLOCKED", "phase": args.phase,
                          "reason": reason, "live_failover_verified": False}))
        return 2


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
