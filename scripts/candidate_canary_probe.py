#!/usr/bin/env python3
"""Disposable GitHub-hosted Docker canary; NEVER a PC24x7 cutover receipt."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

REPOSITORY = "ericson-j-santos/engineering-worker-pool"
CONSUMER = "ericson-j-santos/reqsys-v2-enterprise-real"
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
URL = "http://127.0.0.1:18098"
SQL_READBACK = """
import json, sqlite3
db = sqlite3.connect("file:/data/canary.db?mode=ro", uri=True)
try:
    db.execute("PRAGMA query_only=ON")
    rows = db.execute("SELECT task_id,issue_number,state,leased_by,attempt_count FROM tasks").fetchall()
    worker_count = db.execute("SELECT count(*) FROM workers").fetchone()[0]
    lanes = db.execute("SELECT repository,enabled FROM repository_lanes").fetchall()
    print(json.dumps({"tasks": rows, "workers": worker_count, "lanes": lanes}))
finally:
    db.close()
"""


class CanaryBlocked(RuntimeError):
    """Fail-closed exception; messages may never contain tokens or DB bytes."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise CanaryBlocked(reason)


def command(*argv: str, timeout: int = 30) -> str:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise CanaryBlocked("subprocess_unavailable") from None
    require(p.returncode == 0, "subprocess_failed")
    return p.stdout.strip()


def context(root: Path, consumer_root: Path, env: dict[str, str]) -> tuple[str, str, str]:
    sha, consumer_sha = env.get("EXPECTED_SHA", ""), env.get("REQSYS_SHA", "")
    require(all(SHA40.fullmatch(v) for v in (sha, consumer_sha)), "sha_invalid")
    require(
        env.get("GITHUB_ACTIONS") == "true"
        and env.get("RUNNER_OS") == "Linux"
        and env.get("GITHUB_REPOSITORY") == REPOSITORY
        and env.get("GITHUB_EVENT_NAME") in {"push", "pull_request"},
        "not_disposable_ci",
    )
    require(root.is_dir() and consumer_root.is_dir() and not consumer_root.is_symlink(), "source_missing")
    require(command("git", "-C", str(root), "rev-parse", "HEAD") == sha, "worker_sha_mismatch")
    require(command("git", "-C", str(consumer_root), "rev-parse", "HEAD") == consumer_sha,
            "consumer_sha_mismatch")
    require(command("git", "-C", str(root), "diff", "--name-only") == "", "tracked_tree_dirty")
    return sha, consumer_sha, f"canary-{env['GITHUB_RUN_ID']}-{env['GITHUB_RUN_ATTEMPT']}"


def inspect_container(raw: dict[str, Any], *, image_id: str, name: str,
                      volume: str, token_path: Path) -> None:
    config = raw.get("Config") or {}
    host = raw.get("HostConfig") or {}
    ports = (host.get("PortBindings") or {}).get("8097/tcp") or []
    mounts = raw.get("Mounts") or []
    require(
        raw.get("Image") == image_id
        and raw.get("Name") == "/" + name
        and (raw.get("State") or {}).get("Running") is True
        and host.get("Privileged") is False
        and config.get("User") == "reqsys"
        and "ALL" in (host.get("CapDrop") or [])
        and host.get("ReadonlyRootfs") is True
        and host.get("NetworkMode") == "bridge"
        and len(ports) == 1
        and ports[0].get("HostIp") == "127.0.0.1"
        and ports[0].get("HostPort") == "18098",
        "container_identity_or_isolation_invalid",
    )
    require(len(mounts) == 2, "unexpected_container_mounts")
    targets = {mount.get("Destination"): mount for mount in mounts}
    data, token = targets.get("/data") or {}, targets.get("/run/secrets/canary-token") or {}
    require(data.get("Type") == "volume" and data.get("Name") == volume and data.get("RW") is True,
            "volume_isolation_invalid")
    require(token.get("Type") == "bind" and token.get("RW") is False
            and token.get("Source") == str(token_path.resolve()), "token_bind_invalid")


