#!/usr/bin/env python3
"""Exact DEV Owner Risk3 entry point for the external-authority continuity proof."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES_SHA = "0757b9bcbc1e54ef9a1f081069f3b05ac0690062"
LAUNCHER_SHA = "076c8670fde811233918767ad3fda15584735d38a20be2720951e5516e77b257"
GATEWAY_SHA = "14f1389a5c2447e0ec3ae707fd468c224e0f9f492fd7541de65906918c8ac4d1"
POLICY_SHA = "0d320fcdf43c8bc3fcb0b00a0eb1113b28545fefc7b3b4dcc5e5aecc53df4853"
AUTHORITY_ID = "51f8d7cb-133a-4e57-9cd9-d22b2e224aff"
SCHEMA = "ewp_continuity_v1"
SCHEMA_SHA = "3fcefd52f6e1b37eb4ec4724b66ab6662ddc2e228a58cd3ee6f4f2ebaad1d683"
DATABASE_HOST = "ep-square-smoke-b43ynaah-pooler.c-6.us-east-2.aws.neon.tech"


class DriverBlocked(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise DriverBlocked(code)


def broker_controls(broker_host, no_redirect):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), no_redirect())
    with opener.open("https://" + broker_host + "/health", timeout=15) as response:
        require(response.status == 200 and json.loads(response.read(1025)) == {
            "service": "continuity-identity", "version": 1}, "identity_health_failed")
    request = urllib.request.Request(
        "https://" + broker_host + "/bootstrap",
        data=b'{"purpose":"native-continuity-runtime-v1","run_id":"1"}',
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener.open(request, timeout=15):
            raise DriverBlocked("anonymous_identity_was_admitted")
    except urllib.error.HTTPError as exc:
        require(exc.code == 401, "anonymous_identity_control_failed")


def contract_gate(directory, public):
    output = directory / "postgres-contracts.xml"
    with output.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
    # The explicit provider suite cannot skip or silently substitute SQLite.
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-o", "addopts=", "-q",
         "tests/postgres_authority_contract.py", "--junitxml", str(output)],
        cwd=ROOT, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
    )
    try:
        tree = ET.parse(output).getroot()
        suites = [tree] if tree.tag == "testsuite" else list(tree.findall("testsuite"))
        counts = {key: sum(int(s.get(key, "0")) for s in suites)
                  for key in ("tests", "failures", "errors", "skipped")}
        failed = [case.get("name", "") for suite in suites
                  for case in suite.findall("testcase")
                  if case.find("failure") is not None or case.find("error") is not None]
        require(all(re.fullmatch(r"[A-Za-z0-9_\[\].-]{1,180}", name)
                    for name in failed), "contract_case_name_invalid")
    except (ET.ParseError, ValueError, OSError):
        raise DriverBlocked("postgres_contract_report_invalid") from None
    public["postgres_contracts"] = {**counts, "failed_cases": sorted(set(failed)),
                                     "return_code": result.returncode}
    require(result.returncode == 0 and counts == {
        "tests": 10, "failures": 0, "errors": 0, "skipped": 0},
        "postgres_contract_gate_failed")


def execute(args, public, evidence_directory):
    from app.native_gateway_preflight import attest_execution_boundary
    from app.postgres_authority import PostgresAuthority, PostgresWorkerPoolStore
    from scripts import native_shared_continuity as harness
    from scripts.native_private_config import (
        BROKER_HOST, PRIVATE_ROOT, _NoRedirect, hydrate_github,
        read_private, runtime_environment, validate_runtime,
    )
    phase = args.phase
    suffix = "a" if phase == "producer" else "b"
    env = os.environ
    run, attempt = env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_RUN_ATTEMPT", "")
    require(re.fullmatch(r"[1-9][0-9]{2,21}", run)
            and re.fullmatch(r"[1-9][0-9]{0,3}", attempt), "github_run_invalid")
    expected_session = "live-ewp-" + run + "-" + attempt + "-" + suffix
    expected_venv = Path("/workspace/.chatgpt-workers") / (
        "live-venv-" + run + "-" + attempt + "-" + suffix)
    require(env.get("GITHUB_ACTIONS") == "true"
            and env.get("GITHUB_REPOSITORY") == "ericson-j-santos/engineering-worker-pool"
            and env.get("GITHUB_REPOSITORY_OWNER") == "ericson-j-santos"
            and env.get("GITHUB_ACTOR") == "ericson-j-santos"
            and env.get("GITHUB_EVENT_NAME") == "push"
            and env.get("GITHUB_REF") ==
                "refs/heads/feat/hosted-cold-checkpoint-resume-20261020",
            "github_workflow_not_admitted")
    require(env.get("EXPECTED_RULES_SHA") == RULES_SHA
            and env.get("EXPECTED_SHA") == env.get("GITHUB_SHA")
            and re.fullmatch(r"[0-9a-f]{40}", env.get("EXPECTED_SHA", ""))
            and env.get("EWP_SESSION") == expected_session
            and env.get("EWP_CORRELATION") == expected_session,
            "outer_identity_invalid")
    require(ROOT == Path("/workspace/.chatgpt-workers") / ("wt-chat-" + expected_session)
            and Path.cwd() == ROOT and sys.prefix == str(expected_venv)
            and sys.prefix != sys.base_prefix,
            "native_execution_path_invalid")
    require(args.root == Path("/workspace/.chatgpt-workers") / (
        "native-" + run + "-" + attempt + "-" + suffix)
            and args.rules_root == Path("/workspace/.chatgpt-workers/operational-rules-validation"),
            "native_execution_path_invalid")
    require(env.get("REQSYS_WORKSPACE_ROOT") == "/workspace"
            and env.get("REQSYS_GATEWAY_STATE_DIR") ==
                "/workspace/.reqsys-command-gateway-posix",
            "canonical_environment_required")
    env["PATH"] = str(expected_venv / "bin") + ":/usr/local/bin:/usr/bin:/bin"
    env.update({
        "GIT_AUTHOR_NAME": "Native Continuity Fixture",
        "GIT_AUTHOR_EMAIL": "continuity@example.invalid",
        "GIT_COMMITTER_NAME": "Native Continuity Fixture",
        "GIT_COMMITTER_EMAIL": "continuity@example.invalid",
    })
    for name in ("SSLKEYLOGFILE", "PYTHONPATH", "PYTHONSTARTUP",
                 "CODEX_WORKER_POOL_DB"):
        env.pop(name, None)
    launcher, policy_path, pins = harness.load_launcher(
        args.rules_root, {"rules_sha": RULES_SHA})
    require(pins == {
        "trusted_launcher_sha256": LAUNCHER_SHA,
        "trusted_gateway_sha256": GATEWAY_SHA,
        "trusted_policy_sha256": POLICY_SHA,
        "trusted_rules_sha": RULES_SHA}, "canonical_rules_digest_mismatch")
    gateway = launcher.cg
    with contextlib.redirect_stdout(io.StringIO()):
        inspection = gateway.inspect(
            ROOT, gateway.load_policy(policy_path),
            correlation_id=expected_session, session_id=expected_session)
    require(inspection == 0, "outer_inspection_failed")
    boundary = attest_execution_boundary(
        gateway=gateway, policy_path=policy_path, workspace=ROOT,
        session_id=expected_session, correlation_id=expected_session,
        expected_head=env["EXPECTED_SHA"], trusted_gateway_sha256=GATEWAY_SHA,
        trusted_policy_sha256=POLICY_SHA)
    boundary.validate()
    public["outer_attestation"] = {
        "session_id": boundary.session_id, "source_sha": boundary.expected_head,
        "gateway_verified": boundary.gateway_verified,
        "state_validated": boundary.state_validated,
        "gateway_sha256": GATEWAY_SHA, "policy_sha256": POLICY_SHA,
        "rules_sha": RULES_SHA}
    broker_controls(BROKER_HOST, _NoRedirect)
    public["identity_anonymous_rejected"] = True
    public["identity"] = hydrate_github()["identity"]
    # Configuration is read only after real host/session and OIDC attestation.
    env.update(runtime_environment())
    env["CODEX_WORKER_POOL_FENCED_CUTOVER_ATTESTED"] = "0"
    for name in ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
        env.pop(name, None)
    runtime = validate_runtime(json.loads(read_private(PRIVATE_ROOT / "runtime.json")))
    from urllib.parse import urlsplit
    require(runtime["schema"] == SCHEMA and runtime["authority_id"] == AUTHORITY_ID
            and urlsplit(runtime["database_url"]).hostname == DATABASE_HOST,
            "qualified_database_binding_mismatch")
    database = PostgresAuthority.from_file(
        Path(env["CODEX_WORKER_POOL_DATABASE_URL_FILE"]),
        schema=SCHEMA, authority_id=AUTHORITY_ID)
    qualified = database.verify_schema()
    require(qualified["schema_sha256"] == SCHEMA_SHA, "migration_digest_mismatch")
    with database.read_connection() as connection:
        privilege = connection.execute(
            "SELECT current_user AS role,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls,"
            "has_schema_privilege(current_user,?,'CREATE') AS can_create "
            "FROM pg_catalog.pg_roles WHERE rolname=current_user", (SCHEMA,)).fetchone()
    require(privilege["role"] == "ewp_continuity_runtime"
            and not any(privilege[key] for key in
                        ("rolsuper", "rolcreatedb", "rolcreaterole", "rolbypassrls", "can_create")),
            "runtime_privileges_not_qualified")
    public["authority"] = {
        "authority_id": AUTHORITY_ID, "schema": SCHEMA, "schema_sha256": SCHEMA_SHA,
        "runtime_role": privilege["role"], "administrative_privileges": False,
        "schema_create": False, "tls": "verify-full"}
    pool = PostgresWorkerPoolStore(database, expected_rules_sha=RULES_SHA)
    status = pool.fenced_cutover_status()
    if not status["enabled"]:
        require(phase == "producer", "fenced_authority_not_prepared")
        with database.read_connection() as connection:
            count = connection.execute(
                "SELECT (SELECT count(*) FROM tasks) AS tasks,"
                "(SELECT count(*) FROM workers) AS workers").fetchone()
        require(count == {"tasks": 0, "workers": 0},
                "initial_dev_activation_requires_empty_authority")
        status = pool.enable_fenced_cutover(
            correlation_id=expected_session,
            legacy_claimers_drained=count["workers"] == 0,
            shared_queue_exclusive=count["tasks"] == 0)
    require(status["enabled"], "fenced_activation_readback_failed")
    public["dev_fenced_authority"] = True
    public["activation_correlation"] = status.get("enabled_by_correlation_id")
    if phase == "producer":
        contract_gate(evidence_directory, public)
    # The imported harness always reads the same private runtime provider.
    # Subprocess actors inherit only its qualified runtime environment.
    identity = harness.context()
    require(not args.root.exists() and not args.root.is_symlink(),
            "native_phase_root_exists")
    args.root.mkdir(parents=True, mode=0o700)
    operation = harness.producer if phase == "producer" else harness.consumer
    result = operation(args.root, args.rules_root, identity)
    keys = {
        "result", "run_key", "source_sha", "rules_sha", "task_id",
        "producer_runner", "receiver_runner", "generation", "manifest_sha256",
        "artifact_sha256", "produced_sha", "lease_generations",
        "checkpoint_committed_before_kill", "executor_tree_terminated",
        "local_api_closed", "executor_a_tree_terminated", "api_a_terminated",
        "authority_rejections", "destination_rejections", "effect_applications",
        "database_clock_recovery", "independent_postgres_readonly_process",
        "independent_s3_materialization", "invalid_artifact_controls",
        "snapshot_handoff", "shared_executor_disk", "vm_destruction_validated",
        "vm_destroyed", "product_cutover",
    }
    public["continuity"] = {key: result[key] for key in sorted(keys & result.keys())}
    if phase == "consumer":
        native = result["native_receipts"]
        public["resumed_builder_commands"] = native["builder"]["runtime_commands_invoked"]
        public["sessions"] = [result["producer_session_evidence"],
                              native["builder"]["session_evidence"],
                              native["validator"]["session_evidence"]]
    public["result"] = "NATIVE_LIVE_DRIVER_OK"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("producer", "consumer"), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--rules-root", type=Path, required=True)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    run, attempt = os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not re.fullmatch(r"[1-9][0-9]{2,21}", run) or not re.fullmatch(r"[1-9][0-9]{0,3}", attempt):
        print('{"result":"NATIVE_LIVE_DRIVER_BLOCKED","code":"github_run_invalid"}')
        return 2
    suffix = "a" if args.phase == "producer" else "b"
    evidence_directory = Path("/workspace/.chatgpt-workers") / (
        "native-public-" + run + "-" + attempt + "-" + suffix)
    public = {"result": "NATIVE_LIVE_DRIVER_BLOCKED", "phase": args.phase,
              "run_id": run, "run_attempt": attempt,
              "source_sha": os.environ.get("EXPECTED_SHA"),
              "product_cutover": False}
    status = 2
    try:
        require(not evidence_directory.exists() and not evidence_directory.is_symlink(),
                "public_receipt_path_exists")
        evidence_directory.mkdir(mode=0o700)
        execute(args, public, evidence_directory)
        status = 0
    except Exception as exc:
        code = "native_infrastructure_operation_failed"
        if type(exc).__module__ in {
            __name__, "app.postgres_authority", "app.native_one_shot",
            "scripts.native_private_config", "scripts.native_shared_continuity",
        } and re.fullmatch(r"[a-z][a-z0-9_:]{0,127}", str(exc)):
            code = str(exc)
        public["code"] = code
        public["exception_type"] = type(exc).__name__
    finally:
        if evidence_directory.is_dir() and not evidence_directory.is_symlink():
            from scripts.native_shared_continuity import protected_json
            protected_json(evidence_directory / "receipt.json", public)
    # Owner Risk3 keeps only stdout/stderr digests. A later read-only Gateway
    # command emits the bounded public receipt; private reports stay local.
    print(json.dumps({"result": public["result"], "phase": args.phase}, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
