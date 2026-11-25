"""Real native PostgreSQL acceptance for EWP #61 CAS v2.

This file is intentionally outside pytest's default test_*.py discovery.  The
issue-61 branch workflow invokes it only on a disposable public hosted runner.
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from app.postgres_authority import (
    AuthorityUnavailable, PostgresAuthority, PostgresWorkerPoolStore,
)
from app.postgres_cas_v2 import PostgresCasV2

OWNER_DSN = os.environ.get("EWP61_TEST_POSTGRES_DSN", "")
RULES_SHA = "a" * 40
BASE_SHA = "1" * 40
SOURCE_SHA = "2" * 40
PRODUCED_SHA = "3" * 40


def authority(parameters, schema, authority_id):
    return PostgresAuthority(
        parameters, schema=schema, authority_id=authority_id,
        allow_insecure_loopback=True,
    )


@pytest.fixture(scope="module")
def scope():
    if os.environ.get("EWP61_EPHEMERAL_NATIVE") != "1" or not OWNER_DSN:
        pytest.fail("ewp61_ephemeral_native_gate_missing")
    owner = conninfo_to_dict(OWNER_DSN)
    assert owner.get("host") == "127.0.0.1"
    tag = uuid.uuid4().hex[:12]
    schema = "ewp61_" + tag
    rollback_schema = schema + "_rb"
    publisher_role = "ewp61_pub_" + tag
    reader_role = "ewp61_read_" + tag
    publisher_password = uuid.uuid4().hex
    reader_password = uuid.uuid4().hex
    authority_id = str(uuid.uuid4())
    emitter_id = str(uuid.uuid4())

    control = psycopg.connect(
        **owner, autocommit=True, row_factory=dict_row,
        prepare_threshold=None, application_name="ewp61-contract-control",
    )
    try:
        for role, password in (
            (publisher_role, publisher_password),
            (reader_role, reader_password),
        ):
            control.execute(sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION"
            ).format(sql.Identifier(role), sql.Literal(password)))

        owner_authority = authority(owner, schema, authority_id)
        owner_authority.initialize()
        cas = PostgresCasV2(owner_authority)
        evidence = cas.migrate(
            publisher_role=publisher_role, reader_role=reader_role,
            emitter_id=emitter_id, heartbeat_ttl_seconds=37,
        )
        assert evidence["extension_version"] == 2
        assert owner_authority.verify_schema()["schema_version"] == 1

        publisher = dict(owner)
        publisher.update(user=publisher_role, password=publisher_password)
        reader = dict(owner)
        reader.update(user=reader_role, password=reader_password)
        value = {
            "tag": tag, "schema": schema, "rollback_schema": rollback_schema,
            "owner": owner, "owner_authority": owner_authority, "cas": cas,
            "publisher_role": publisher_role, "reader_role": reader_role,
            "publisher": publisher, "reader": reader,
            "authority_id": authority_id, "emitter_id": emitter_id,
            "control": control,
        }
        yield value
    finally:
        control.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
            sql.Identifier(schema)))
        control.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
            sql.Identifier(rollback_schema)))
        control.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(
            sql.Identifier(publisher_role)))
        control.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(
            sql.Identifier(reader_role)))
        control.close()


def make_task(scope, suffix, payload):
    pool = PostgresWorkerPoolStore(
        scope["owner_authority"], heartbeat_ttl_seconds=37,
        default_lease_seconds=60, default_max_attempts=4,
        expected_rules_sha=RULES_SHA,
    )
    pool.enable_fenced_cutover(
        correlation_id="ewp61-cutover-" + suffix,
        legacy_claimers_drained=True, shared_queue_exclusive=True,
    )
    repository = "example/ewp61-" + scope["tag"] + "-" + suffix
    worker = "builder-" + scope["tag"] + "-" + suffix
    pool.configure_repository(repository=repository, enabled=True, max_in_flight=1)
    pool.register_worker(
        worker_id=worker, host="github-hosted-native", role="builder",
        profile="NORMAL", controller_version="ewp61-contract",
        rules_sha=RULES_SHA, gateway_ok=True, state_validated=True,
        correlation_id="ewp61-register-" + suffix,
    )
    pool.replace_worker_affinities(worker, [repository])
    task, created = pool.enqueue_task(
        repository=repository, issue_number=61,
        request_id="req-" + scope["tag"] + "-" + suffix,
        correlation_id="ewp61-corr-" + suffix, base_sha=BASE_SHA,
        max_attempts=4, fencing_required=True,
    )
    assert created
    row, lease = pool.claim_task(
        worker_id=worker, role="builder", correlation_id="ewp61-claim-" + suffix,
        only_task_id=task["task_id"], fencing_required=True, lease_seconds=60,
    )
    assert row and lease
    pool.start_task(
        task_id=task["task_id"], worker_id=worker,
        lease_token=lease.lease_token, lease_generation=lease.lease_generation,
        attempt_id=lease.attempt_id, correlation_id="ewp61-start-" + suffix,
    )
    digest = hashlib.sha256(payload).hexdigest()
    checkpoint, created = pool.record_checkpoint(
        task_id=task["task_id"], worker_id=worker,
        lease_token=lease.lease_token, lease_generation=lease.lease_generation,
        attempt_id=lease.attempt_id, checkpoint_id="cp-" + suffix,
        sequence=1, stage="cas_publish", next_action="publish",
        correlation_id="ewp61-corr-" + suffix, artifact_kind="git_bundle",
        artifact_ref="cas-pending", artifact_sha256=digest,
        effect_id="effect-" + suffix, effect_state="pending",
    )
    assert created
    current = pool.get_task(task["task_id"])
    binding = {
        "authority_id": scope["authority_id"],
        "emitter_id": scope["emitter_id"],
        "task_id": task["task_id"],
        "effect_id": "effect-" + suffix,
        "repository": repository,
        "issue_number": 61,
        "request_id": current["request_id"],
        "correlation_id": current["correlation_id"],
        "base_sha": BASE_SHA,
        "source_sha": SOURCE_SHA,
        "produced_sha": PRODUCED_SHA,
        "worker_id": worker,
        "lease_token": lease.lease_token,
        "lease_generation": lease.lease_generation,
        "attempt_id": lease.attempt_id,
        "rules_sha": RULES_SHA,
        "checkpoint_id": checkpoint["checkpoint_id"],
        "checkpoint_digest": checkpoint["content_digest"],
        "artifact_sha256": digest,
    }
    return {
        "pool": pool, "task_id": task["task_id"], "worker": worker,
        "lease": lease, "repository": repository, "digest": digest,
        "effect_id": "effect-" + suffix, "binding": binding,
    }


def port(scope, kind):
    params = scope[kind]
    return PostgresCasV2(authority(
        params, scope["schema"], scope["authority_id"],
    ))


def reconcile(reader, case):
    return reader.reconcile(
        task_id=case["task_id"], effect_id=case["effect_id"],
        digest_hex=case["digest"], repository=case["repository"],
        base_sha=BASE_SHA, source_sha=SOURCE_SHA, produced_sha=PRODUCED_SHA,
    )


def test_authenticated_concurrent_durable_replay_and_historical_recovery(scope):
    payload = b"ewp61-durable-git-bundle-v2"
    case = make_task(scope, "main", payload)
    publisher = port(scope, "publisher")
    attestation = publisher.issue_attestation(case["binding"])
    assert publisher.issue_attestation(case["binding"]) == attestation

    blocker = scope["control"]
    blocker.execute("BEGIN")
    blocker.execute("SELECT pg_advisory_xact_lock(%s)",
                    (scope["owner_authority"].lock_key,))
    def run_publish():
        return port(scope, "publisher").publish(attestation, payload)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run_publish) for _ in range(2)]
        deadline = time.monotonic() + 10
        waiting = []
        while time.monotonic() < deadline:
            waiting = blocker.execute(
                "SELECT pid FROM pg_stat_activity WHERE usename=%s "
                "AND state='active' AND query LIKE %s ORDER BY pid",
                (scope["publisher_role"], "%publish_fenced_cas_v2%")
            ).fetchall()
            if len({row["pid"] for row in waiting}) == 2:
                break
            time.sleep(0.05)
        assert len({row["pid"] for row in waiting}) == 2
        blocker.commit()
        results = [future.result(timeout=15) for future in futures]

    assert sorted(result["created"] for result in results) == [False, True]
    assert len({result["backend_pid"] for result in results}) == 2
    reader = port(scope, "reader")
    recovered = reconcile(reader, case)
    assert recovered["status"] == "confirmed"
    assert recovered["payload"] == payload
    assert recovered["sha256_hex"] == case["digest"]

    with scope["owner_authority"].read_connection() as db:
        assert db.execute("SELECT count(*) AS n FROM cas_objects_v2").fetchone()["n"] == 1
        assert db.execute(
            "SELECT count(*) AS n FROM cas_publication_receipts_v2"
        ).fetchone()["n"] == 1

    with pytest.raises(AuthorityUnavailable, match="cas_reconcile_rejected"):
        reader.reconcile(
            task_id=case["task_id"], effect_id=case["effect_id"],
            digest_hex=case["digest"], repository=case["repository"] + "-wrong",
            base_sha=BASE_SHA, source_sha=SOURCE_SHA, produced_sha=PRODUCED_SHA,
        )

    # Simulate commit success with a lost response, then an expired old lease and
    # a new generation.  Recovery is historical readback; no second write occurs.
    with scope["owner_authority"].transaction() as db:
        db.execute(
            "UPDATE task_attempts SET ended_at=?,end_reason=? WHERE attempt_id=?",
            ("2026-01-01T00:00:00Z", "lost_response", case["lease"].attempt_id),
        )
        db.execute(
            "UPDATE tasks SET state='queued',leased_by=NULL,lease_token=NULL,"
            "lease_expires_at=NULL,attempt_id=NULL WHERE task_id=?",
            (case["task_id"],),
        )
    next_worker = "builder-" + scope["tag"] + "-next"
    case["pool"].register_worker(
        worker_id=next_worker, host="github-hosted-native-next", role="builder",
        profile="NORMAL", controller_version="ewp61-contract",
        rules_sha=RULES_SHA, gateway_ok=True, state_validated=True,
        correlation_id="ewp61-register-next",
    )
    case["pool"].replace_worker_affinities(next_worker, [case["repository"]])
    _row, new_lease = case["pool"].claim_task(
        worker_id=next_worker, role="builder", correlation_id="ewp61-reclaim",
        only_task_id=case["task_id"], fencing_required=True, lease_seconds=60,
    )
    assert new_lease.lease_generation == case["lease"].lease_generation + 1
    assert reconcile(reader, case)["payload"] == payload
    with pytest.raises(AuthorityUnavailable, match="cas_publish_rejected"):
        publisher.publish(attestation, payload)


def test_expiration_identity_custody_and_safe_rollback(scope):
    payload = b"ewp61-expiry-negative"
    case = make_task(scope, "expiry", payload)
    publisher = port(scope, "publisher")
    attestation = publisher.issue_attestation(case["binding"])
    with scope["owner_authority"].transaction() as db:
        db.execute(
            "UPDATE cas_attestations_v2 SET expires_at=? WHERE attestation_id=?",
            ("2026-01-01T00:00:00Z", attestation),
        )
    with pytest.raises(AuthorityUnavailable, match="cas_publish_rejected"):
        publisher.publish(attestation, payload)

    reader = port(scope, "reader")
    assert reconcile(reader, case)["status"] == "not_applied"
    with pytest.raises(AuthorityUnavailable, match="cas_attestation_rejected"):
        reader.issue_attestation(case["binding"])
    with pytest.raises(AuthorityUnavailable, match="cas_reconcile_rejected"):
        publisher.reconcile(
            task_id=case["task_id"], effect_id=case["effect_id"],
            digest_hex=case["digest"], repository=case["repository"],
            base_sha=BASE_SHA, source_sha=SOURCE_SHA, produced_sha=PRODUCED_SHA,
        )
    raw = publisher.authority._connect()
    try:
        raw.execute("BEGIN")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            raw.execute(sql.SQL(
                "INSERT INTO {}.cas_objects_v2(sha256_hex,payload,size_bytes) "
                "VALUES(%s,%s,%s)"
            ).format(sql.Identifier(scope["schema"])),
                ("f" * 64, b"x", 1))
        raw.rollback()
    finally:
        raw.close()

    rollback_authority = authority(
        scope["owner"], scope["rollback_schema"], scope["authority_id"],
    )
    rollback_authority.initialize()
    rollback_cas = PostgresCasV2(rollback_authority)
    rollback_cas.migrate(
        publisher_role=scope["publisher_role"],
        reader_role=scope["reader_role"],
        emitter_id=scope["emitter_id"], heartbeat_ttl_seconds=37,
    )
    evidence = rollback_cas.rollback_empty()
    assert evidence["schema_version"] == 1
    assert evidence["schema_sha256"] == rollback_authority.schema_sha256
    with rollback_authority.read_connection() as db:
        assert db.execute(
            "SELECT to_regclass(?) AS relation",
            (scope["rollback_schema"] + ".cas_objects_v2",)
        ).fetchone()["relation"] is None