def run(root: Path, consumer_root: Path, token_file: Path, env: dict[str, str]) -> dict[str, Any]:
    sha, consumer_sha, correlation = context(root, consumer_root, env)
    name, volume = env.get("CANARY_CONTAINER", ""), env.get("CANARY_VOLUME", "")
    image_id = env.get("CANARY_IMAGE_ID", "")
    require(re.fullmatch(r"wp-canary-[0-9]{3,22}-[0-9]{1,4}", name) is not None
            and volume == name and re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is not None,
            "container_image_identity_invalid")
    require(token_file.is_file() and not token_file.is_symlink(), "test_token_file_invalid")
    token = token_file.read_text(encoding="utf-8").strip()
    require(len(token) >= 32 and "\n" not in token, "test_token_invalid")

    image = json.loads(command("docker", "image", "inspect", image_id))
    require(len(image) == 1 and image[0].get("Id") == image_id, "image_digest_invalid")
    labels = (image[0].get("Config") or {}).get("Labels") or {}
    require(labels.get("org.opencontainers.image.revision") == sha, "image_source_sha_invalid")
    container = json.loads(command("docker", "inspect", name))
    require(len(container) == 1, "container_not_unique")
    inspect_container(container[0], image_id=image_id, name=name, volume=volume,
                      token_path=token_file)

    sys.path.insert(0, str(consumer_root))
    from scripts import pending_development_worker_pool_bridge as bridge

    with httpx.Client(base_url=URL, timeout=4, trust_env=False, follow_redirects=False) as client:
        for attempt in range(30):
            try:
                response = client.get("/health")
                if response.status_code == 200 and response.json().get("status") == "healthy":
                    break
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.5)
        else:
            raise CanaryBlocked("candidate_health_unavailable")
        require(client.get("/v1/contract").status_code == 401, "anonymous_auth_not_blocked")
        require(client.get("/v1/contract", headers={"Authorization": "Bearer invalid"})
                .status_code == 401, "invalid_token_not_blocked")
        headers = {"Authorization": "Bearer " + token}
        contract = client.get("/v1/contract", headers=headers)
        require(contract.status_code == 200 and
                contract.json().get("contract_version") == "v1" and
                contract.json().get("contract_name") == "engineering-worker-pool",
                "public_contract_invalid")
        openapi = client.get("/openapi.json")
        require(openapi.status_code == 200 and
                openapi.json().get("info", {}).get("version") == "1.4.0",
                "candidate_api_version_invalid")
        require(client.get("/v1/tasks/not-valid/diagnostics", headers=headers).status_code == 422,
                "invalid_diagnostics_id_not_blocked")
        lane = client.post("/v1/repositories", headers=headers, json={
            "repository": CONSUMER, "enabled": False, "max_in_flight": 1,
            "correlation_id": correlation[:128],
        })
        require(lane.status_code == 200 and lane.json().get("enabled") is False,
                "canary_lane_not_disabled")

        report = bridge.direct_report(CONSUMER, 2020, "main", correlation)
        handoff = bridge.enqueue_local_work(
            report, base_sha=consumer_sha, pool_url=URL, token=token,
            allow_legacy_fallback=False, allow_work_fallback=False)
        require(handoff.get("result") == "WORKER_POOL_ENQUEUED" and
                handoff.get("contract_mode") == "v1" and
                handoff.get("dispatch_modes") == ["work_v1"] and
                handoff.get("legacy_fallback_used") is False, "reqsys_consumer_contract_failed")
        entries = handoff.get("items") or []
        require(len(entries) == 1 and entries[0].get("replay_created") is False and
                entries[0].get("independent_readback") is True, "reqsys_consumer_replay_failed")
        task_id, work_id = entries[0].get("task_id"), entries[0].get("work_id")
        require(isinstance(task_id, str) and isinstance(work_id, str), "id_missing")
        work = client.get(f"/v1/work/{work_id}", headers=headers)
        diag = client.get(f"/v1/tasks/{task_id}/diagnostics", headers=headers)
        require(work.status_code == 200 and diag.status_code == 200
                and diag.json().get("claim_performed") is False
                and diag.json().get("task", {}).get("state") == "queued"
                and diag.json().get("precheck_eligible_workers") == 0,
                "read_only_diagnostics_invalid")

    sql = json.loads(command("docker", "exec", "--user", "reqsys", name,
                             "python", "-c", SQL_READBACK))
    tasks, lanes = sql.get("tasks") or [], sql.get("lanes") or []
    require(len(tasks) == 1 and tasks[0][0] == task_id and tasks[0][1] == 2020
            and tasks[0][2] == "queued" and tasks[0][3] is None and tasks[0][4] == 0
            and sql.get("workers") == 0
            and lanes == [[CONSUMER, 0]], "independent_sqlite_readback_invalid")
    return {
        "result": "WORKER_POOL_CANDIDATE_HOSTED_CANARY_PASSED",
        "environment": "github-hosted-disposable",
        "worker_pool_sha": sha,
        "reqsys_sha": consumer_sha,
        "image_id": image_id,
        "contract_version": "v1",
        "application_version": "1.4.0",
        "correlation_id": correlation,
        "authenticated_reqsys_consumer_e2e": True,
        "negative_auth_control": True,
        "replay_idempotent": True,
        "independent_sqlite_readback": True,
        "isolated_lane_disabled": True,
        "worker_count": 0,
        "task_count": 1,
        "physical_runtime_validated": False,
        "candidate_image_digest_attested_on_desktop": False,
        "rollback_rehearsal_passed": False,
        "cutover_ready": False,
        "production_touched": False,
        "live_database_modified": False,
        "secrets_exposed": False,
    }


def main() -> int:
    report = Path("artifacts/candidate-canary/evidence.json")
    try:
        evidence = run(Path.cwd(), Path(".reqsys-consumer").resolve(),
                       Path(os.environ["CANARY_TOKEN_FILE"]), os.environ)
        exit_code = 0
    except Exception as exc:
        evidence = {"result": "WORKER_POOL_CANDIDATE_HOSTED_CANARY_BLOCKED",
                    "reason": type(exc).__name__ if not isinstance(exc, CanaryBlocked)
                    else str(exc),
                    "cutover_ready": False, "physical_runtime_validated": False,
                    "production_touched": False, "secrets_exposed": False}
        exit_code = 2
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(evidence, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in evidence.items() if k != "correlation_id"}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
