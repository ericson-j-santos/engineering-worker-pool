#!/usr/bin/env python3
"""Native DEV continuity against a qualified PostgreSQL authority and real S3.

Invoked only through a validated Gateway session. This is an infrastructure
fixture, not a product adapter, a second task queue, or a VM destruction test.
No database, workspace, receipt, credential, or artifact bytes cross job outputs.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib
import json
import os
import re
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
REPOSITORY = "ericson-j-santos/engineering-worker-pool"
OWNER = "ericson-j-santos"
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_DOCUMENT = 64 * 1024
LEASE_SECONDS = 45
DEADLINE_SECONDS = 240
BRANCH = "feat/hosted-cold-checkpoint-resume-20261020"


def worker_id(identity: Mapping[str, str], role: str) -> str:
    require(role in {"builder-a", "builder-b", "validator-b"}, "worker_role_invalid")
    return identity["run_key"] + "-" + role


class HarnessBlocked(RuntimeError):
    pass


def require(condition: Any, code: str) -> None:
    if not condition:
        raise HarnessBlocked(code)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def document(raw: str, expected: str | None = None) -> dict[str, Any]:
    require(isinstance(raw, str) and 0 < len(raw.encode()) <= MAX_DOCUMENT,
            "document_size")
    if expected is not None:
        require(isinstance(expected, str) and SHA256.fullmatch(expected)
                and digest(raw.encode()) == expected, "document_integrity")
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "document_duplicate_key")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               HarnessBlocked("document_constant")))
    except (ValueError, TypeError):
        raise HarnessBlocked("document_format") from None
    require(type(value) is dict and canonical(value).decode() == raw,
            "document_not_canonical")
    return value


def protected_json(path: Path, value: Mapping[str, Any]) -> None:
    require(not path.exists() and not path.is_symlink(), "receipt_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(canonical(dict(value)).decode())
        stream.flush()
        os.fsync(stream.fileno())


def read_private_json(path: Path) -> dict[str, Any]:
    require(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "posix_required")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    and info.st_uid == os.getuid() and not info.st_mode & 0o077,
                    "private_document_permissions")
            raw = stream.read(MAX_DOCUMENT + 1)
        return document(raw.decode("utf-8"))
    except (OSError, UnicodeError):
        raise HarnessBlocked("private_document_unavailable") from None


def git(root: Path, *args: str, allowed: tuple[int, ...] = (0,)) -> str:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Native Continuity Fixture",
        "GIT_AUTHOR_EMAIL": "continuity@example.invalid",
        "GIT_COMMITTER_NAME": "Native Continuity Fixture",
        "GIT_COMMITTER_EMAIL": "continuity@example.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    })
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull,
             "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
             "-c", "credential.helper=", "-C", str(root), *args],
            env=environment, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        raise HarnessBlocked("git_unavailable") from None
    require(result.returncode in allowed, "git_rejected")
    return result.stdout.strip()


def context() -> dict[str, str]:
    env = os.environ
    require(os.name == "posix" and sys.platform.startswith("linux")
            and Path("/proc").is_dir(), "native_linux_required")
    require(env.get("GITHUB_ACTIONS") == "true"
            and env.get("GITHUB_REPOSITORY") == REPOSITORY
            and env.get("GITHUB_REPOSITORY_OWNER") == OWNER
            and env.get("GITHUB_ACTOR") == OWNER,
            "hosted_owner_context_required")
    require(env.get("GITHUB_EVENT_NAME") in {"push", "workflow_dispatch"}
            and env.get("GITHUB_REF") == "refs/heads/" + BRANCH,
            "hosted_event_refused")
    sha, rules_sha = env.get("EXPECTED_SHA", ""), env.get("EXPECTED_RULES_SHA", "")
    require(SHA40.fullmatch(sha) and SHA40.fullmatch(rules_sha), "source_identity_invalid")
    require(env.get("GITHUB_SHA") == sha
            and git(ROOT, "rev-parse", "HEAD") == sha
            and not git(ROOT, "status", "--porcelain", "--untracked-files=all",
                        "--", ".", ":(exclude).tmp/**"), "source_identity_drift")
    run, attempt, runner = (env.get(key, "") for key in
                            ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "RUNNER_NAME"))
    require(re.fullmatch(r"[1-9][0-9]{2,21}", run)
            and re.fullmatch(r"[1-9][0-9]{0,3}", attempt)
            and 0 < len(runner) <= 200, "runner_identity_invalid")
    require(env.get("CODEX_WORKER_POOL_BACKEND") == "postgres"
            and not env.get("CODEX_WORKER_POOL_DB"), "postgres_only")
    authority_id = env.get("CODEX_WORKER_POOL_AUTHORITY_ID", "")
    try:
        require(str(uuid.UUID(authority_id)) == authority_id, "authority_identity_invalid")
    except ValueError:
        raise HarnessBlocked("authority_identity_invalid") from None
    require(env.get("CODEX_WORKER_POOL_DB_SCHEMA")
            and env.get("CODEX_WORKER_POOL_DATABASE_URL_FILE")
            and (env.get("CODEX_WORKER_POOL_FENCED_CUTOVER_ATTESTED") or "0") == "0",
            "qualified_authority_required")
    return {"run_key": "native-" + run + "-" + attempt, "run": run,
            "attempt": attempt, "source_sha": sha, "rules_sha": rules_sha,
            "runner": runner, "authority_id": authority_id}


def authority():
    from app.postgres_authority import PostgresAuthority
    path = os.environ.get("CODEX_WORKER_POOL_DATABASE_URL_FILE", "")
    require(bool(path), "qualified_database_reference_required")
    return PostgresAuthority.from_file(
        Path(path), schema=os.environ["CODEX_WORKER_POOL_DB_SCHEMA"],
        authority_id=os.environ["CODEX_WORKER_POOL_AUTHORITY_ID"],
    )


def pool_store(database):
    from app.postgres_authority import PostgresWorkerPoolStore
    return PostgresWorkerPoolStore(
        database, heartbeat_ttl_seconds=300, default_lease_seconds=LEASE_SECONDS,
        default_max_attempts=5, progress_stall_seconds=300,
        expected_rules_sha=os.environ["EXPECTED_RULES_SHA"],
    )


def source_fixture(path: Path) -> str:
    from app.native_runtime import SYNTHETIC_FILES
    require(not path.exists() and not path.is_symlink(), "fixture_source_exists")
    path.mkdir(parents=True, mode=0o700)
    for name, value in sorted(SYNTHETIC_FILES.items()):
        target = path / name
        require(target.resolve().is_relative_to(path.resolve()), "fixture_path_invalid")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value.encode("utf-8"))
        target.chmod(0o644)
    git(path, "init", "--quiet", "--template=", "--object-format=sha1",
        "--initial-branch=fixture")
    git(path, "add", "--", *sorted(SYNTHETIC_FILES))
    git(path, "commit", "--quiet", "-m", "Deterministic native continuity fixture")
    require(not git(path, "remote") and not git(path, "status", "--porcelain"),
            "fixture_source_dirty")
    return git(path, "rev-parse", "HEAD")


def read_run(database, identity: Mapping[str, str]) -> dict[str, Any]:
    with database.read_connection() as db:
        row = db.execute("SELECT * FROM native_continuity_runs WHERE run_key=?",
                         (identity["run_key"],)).fetchone()
    require(row is not None, "prepared_run_missing")
    row = dict(row)
    require(all(row[key] == identity[key] for key in
                ("source_sha", "rules_sha", "authority_id")), "run_identity_drift")
    original = document(row["manifest_json"], row["manifest_sha256"])
    from app.native_one_shot import TaskManifest
    from app.native_runtime import synthetic_manifest
    parsed = TaskManifest.parse(original)
    require(parsed.task_id == row["task_id"] and parsed.base_sha == row["base_sha"]
            and original == synthetic_manifest(
                task_id=row["task_id"], base_sha=row["base_sha"],
                workspace_sha=row["base_sha"], role="builder"),
            "persisted_manifest_drift")
    return row


PHASE_TRANSITIONS = frozenset({
    ("definition_persisted", "prepared"), ("prepared", "a_started"),
    ("a_started", "a_checkpoint_confirmed"),
    ("a_checkpoint_confirmed", "a_tree_terminated"),
    ("a_tree_terminated", "b_started"), ("b_started", "b_finished"),
    ("b_finished", "verified"),
})


def transition(database, identity: Mapping[str, str], old: str, new: str,
               values: Mapping[str, Any] | None = None) -> None:
    require((old, new) in PHASE_TRANSITIONS, "phase_transition_invalid")
    allowed = {"consumer_runner", "artifact_json", "produced_sha",
               "stale_lease_json", "process_evidence_json", "final_receipt_json"}
    values = dict(values or {})
    require(set(values) <= allowed, "phase_fields_invalid")
    keys = sorted(values)
    assignments = ", ".join(["phase=?"] + [key + "=?" for key in keys])
    with database.transaction() as db:
        changed = db.execute(
            "UPDATE native_continuity_runs SET " + assignments +
            " WHERE run_key=? AND source_sha=? AND authority_id=? AND phase=?",
            (new, *(values[key] for key in keys), identity["run_key"],
             identity["source_sha"], identity["authority_id"], old),
        ).rowcount
        require(changed == 1, "phase_transition_rejected")


def lease_fields(task: Mapping[str, Any], identity: Mapping[str, str]) -> dict[str, Any]:
    result = {"worker_id": task.get("leased_by"),
              "lease_token": task.get("lease_token"),
              "lease_generation": task.get("lease_generation"),
              "attempt_id": task.get("attempt_id")}
    require(result["worker_id"] in {worker_id(identity, value) for value in
                                    ("builder-a", "builder-b", "validator-b")}
            and type(result["lease_token"]) is str
            and 16 <= len(result["lease_token"]) <= 128
            and type(result["lease_generation"]) is int
            and result["lease_generation"] >= 1
            and type(result["attempt_id"]) is str
            and 1 <= len(result["attempt_id"]) <= 128, "lease_readback_invalid")
    return result


def sink_apply(store, task_id: str, lease: Mapping[str, Any],
               payload_sha256: str) -> bool:
    require(SHA256.fullmatch(payload_sha256), "sink_payload_invalid")
    with store._tx() as db:
        store._lease(db, task_id, lease["worker_id"], lease["lease_token"],
                     ("running",), lease_generation=lease["lease_generation"],
                     attempt_id=lease["attempt_id"])
        old = db.execute(
            "SELECT * FROM native_continuity_effects WHERE task_id=? AND effect_id=?",
            (task_id, "publish-native-result"),
        ).fetchone()
        if old is not None:
            require(old["payload_sha256"] == payload_sha256
                    and old["applied_generation"] == lease["lease_generation"]
                    and old["applied_attempt"] == lease["attempt_id"]
                    and old["applications"] == 1, "sink_replay_divergent")
            return False
        db.execute(
            "INSERT INTO native_continuity_effects "
            "(task_id,effect_id,payload_sha256,applied_generation,applied_attempt,applications)"
            " VALUES(?,?,?,?,?,?)",
            (task_id, "publish-native-result", payload_sha256,
             lease["lease_generation"], lease["attempt_id"], 1),
        )
    return True


def load_launcher(rules_root: Path, identity: Mapping[str, str]):
    rules_root = rules_root.resolve()
    require(rules_root.is_dir()
            and git(rules_root, "rev-parse", "HEAD") == identity["rules_sha"]
            and not git(rules_root, "status", "--porcelain", "--untracked-files=all",
                        "--", ".", ":(exclude).tmp/**"), "rules_checkout_drift")
    scripts = rules_root / "scripts"
    policy = rules_root / "config" / "command-gateway.policy.posix.json"
    # HEAD and the entire tracked working tree are checked before importing.
    # No temporary policy and no truth-valued attestation substitute are used.
    for path in (scripts / "session_launcher.py", scripts / "command_gateway.py",
                 policy):
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and not path.is_symlink()
                and path.resolve().is_relative_to(rules_root), "rules_file_invalid")
        relative = path.relative_to(rules_root).as_posix()
        require(git(rules_root, "hash-object", "--no-filters", str(path))
                == git(rules_root, "rev-parse", identity["rules_sha"] + ":" + relative),
                "rules_file_bytes_drift")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(scripts))
    launcher = importlib.import_module("session_launcher")
    require(Path(launcher.__file__).resolve() == scripts / "session_launcher.py"
            and Path(launcher.cg.__file__).resolve() == scripts / "command_gateway.py",
            "launcher_import_origin")
    return launcher, policy, {
        "trusted_launcher_sha256": digest((scripts / "session_launcher.py").read_bytes()),
        "trusted_gateway_sha256": digest((scripts / "command_gateway.py").read_bytes()),
        "trusted_policy_sha256": digest(policy.read_bytes()),
        "trusted_rules_sha": identity["rules_sha"],
    }


def prepare(root: Path, identity: Mapping[str, str]) -> dict[str, Any]:
    from app.native_runtime import SYNTHETIC_REPOSITORY, synthetic_manifest
    from app.store import identity as task_identity, task_id as canonical_task_id
    database = authority()
    store = pool_store(database)
    require(store.fenced_cutover_status()["enabled"], "fenced_authority_not_prepared")
    base = source_fixture(root / "prepared-base")
    task_id = canonical_task_id(task_identity(SYNTHETIC_REPOSITORY, 1, identity["run_key"]))
    original = synthetic_manifest(task_id=task_id, base_sha=base,
                                  workspace_sha=base, role="builder")
    raw = canonical(original).decode()
    # Setup/migration and column-scoped grants are performed separately by the
    # operator. Runtime only inserts the immutable definition and updates phase.
    with database.transaction() as db:
        require(db.execute(
            "SELECT run_key FROM native_continuity_runs WHERE run_key=?",
            (identity["run_key"],)).fetchone() is None, "run_already_prepared")
        db.execute(
            "INSERT INTO native_continuity_runs "
            "(run_key,source_sha,rules_sha,authority_id,task_id,base_sha,"
            "manifest_json,manifest_sha256,phase,producer_runner)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (identity["run_key"], identity["source_sha"], identity["rules_sha"],
             identity["authority_id"], task_id, base, raw, digest(raw.encode()),
             "definition_persisted", identity["runner"]),
        )
    # The original definition exists durably before the task becomes claimable.
    prepared = read_run(authority(), identity)
    require(prepared["phase"] == "definition_persisted", "definition_readback_failed")
    store.configure_repository(repository=SYNTHETIC_REPOSITORY, enabled=True,
                               max_in_flight=1)
    params = dict(repository=SYNTHETIC_REPOSITORY, issue_number=1,
                  request_id=identity["run_key"], correlation_id=identity["run_key"],
                  base_sha=base, max_attempts=5, fencing_required=True)
    task, created = store.enqueue_task(**params)
    replay, replay_created = store.enqueue_task(**params)
    require(created and not replay_created and task["task_id"] == replay["task_id"] == task_id
            and task["state"] == "queued" and task["lease_generation"] == 0,
            "enqueue_independent_readback_failed")
    transition(database, identity, "definition_persisted", "prepared")
    return read_run(authority(), identity)


def api_call(port: int, token_file: Path, method: str, route: str,
             payload: Mapping[str, Any] | None = None, statuses=(200,)):
    from app.native_pool_http import LocalWorkerApi
    with LocalWorkerApi(port=port, token_file=token_file) as api:
        return api._request(method, route, dict(payload) if payload is not None else None,
                            expected_statuses=tuple(statuses))


def checkpoint_artifact(checkpoint: Mapping[str, Any]) -> dict[str, str]:
    from app.native_one_shot import DurableArtifact
    fields = {key: checkpoint.get(key) for key in
              ("artifact_kind", "artifact_ref", "artifact_sha256")}
    return DurableArtifact.parse(fields).checkpoint_fields()


def materialize_independently(root: Path, row: Mapping[str, Any],
                              artifact_fields: Mapping[str, str]) -> dict[str, Any]:
    from app.native_one_shot import DurableArtifact, LocalWorkspace, TaskManifest
    from app.native_runtime import REPAIRED_SOURCE
    from app.s3_git_bundle_artifact import S3GitBundleArtifactTransport
    base = source_fixture(root)
    require(base == row["base_sha"], "independent_base_drift")
    manifest = TaskManifest.parse(document(row["manifest_json"], row["manifest_sha256"]))
    workspace = LocalWorkspace(root)
    transport = S3GitBundleArtifactTransport.from_environment()
    artifact = DurableArtifact.parse(dict(artifact_fields))
    result = transport.materialize(task_id=row["task_id"], manifest=manifest,
                                   workspace=workspace, artifact=artifact)
    require(result.produced_sha != row["base_sha"]
            and workspace.head() == result.produced_sha and workspace.status() == ""
            and (root / "src" / "calculator.py").read_bytes()
            == REPAIRED_SOURCE.encode("utf-8"), "independent_artifact_readback_failed")
    transport.verify(task_id=row["task_id"], manifest=manifest, workspace=workspace,
                     produced_sha=result.produced_sha, artifact=artifact)
    return {"produced_sha": result.produced_sha,
            "artifact_sha256": artifact.artifact_sha256,
            "changed_paths": list(result.changed_paths)}


def b_fence_controls(config: Mapping[str, Any]) -> dict[str, Any]:
    from app.store import ConflictError
    identity = config["identity"]
    database = authority()
    store = pool_store(database)
    row = read_run(database, identity)
    require(row["phase"] == "b_started", "consumer_phase_drift")
    stale = document(row["stale_lease_json"])
    current = store.get_task(row["task_id"])
    lease = lease_fields(current, identity)
    require(current["state"] == "running"
            and lease["worker_id"] == worker_id(identity, "builder-b")
            and lease["lease_generation"] == 2
            and stale["worker_id"] == worker_id(identity, "builder-a")
            and stale["lease_generation"] == 1
            and lease["attempt_id"] != stale["attempt_id"], "consumer_lease_drift")
    checkpoint = store.get_latest_checkpoint(row["task_id"])
    require(checkpoint is not None and checkpoint["lease_generation"] == 2
            and checkpoint["stage"] == "builder_result"
            and checkpoint["effect_state"] == "confirmed"
            and checkpoint_artifact(checkpoint) == document(row["artifact_json"]),
            "consumer_checkpoint_drift")
    mixed_generation = {**lease, "lease_generation": stale["lease_generation"]}
    mixed_attempt = {**lease, "attempt_id": stale["attempt_id"]}
    for rejected in (stale, mixed_generation, mixed_attempt):
        api_call(config["port"], Path(config["token_file"]), "POST",
                 "/v1/tasks/" + row["task_id"] + "/lease/renew",
                 {**rejected, "correlation_id": identity["run_key"] + "-late-fence"},
                 statuses=(409,))
    after = store.get_task(row["task_id"])
    require(after == current, "late_authority_mutated_task")
    fields = document(row["artifact_json"])
    value = fields["artifact_sha256"]
    for rejected in (stale, mixed_generation, mixed_attempt):
        try:
            sink_apply(store, row["task_id"], rejected, value)
        except ConflictError:
            pass
        else:
            raise HarnessBlocked("late_destination_not_fenced")
    # Exactly one semantic effect; replay cannot apply it twice.
    require(sink_apply(store, row["task_id"], lease, value), "effect_not_applied")
    require(not sink_apply(store, row["task_id"], lease, value), "effect_replayed")
    with authority().read_connection() as db:
        effect = db.execute(
            "SELECT * FROM native_continuity_effects WHERE task_id=?",
            (row["task_id"],)).fetchone()
    require(effect is not None and effect["applications"] == 1
            and effect["applied_generation"] == 2
            and effect["applied_attempt"] == lease["attempt_id"]
            and effect["payload_sha256"] == value, "effect_readback_failed")
    return {"authority_rejections": 3, "destination_rejections": 3,
            "original_a_lease_replayed": True, "current_token_old_generation_rejected": True,
            "current_token_old_attempt_rejected": True, "effect_applications": 1,
            "effect_generation": 2, "effect_payload_sha256": value}


def persist_a_checkpoint(config: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    # The real API route has already committed. Publish recovery metadata before
    # the barrier: a later SIGSTOP may suspend a Controller with the PG lock.
    fields = checkpoint_artifact(payload)
    lease = {key: payload[key] for key in
             ("worker_id", "lease_token", "lease_generation", "attempt_id")}
    transition(authority(), config["identity"], "a_started", "a_checkpoint_confirmed", {
        "artifact_json": canonical(fields).decode(),
        "stale_lease_json": canonical(lease).decode(),
    })


class ContinuityGate:
    """Real ASGI facade, with a narrow failure-injection/effect-test boundary.

    Every task mutation first executes the actual app.main route and commits
    through PostgresWorkerPoolStore. This is not an alternate API or store.
    A's confirmed-checkpoint response is withheld until its process tree dies.
    B's confirmed checkpoint is followed by actual rejection/effect requests.
    """

    def __init__(self, app, config: Mapping[str, Any]):
        self.app, self.config = app, config
        self.gated = False

    async def __call__(self, scope, receive, send):
        route = "/v1/tasks/" + self.config["task_id"] + "/checkpoints"
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") != route:
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            require(message["type"] == "http.request", "checkpoint_request_disconnected")
            body.extend(message.get("body", b""))
            require(len(body) <= MAX_DOCUMENT, "checkpoint_body_large")
            if not message.get("more_body", False):
                break
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            payload = {}
        if type(payload) is not dict:
            payload = {}
        identity = self.config["identity"]
        expected_role = "builder-a" if self.config["role"] == "a" else "builder-b"
        generation = 1 if self.config["role"] == "a" else 2
        target = (payload.get("worker_id") == worker_id(identity, expected_role)
                  and payload.get("lease_generation") == generation
                  and payload.get("stage") == "builder_result"
                  and payload.get("next_action") == "submit_validation"
                  and payload.get("effect_state") == "confirmed")
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        if not target:
            await self.app(scope, replay, send)
            return
        require(not self.gated, "checkpoint_gate_repeated")
        self.gated = True
        messages = []
        async def capture(message):
            messages.append(dict(message))
            require(sum(len(item.get("body", b"")) for item in messages) <= MAX_DOCUMENT,
                    "checkpoint_response_large")
        await self.app(scope, replay, capture)
        require(messages and messages[0].get("status") == 201,
                "checkpoint_not_committed")
        if self.config["role"] == "a":
            await asyncio.to_thread(persist_a_checkpoint, self.config, payload)
            protected_json(Path(self.config["barrier"]), {
                "run_key": identity["run_key"], "task_id": self.config["task_id"],
                "api_pid": os.getpid(), "pgid": os.getpgrp(),
                "checkpoint_id": payload["checkpoint_id"], "lease_generation": 1,
            })
            # Supervisor observes the durable marker, freezes the entire group,
            # verifies PostgreSQL+S3 independently, then SIGKILLs the group.
            await asyncio.Event().wait()
        else:
            controls = await asyncio.to_thread(b_fence_controls, self.config)
            protected_json(Path(self.config["barrier"]), controls)
            for message in messages:
                await send(message)


def facade(config_path: Path) -> None:
    import uvicorn
    config = read_private_json(config_path)
    identity = context()
    require(config["identity"] == identity, "facade_identity_drift")
    os.environ.update(facade_environment(config))
    import app.main as main
    require(main.store.__class__.__name__ == "PostgresWorkerPoolStore",
            "facade_not_postgres")
    uvicorn.run(ContinuityGate(main.app, config), host="127.0.0.1",
                port=config["port"], access_log=False, log_level="critical")


def port_available() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_closed(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def private_token(path: Path) -> None:
    value = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def facade_environment(config: Mapping[str, Any]) -> dict[str, str]:
    return {
        "PYTHONDONTWRITEBYTECODE": "1",
        "CODEX_WORKER_POOL_API_TOKEN_FILE": config["token_file"],
        "CODEX_WORKER_POOL_RECONCILIATION_TOKEN_FILE": config["reconciliation_file"],
        "CODEX_WORKER_POOL_EXPECTED_RULES_SHA": config["identity"]["rules_sha"],
        "CODEX_WORKER_POOL_LEASE_SECONDS": str(LEASE_SECONDS),
        "CODEX_WORKER_POOL_HEARTBEAT_TTL_SECONDS": "300",
        "CODEX_WORKER_POOL_MAX_ATTEMPTS": "5",
        "CODEX_WORKER_POOL_PROGRESS_STALL_SECONDS": "300",
        "CODEX_WORKER_POOL_RECONCILE_INTERVAL_SECONDS": "1",
        "CODEX_WORKER_POOL_FENCED_CUTOVER_ATTESTED": "0",
    }


def start_facade(config_path: Path, config: Mapping[str, Any]):
    environment = dict(os.environ)
    environment.update(facade_environment(config))
    process = subprocess.Popen(
        [sys.executable, "-B", str(SCRIPT), "--phase", "_facade",
         "--config", str(config_path)],
        cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while True:
        require(process.poll() is None, "facade_exited_before_ready")
        try:
            result = api_call(config["port"], Path(config["token_file"]),
                              "GET", "/v1/contract")
            require(type(result) is dict and result, "facade_contract_missing")
            return process
        except Exception:
            require(time.monotonic() < deadline, "facade_ready_timeout")
            time.sleep(0.1)


def native_run(config: Mapping[str, Any], source: Path, manifest: Mapping[str, Any],
               role: str, label: str, receipts: Path) -> dict[str, Any]:
    from app.native_runtime import run_synthetic_once
    identity = config["identity"]
    launcher, policy, trusted = load_launcher(Path(config["rules_root"]), identity)
    return run_synthetic_once(
        launcher=launcher, policy_path=policy, **trusted, source_repo=source,
        manifest=dict(manifest), role=role, worker_id=worker_id(identity, label),
        session_id=identity["run_key"] + "-" + label,
        correlation_id=identity["run_key"] + "-" + label,
        token_file=Path(config["token_file"]), api_port=config["port"],
        receipt_root=receipts, runner_name=identity["runner"],
    )


def sanitized_runtime_receipt(raw: Mapping[str, Any], identity: Mapping[str, str],
                              label: str, expected_generation: int) -> dict[str, Any]:
    expected_role = "validator" if label == "validator-b" else "builder"
    require(raw.get("native_runtime_mode") == "synthetic_infrastructure"
            and raw.get("product_cutover") is False
            and raw.get("gateway_verified") is True
            and raw.get("fencing_required") is True
            and raw.get("lease_generation") == expected_generation
            and raw.get("worker_id") == worker_id(identity, label)
            and raw.get("role") == expected_role, "native_receipt_unverified")
    session = raw.get("session_evidence")
    require(type(session) is dict
            and session.get("session_id") == identity["run_key"] + "-" + label
            and session.get("correlation_id") == identity["run_key"] + "-" + label,
            "session_receipt_drift")
    allowed = {
        "schema_version", "result", "task_id", "role", "worker_id",
        "manifest_sha256", "adapter_contract_sha256", "produced_sha",
        "evidence_sha256", "changed_paths", "invoked_commands", "session_id",
        "correlation_id", "gateway_verified", "replayed", "workspace_id",
        "fencing_required", "lease_generation", "attempt_id", "artifact_kind",
        "artifact_sha256", "native_runtime_mode", "product_cutover",
        "runtime_commands_invoked", "runtime_command_count", "artifact_operations",
    }
    result = {key: raw[key] for key in sorted(allowed & set(raw))}
    session_fields = {
        "session_id", "correlation_id", "snapshot_sha256", "expected_head",
        "gateway_sha256", "policy_sha256", "rules_sha", "runner_name",
    }
    result["session_evidence"] = {key: session[key] for key in
                                  sorted(session_fields & set(session))}
    return result


def require_builder_resume_commands(receipt: Mapping[str, Any]) -> None:
    require(type(receipt.get("runtime_commands_invoked")) is list
            and receipt["runtime_commands_invoked"] == []
            and type(receipt.get("runtime_command_count")) is int
            and receipt["runtime_command_count"] == 0,
            "builder_resume_executed_commands")


def actor(config_path: Path) -> None:
    from app.native_runtime import synthetic_manifest
    config = read_private_json(config_path)
    identity = context()
    require(config["identity"] == identity, "actor_identity_drift")
    role, root = config["role"], Path(config["actor_root"])
    require(role in {"a", "b"} and os.getpid() == os.getpgrp(), "actor_group_invalid")
    row = read_run(authority(), identity)
    base = source_fixture(root / "builder-source")
    require(base == row["base_sha"], "actor_base_drift")
    original = document(row["manifest_json"], row["manifest_sha256"])
    server = start_facade(config_path, config)
    try:
        if role == "b":
            # Only the real local controller performs lease-expiry recovery.
            # GET does not reconcile and no injected or advanced clock exists.
            deadline = time.monotonic() + LEASE_SECONDS + 60
            while True:
                task = api_call(config["port"], Path(config["token_file"]),
                                "GET", "/v1/tasks/" + row["task_id"])
                if task["state"] == "queued":
                    require(task["lease_generation"] == 1
                            and task["last_error"] == "lease_expired_recovered",
                            "background_recovery_unproved")
                    break
                require(task["state"] == "running" and task["lease_generation"] == 1
                        and time.monotonic() < deadline, "background_recovery_timeout")
                time.sleep(0.2)
        builder = native_run(config, root / "builder-source", original, "builder",
                             "builder-" + role, root / "builder-receipts")
        require(role == "b", "producer_returned_without_crash")
        require_builder_resume_commands(builder)
        require(builder["produced_sha"] == row["produced_sha"]
                and builder["artifact_sha256"] == document(row["artifact_json"])["artifact_sha256"],
                "builder_recovery_artifact_drift")
        # A Validator gets its own source, Launcher session, worktree and worker.
        verified = materialize_independently(
            root / "validator-source", row, document(row["artifact_json"]))
        require(verified["produced_sha"] == builder["produced_sha"], "validator_source_drift")
        validator_manifest = synthetic_manifest(
            task_id=row["task_id"], base_sha=row["base_sha"],
            workspace_sha=verified["produced_sha"], role="validator")
        validator = native_run(config, root / "validator-source", validator_manifest,
                               "validator", "validator-b", root / "validator-receipts")
        require(builder["target_path"] != validator["target_path"]
                and builder["workspace_id"] != validator["workspace_id"]
                and builder["session_id"] != validator["session_id"],
                "validator_not_independent")
        receipts = {
            "builder": sanitized_runtime_receipt(builder, identity, "builder-b", 2),
            "validator": sanitized_runtime_receipt(validator, identity, "validator-b", 3),
            "controls": read_private_json(Path(config["barrier"])),
        }
        protected_json(Path(config["actor_receipt"]), receipts)
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


def process_record(raw: str) -> dict[str, Any]:
    # /proc/PID/stat comm can contain spaces or parentheses. Field 22 is the
    # start-time tick; retaining it avoids mistaking a reused PID for this actor.
    end = raw.rfind(")")
    require(end > 0 and "(" in raw[:end], "process_stat_invalid")
    try:
        pid = int(raw.split("(", 1)[0].strip())
        fields = raw[end + 2:].split()
        return {"pid": pid, "state": fields[0], "ppid": int(fields[1]),
                "pgid": int(fields[2]), "session": int(fields[3]),
                "start_ticks": int(fields[19])}
    except (ValueError, IndexError):
        raise HarnessBlocked("process_stat_invalid") from None


def process_inventory() -> dict[int, dict[str, Any]]:
    result = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            record = process_record((path / "stat").read_text())
        except (OSError, HarnessBlocked):
            continue
        result[record["pid"]] = record
    return result


def live_group(group: int) -> dict[int, dict[str, Any]]:
    return {pid: row for pid, row in process_inventory().items()
            if row["pgid"] == group and row["state"] not in {"Z", "X", "x"}}


def require_owned_tree(group: int, api_pid: int) -> dict[int, dict[str, Any]]:
    inventory = process_inventory()
    members = {pid: row for pid, row in inventory.items()
               if row["pgid"] == group and row["state"] not in {"Z", "X", "x"}}
    require(group != os.getpgrp() and group in members and api_pid in members
            and members[group]["session"] == group
            and members[api_pid]["session"] == group, "actor_tree_not_owned")
    descendants = {group}
    while True:
        found = {pid for pid, row in inventory.items() if row["ppid"] in descendants}
        expanded = descendants | found
        if expanded == descendants:
            break
        descendants = expanded
    require(all(inventory[pid]["pgid"] == group for pid in descendants),
            "actor_descendant_escaped_group")
    return members


def kill_group(process: subprocess.Popen, port: int) -> dict[str, Any]:
    require(process.pid != os.getpgrp(), "supervisor_kill_refused")
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)
    deadline = time.monotonic() + 10
    while live_group(process.pid) or not port_closed(port):
        require(time.monotonic() < deadline, "actor_tree_survived")
        time.sleep(0.05)
    require(process.returncode == -signal.SIGKILL, "actor_exit_not_sigkill")
    return {"signal": "SIGKILL", "actor_exit_code": process.returncode,
            "live_group_members_after": 0, "local_api_closed": True,
            "executor_tree_terminated": True, "vm_destroyed": False}


def configuration(root: Path, rules_root: Path, identity: Mapping[str, str],
                  role: str, task_id: str) -> tuple[Path, dict[str, Any]]:
    actor_root = root / "actor"
    actor_root.mkdir(mode=0o700)
    token, reconciliation = actor_root / "api-token", actor_root / "reconciliation-token"
    private_token(token)
    private_token(reconciliation)
    value = {
        "identity": dict(identity), "role": role, "task_id": task_id,
        "actor_root": str(actor_root), "rules_root": str(rules_root.resolve()),
        "token_file": str(token), "reconciliation_file": str(reconciliation),
        "port": port_available(), "barrier": str(actor_root / "checkpoint-barrier.json"),
        "actor_receipt": str(actor_root / "actor-receipt.json"),
        "failure": str(actor_root / "blocked.json"),
    }
    path = root / "actor-config.json"
    protected_json(path, value)
    return path, value


def spawn_actor(config_path: Path) -> subprocess.Popen:
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.Popen(
        [sys.executable, "-B", str(SCRIPT), "--phase", "_actor",
         "--config", str(config_path)],
        cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def producer(root: Path, rules_root: Path, identity: Mapping[str, str]) -> dict[str, Any]:
    row = prepare(root, identity)
    config_path, config = configuration(root, rules_root, identity, "a", row["task_id"])
    database = authority()
    transition(database, identity, "prepared", "a_started")
    process = spawn_actor(config_path)
    killed = False
    try:
        deadline = time.monotonic() + DEADLINE_SECONDS
        barrier = Path(config["barrier"])
        while not barrier.exists():
            require_actor_running(process, config, "producer_before_checkpoint_failed")
            require(time.monotonic() < deadline, "producer_checkpoint_timeout")
            time.sleep(0.01)
        marker = read_private_json(barrier)
        require(marker["run_key"] == identity["run_key"]
                and marker["task_id"] == row["task_id"]
                and marker["pgid"] == process.pid, "producer_barrier_drift")
        os.killpg(process.pid, signal.SIGSTOP)
        freeze_deadline = time.monotonic() + 2
        while True:
            members = require_owned_tree(process.pid, marker["api_pid"])
            if all(value["state"] in {"T", "t"} for value in members.values()):
                break
            require(time.monotonic() < freeze_deadline, "producer_tree_not_frozen")
            time.sleep(0.01)
        # A frozen Controller might still hold an advisory transaction lock.
        # Until SIGKILL closes its connections, use only independent MVCC reads.
        with authority().read_connection() as db:
            task_record = db.execute("SELECT * FROM tasks WHERE task_id=?",
                                     (row["task_id"],)).fetchone()
            checkpoint_record = db.execute(
                "SELECT * FROM task_checkpoints WHERE task_id=? "
                "ORDER BY lease_generation DESC,sequence DESC LIMIT 1",
                (row["task_id"],)).fetchone()
        require(task_record is not None, "producer_task_readback_missing")
        current = dict(task_record)
        stale = lease_fields(current, identity)
        checkpoint = dict(checkpoint_record) if checkpoint_record is not None else None
        require(current["state"] == "running" and current["produced_sha"] is None
                and stale["worker_id"] == worker_id(identity, "builder-a")
                and stale["lease_generation"] == 1
                and checkpoint is not None
                and checkpoint["checkpoint_id"] == marker["checkpoint_id"]
                and checkpoint["lease_generation"] == 1
                and checkpoint["stage"] == "builder_result"
                and checkpoint["next_action"] == "submit_validation"
                and checkpoint["effect_state"] == "confirmed",
                "producer_checkpoint_not_durable")
        artifact = checkpoint_artifact(checkpoint)
        confirmed = read_run(authority(), identity)
        require(confirmed["phase"] == "a_checkpoint_confirmed"
                and document(confirmed["artifact_json"]) == artifact
                and document(confirmed["stale_lease_json"]) == stale,
                "checkpoint_metadata_readback_failed")
        verified = materialize_independently(root / "independent-a-readback", row, artifact)
        # Bootstrap evidence is never used as continuation state.
        evidence_path = Path(config["actor_root"]) / "builder-receipts" / "session-evidence.json"
        session_evidence = read_private_json(evidence_path)
        require(session_evidence.get("session_id") == identity["run_key"] + "-builder-a"
                and session_evidence.get("expected_head") == row["base_sha"]
                and SHA256.fullmatch(session_evidence.get("snapshot_sha256", ""))
                and session_evidence.get("state_validated") is True
                and session_evidence.get("gateway_verified") is True
                and session_evidence.get("rules_sha") == identity["rules_sha"]
                and session_evidence.get("runner_name") == identity["runner"]
                and session_evidence.get("native_runtime_mode") == "synthetic_infrastructure"
                and session_evidence.get("product_cutover") is False,
                "producer_session_evidence_missing")
        evidence = kill_group(process, config["port"])
        killed = True
        evidence.update({"observed_process_count": len(members),
                         "native_hostname": socket.gethostname(),
                         "session_evidence": session_evidence,
                         "checkpoint_committed_before_kill": True,
                         "independent_s3_readback_before_kill": True})
        transition(database, identity, "a_checkpoint_confirmed", "a_tree_terminated", {
            "produced_sha": verified["produced_sha"],
            "process_evidence_json": canonical(evidence).decode(),
        })
        observed = read_run(authority(), identity)
        require(observed["phase"] == "a_tree_terminated", "producer_phase_readback_failed")
        return {
            "result": "NATIVE_A_TREE_TERMINATED", "run_key": identity["run_key"],
            "source_sha": identity["source_sha"], "rules_sha": identity["rules_sha"],
            "task_id": row["task_id"], "producer_runner": identity["runner"],
            "generation": 1, "manifest_sha256": row["manifest_sha256"],
            "artifact_sha256": artifact["artifact_sha256"],
            "checkpoint_committed_before_kill": True,
            "executor_tree_terminated": True, "local_api_closed": True,
            "vm_destroyed": False, "product_cutover": False,
        }
    finally:
        if not killed and (process.poll() is None or live_group(process.pid)):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


def invalid_artifact_controls(root: Path, row: Mapping[str, Any]) -> int:
    from app.native_one_shot import DurableArtifact, LocalWorkspace, TaskManifest, NativeExecutorBlocked
    from app.s3_git_bundle_artifact import S3GitBundleArtifactTransport
    fields = document(row["artifact_json"])
    original = document(row["manifest_json"], row["manifest_sha256"])
    base = source_fixture(root)
    require(base == row["base_sha"], "negative_base_drift")
    workspace = LocalWorkspace(root)
    cases = [
        (original, {**fields, "artifact_sha256":
                    ("0" if fields["artifact_sha256"][0] != "0" else "1")
                    + fields["artifact_sha256"][1:]}),
        ({**original, "task_id": "cwp-" + ("f" * 24 if row["task_id"] != "cwp-" + "f" * 24
                                         else "e" * 24)}, fields),
    ]
    for manifest_fields, artifact_fields in cases:
        manifest = TaskManifest.parse(manifest_fields)
        try:
            S3GitBundleArtifactTransport.from_environment().materialize(
                task_id=manifest.task_id, manifest=manifest, workspace=workspace,
                artifact=DurableArtifact.parse(artifact_fields))
        except NativeExecutorBlocked:
            pass
        else:
            raise HarnessBlocked("invalid_artifact_accepted")
        require(workspace.head() == base and workspace.status() == "",
                "invalid_artifact_changed_workspace")
    return len(cases)


def independent_reader(config_path: Path) -> dict[str, Any]:
    from app.store import parse_iso
    config = read_private_json(config_path)
    identity = context()
    require(config["identity"] == identity, "reader_identity_drift")
    database = authority()
    row = read_run(database, identity)
    require(row["phase"] == "b_finished" and row["producer_runner"] != identity["runner"]
            and row["consumer_runner"] == identity["runner"], "independent_phase_drift")
    with database.read_connection() as db:
        task = db.execute("SELECT * FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()
        attempts = db.execute(
            "SELECT * FROM task_attempts WHERE task_id=? ORDER BY lease_generation",
            (row["task_id"],)).fetchall()
        checkpoints = db.execute(
            "SELECT * FROM task_checkpoints WHERE task_id=? ORDER BY lease_generation,sequence",
            (row["task_id"],)).fetchall()
        effect = db.execute(
            "SELECT * FROM native_continuity_effects WHERE task_id=?",
            (row["task_id"],)).fetchone()
        workers = db.execute(
            "SELECT worker_id,host,role,profile,rules_sha,gateway_ok,state_validated,worktree_root "
            "FROM workers WHERE worker_id IN (?,?,?)",
            tuple(worker_id(identity, role) for role in
                  ("builder-a", "builder-b", "validator-b"))).fetchall()
    require(task is not None and task["state"] == "completed"
            and task["produced_sha"] == row["produced_sha"]
            and task["lease_generation"] == 3 and task["leased_by"] is None
            and task["lease_token"] is None and bool(task["fencing_required"]),
            "final_task_readback_failed")
    require([item["lease_generation"] for item in attempts] == [1, 2, 3]
            and [item["worker_id"] for item in attempts] == [
                worker_id(identity, "builder-a"), worker_id(identity, "builder-b"),
                worker_id(identity, "validator-b")]
            and [item["end_reason"] for item in attempts] == [
                "lease_expired_recovered", "validation_handoff", "completed"]
            and parse_iso(attempts[0]["lease_expires_at"])
                <= parse_iso(attempts[1]["lease_started_at"]),
            "real_clock_attempt_history_failed")
    expected_checkpoints = [(1, 1, "artifact_publish", "pending"),
                            (1, 2, "builder_result", "confirmed"),
                            (2, 1, "builder_result", "confirmed"),
                            (3, 1, "validator_result", "none")]
    observed = [(item["lease_generation"], item["sequence"], item["stage"],
                 item["effect_state"]) for item in checkpoints]
    require(observed == expected_checkpoints, "checkpoint_history_drift")
    artifact = document(row["artifact_json"])
    require(all(checkpoint_artifact(item) == artifact for item in checkpoints
                if item["stage"] in {"builder_result", "validator_result"}),
            "resume_artifact_reference_changed")
    require(effect is not None and effect["applications"] == 1
            and effect["payload_sha256"] == artifact["artifact_sha256"]
            and effect["applied_generation"] == 2
            and effect["applied_attempt"] == attempts[1]["attempt_id"],
            "external_effect_readback_failed")
    require(len(workers) == 3 and all(
        item["profile"] == "NORMAL" and bool(item["gateway_ok"])
        and bool(item["state_validated"]) and item["rules_sha"] == identity["rules_sha"]
        for item in workers), "native_registration_unverified")
    require(len({item["worktree_root"] for item in workers}) == 3,
            "native_worktrees_not_independent")
    producer = document(row["process_evidence_json"])
    native = document(row["final_receipt_json"])
    require(producer["executor_tree_terminated"] is True
            and producer["local_api_closed"] is True
            and producer["signal"] == "SIGKILL" and producer["vm_destroyed"] is False
            and native["controls"]["authority_rejections"] == 3
            and native["controls"]["destination_rejections"] == 3,
            "process_or_negative_evidence_incomplete")
    readback = materialize_independently(Path(config["root"]) / "fresh-s3-readback", row, artifact)
    require(readback["produced_sha"] == row["produced_sha"], "final_s3_readback_drift")
    negatives = invalid_artifact_controls(Path(config["root"]) / "invalid-artifact", row)
    result = {
        "result": "NATIVE_SHARED_CONTINUITY_OK", "schema_version": 1,
        "run_key": identity["run_key"], "source_sha": identity["source_sha"],
        "rules_sha": identity["rules_sha"], "task_id": row["task_id"],
        "producer_runner": row["producer_runner"], "receiver_runner": identity["runner"],
        "receiver_native_hostname": socket.gethostname(),
        "authority_backend": "postgres", "artifact_backend": "s3",
        "original_manifest_sha256": row["manifest_sha256"],
        "artifact_sha256": artifact["artifact_sha256"], "produced_sha": row["produced_sha"],
        "lease_generations": [1, 2, 3], "database_clock_recovery": True,
        "executor_a_tree_terminated": True, "api_a_terminated": True,
        "authority_rejections": 3, "destination_rejections": 3,
        "effect_applications": 1, "invalid_artifact_controls": negatives,
        "independent_postgres_readonly_process": True,
        "independent_s3_materialization": True,
        "snapshot_handoff": False, "shared_executor_disk": False,
        "vm_destruction_validated": False, "product_cutover": False,
        "raw_s3_put_fencing_claimed": False,
        "producer_session_evidence": producer["session_evidence"],
        "native_receipts": native,
    }
    transition(database, identity, "b_finished", "verified",
               {"final_receipt_json": canonical(result).decode()})
    protected_json(Path(config["output"]), result)
    return result


def consumer(root: Path, rules_root: Path, identity: Mapping[str, str]) -> dict[str, Any]:
    database = authority()
    row = read_run(database, identity)
    require(row["phase"] == "a_tree_terminated" and row["producer_runner"] != identity["runner"],
            "distinct_receiver_required")
    transition(database, identity, "a_tree_terminated", "b_started",
               {"consumer_runner": identity["runner"]})
    config_path, config = configuration(root, rules_root, identity, "b", row["task_id"])
    process = spawn_actor(config_path)
    try:
        deadline = time.monotonic() + DEADLINE_SECONDS
        while process.poll() is None:
            require(time.monotonic() < deadline, "consumer_runtime_timeout")
            time.sleep(0.1)
        if process.returncode != 0:
            raise HarnessBlocked(actor_failure(config, "consumer_runtime_failed"))
        require(not live_group(process.pid) and port_closed(config["port"]),
                "consumer_local_facade_survived")
        receipts = read_private_json(Path(config["actor_receipt"]))
        transition(database, identity, "b_started", "b_finished",
                   {"final_receipt_json": canonical(receipts).decode()})
        reader_root = root / "independent-reader"
        reader_root.mkdir(mode=0o700)
        reader_output = reader_root / "readback.json"
        reader_config = reader_root / "config.json"
        protected_json(reader_config, {
            "identity": dict(identity), "root": str(reader_root), "output": str(reader_output),
            "failure": str(reader_root / "blocked.json"),
        })
        # New interpreter, new PostgreSQL connection, new S3 client and new Git
        # source. Neither local API nor any actor client survives into this read.
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--phase", "_reader",
             "--config", str(reader_config)],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=120,
        )
        if result.returncode != 0:
            raise HarnessBlocked(actor_failure(
                read_private_json(reader_config), "independent_reader_failed"))
        return read_private_json(reader_output)
    finally:
        if process.poll() is None or live_group(process.pid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


def actor_failure(config: Mapping[str, Any], fallback: str) -> str:
    try:
        path = Path(config["failure"])
        data = read_private_json(path)
        code = data.get("reason_code", "")
        if type(code) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
            return code
    except (KeyError, OSError, HarnessBlocked):
        pass
    return fallback


def require_actor_running(process: subprocess.Popen, config: Mapping[str, Any],
                          fallback: str) -> None:
    if process.poll() is not None:
        raise HarnessBlocked(actor_failure(config, fallback))


def record_blocked(args, code: str) -> None:
    payload = {"result": "NATIVE_SHARED_CONTINUITY_BLOCKED", "reason_code": code}
    if args.phase.startswith("_") and args.config is not None:
        with contextlib.suppress(Exception):
            config = read_private_json(args.config)
            path = Path(config["failure"])
            if not path.exists():
                protected_json(path, payload)
    else:
        print(canonical(payload).decode())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True,
                        choices=("producer", "consumer", "_actor", "_facade", "_reader"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--rules-root", type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from scripts.native_private_config import runtime_environment
        os.environ.update(runtime_environment())
        if args.phase.startswith("_"):
            require(args.config is not None, "private_config_required")
            if args.phase == "_actor":
                actor(args.config)
            elif args.phase == "_facade":
                facade(args.config)
            else:
                independent_reader(args.config)
            return 0
        identity = context()
        require(args.root is not None and args.rules_root is not None,
                "hosted_paths_required")
        root = args.root
        require(not root.exists() and not root.is_symlink()
                and root.resolve().is_relative_to(Path("/workspace/.chatgpt-workers")),
                "isolated_root_invalid")
        root.mkdir(parents=True, mode=0o700)
        require(os.environ.get("REQSYS_WORKSPACE_ROOT") == "/workspace"
                and os.environ.get("REQSYS_GATEWAY_STATE_DIR")
                == "/workspace/.reqsys-command-gateway-posix",
                "canonical_posix_environment_required")
        load_launcher(args.rules_root, identity)
        operation = producer if args.phase == "producer" else consumer
        result = operation(root, args.rules_root, identity)
        print(canonical(result).decode())
        return 0
    except HarnessBlocked as exc:
        record_blocked(args, str(exc))
        return 2
    except Exception as exc:
        # Only NativeExecutorBlocked's source-defined symbolic code is emitted.
        # Driver/provider/credential messages and tracebacks remain suppressed.
        code = "infrastructure_operation_failed"
        if (type(exc).__module__ == "app.native_one_shot"
                and type(exc).__name__ == "NativeExecutorBlocked"
                and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", str(exc))):
            code = str(exc)
        record_blocked(args, code)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
