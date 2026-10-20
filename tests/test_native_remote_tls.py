"""Real TLS + persistent authority process, not a private-cloud/VM failover proof.

All keys, tokens, lanes and tasks are disposable fixtures. No existing service,
credential or DB is used. Worker A has no access to the authority's DB path.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.native_one_shot import NativeExecutorBlocked
from app.native_remote_runtime import (
    HttpsWorkerApi, HttpsWorkerRoute, PosixWorkerTokenFile,
)

RULES = "a" * 40
BASE = "b" * 40
TOKEN = "disposable-tls-fixture-token-not-operational"


def bounded_wait(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("disposable fixture did not reach expected state")


def stop_owned(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


@contextmanager
def tls_authority(tmp_path):
    openssl = shutil.which("openssl")
    assert openssl, "OpenSSL is required for the real TLS test, not a skipped test"
    ca = tmp_path / "fixture-cert.pem"
    key = tmp_path / "fixture-key.pem"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(ca), "-days", "1", "-sha256",
         "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1",
         "-addext", "basicConstraints=critical,CA:TRUE"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
    )
    key.chmod(0o600)
    token = tmp_path / "fixture-token"
    token.write_text(TOKEN, encoding="ascii")
    token.chmod(0o600)
    db = tmp_path / "authority.db"
    # Keep a bound listener open across process start; only our child inherits it.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k in {
        "PATH", "HOME", "SYSTEMROOT", "TEMP", "TMP", "LD_LIBRARY_PATH",
        "PYTHONHOME", "VIRTUAL_ENV",
    }}
    env.update(
        PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1",
        CODEX_WORKER_POOL_DB=str(db),
        CODEX_WORKER_POOL_API_TOKEN_FILE=str(token),
        CODEX_WORKER_POOL_EXPECTED_RULES_SHA=RULES,
        CODEX_WORKER_POOL_HEARTBEAT_TTL_SECONDS="600",
        CODEX_WORKER_POOL_LEASE_SECONDS="4",
        CODEX_WORKER_POOL_RECONCILE_INTERVAL_SECONDS="0.2",
        CODEX_WORKER_POOL_FENCED_CUTOVER_ATTESTED="1",
        CODEX_WORKER_POOL_PROGRESS_STALL_SECONDS="300",
    )
    server = subprocess.Popen(
        [sys.executable, "-B", "-m", "uvicorn", "app.main:app",
         "--fd", str(listener.fileno()), "--ssl-certfile", str(ca),
         "--ssl-keyfile", str(key), "--no-access-log", "--log-level", "error"],
        cwd=ROOT, env=env, pass_fds=(listener.fileno(),),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    listener.close()
    route = HttpsWorkerRoute(f"https://127.0.0.1:{port}", ca.read_text())
    admin = httpx.Client(base_url=route.origin, verify=route.ssl_context(),
                         trust_env=False, follow_redirects=False, timeout=2,
                         headers={"Authorization": "Bearer " + TOKEN})
    try:
        def ready():
            assert server.poll() is None, "isolated TLS authority exited"
            try:
                return admin.get("/health").status_code == 200
            except httpx.HTTPError:
                return False
        bounded_wait(ready)
        yield route, token, db, server, admin, env, ca
    finally:
        admin.close()
        stop_owned(server)


def worker_a(origin, ca_path, token_path, task_id, receipt_path):
    # This is test-only code, not a production launcher or a general CLI.
    assert re.fullmatch(r"https://127\.0\.0\.1:[1-9][0-9]{0,4}", origin)
    assert re.fullmatch(r"cwp-[0-9a-f]{24}", task_id)
    route = HttpsWorkerRoute(origin, Path(ca_path).read_text())
    with HttpsWorkerApi(route=route,
                        token_provider=PosixWorkerTokenFile(Path(token_path))) as api:
        claim = api.claim_fenced_exact("fixture-builder-a", task_id, "tls-fixture-claim-a")
        assert claim["claimed"] is True
        lease = claim["lease"]
        fence = {k: lease[k] for k in ("lease_generation", "attempt_id")}
        api.start(task_id, "fixture-builder-a", lease["lease_token"],
                  "tls-fixture-start-a", **fence)
        saved = api.record_checkpoint(
            task_id, "fixture-builder-a", lease["lease_token"],
            lease["lease_generation"], lease["attempt_id"],
            "tls-fixture-saved", 1, "prepared", "continue_fixture",
            "tls-fixture-checkpoint-a",
        )
        # The checkpoint is already committed remotely and read back before this
        # local test receipt. No DB/snapshot is ever exported by this worker.
        target = Path(receipt_path)
        with target.open("x", encoding="ascii") as output:
            os.chmod(target, 0o600)
            json.dump({"lease": lease, "checkpoint": saved}, output)
            output.flush()
            os.fsync(output.fileno())
        time.sleep(30)
        raise AssertionError("parent must interrupt this exact test child")


def test_real_tls_authority_survives_worker_process_loss(tmp_path):
    with tls_authority(tmp_path) as (route, token, db, server, admin, env, ca):
        lane = admin.post("/v1/repositories", json={
            "repository": "fixture/remote-continuity", "enabled": True,
            "max_in_flight": 1, "correlation_id": "tls-fixture-lane",
        })
        assert lane.status_code == 200
        for name, role in (("fixture-builder-a", "builder"),
                           ("fixture-builder-b", "builder"),
                           ("fixture-validator", "validator")):
            registered = admin.post("/v1/workers", json={
                "worker_id": name, "host": "disposable-test-process",
                "role": role, "profile": "NORMAL", "gateway_ok": True,
                "state_validated": True, "rules_sha": RULES,
                "worktree_root": str(tmp_path / name),
                "correlation_id": "tls-fixture-register-" + role,
            })
            assert registered.status_code == 200
        intent = {
            "repository": "fixture/remote-continuity", "issue_number": 1,
            "request_id": "tls-process-loss-fixture", "base_sha": BASE,
            "correlation_id": "tls-fixture-create", "fencing_required": True,
        }
        created = admin.post("/v1/tasks", json=intent)
        assert created.status_code == 201
        task_id = created.json()["task"]["task_id"]
        receipt = tmp_path / "worker-a-receipt.json"
        # A only receives HTTPS + credential references, never CODEX_WORKER_POOL_DB.
        child_env = dict(env)
        child_env.pop("CODEX_WORKER_POOL_DB")
        worker = subprocess.Popen(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--fixture-worker",
             route.origin, str(ca), str(token), task_id, str(receipt)],
            cwd=ROOT, env=child_env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            def committed():
                assert worker.poll() is None, "test worker exited before checkpoint"
                if not receipt.exists():
                    return False
                try:
                    return json.loads(receipt.read_text())
                except ValueError:
                    return False
            prior = bounded_wait(committed)
            assert prior["checkpoint"]["checkpoint_id"] == "tls-fixture-saved"
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as independent:
                assert independent.execute(
                    "SELECT state, lease_generation FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone() == ("running", 1)
            worker.kill()
            assert worker.wait(timeout=5) != 0
            assert server.poll() is None  # Authority survives, no restore/start.
            bounded_wait(lambda: admin.get(f"/v1/tasks/{task_id}").json()["state"] == "queued")
            with HttpsWorkerApi(route=route, token_provider=PosixWorkerTokenFile(token)) as api:
                acquired = api.claim_fenced_exact(
                    "fixture-builder-b", task_id, "tls-fixture-claim-b")
                assert acquired["claimed"] is True
                lease_b = acquired["lease"]
                assert lease_b["lease_generation"] == 2
                assert acquired["recovery_context"]["latest_checkpoint"][
                    "checkpoint_id"] == "tls-fixture-saved"
                old = prior["lease"]
                with pytest.raises(NativeExecutorBlocked, match="^worker_http_unverified$"):
                    api.start(task_id, "fixture-builder-a", old["lease_token"],
                              "tls-fixture-stale-a", lease_generation=1,
                              attempt_id=old["attempt_id"])
                assert admin.get(f"/v1/tasks/{task_id}").json()["leased_by"] == "fixture-builder-b"
                fence_b = {k: lease_b[k] for k in ("lease_generation", "attempt_id")}
                api.start(task_id, "fixture-builder-b", lease_b["lease_token"],
                          "tls-fixture-start-b", **fence_b)
                api.submit_validation(task_id, "fixture-builder-b", lease_b["lease_token"],
                                      "c" * 40, "tls-fixture-validate", **fence_b)
                claim_v = api.claim_fenced_exact(
                    "fixture-validator", task_id, "tls-fixture-claim-v")
                assert claim_v["claimed"] is True
                lease_v = claim_v["lease"]
                assert lease_v["lease_generation"] == 3
                api.complete(task_id, "fixture-validator", lease_v["lease_token"],
                             "tls-fixture-complete",
                             lease_generation=3, attempt_id=lease_v["attempt_id"])
                assert api.get_task(task_id)["state"] == "completed"
                assert api.claim_fenced_exact(
                    "fixture-builder-b", task_id, "tls-fixture-no-duplicate")["claimed"] is False
            replay = admin.post("/v1/tasks", json=intent)
            assert replay.status_code == 200
            assert replay.json()["task"]["task_id"] == task_id
            assert admin.get(f"/v1/tasks/{task_id}").json()["state"] == "completed"
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as independent:
                assert independent.execute(
                    "SELECT state, lease_generation, produced_sha FROM tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone() == ("completed", 3, "c" * 40)
                assert independent.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
            print("REMOTE_TLS_PROCESS_LOSS_PROVEN generations=1,2,3 tasks=1 "
                  "authority_restarted=false snapshot_exported=false private_s3_verified=false")
        finally:
            stop_owned(worker)


def test_real_tls_certificate_and_authentication_negative_controls(tmp_path):
    with tls_authority(tmp_path) as (route, token, db, server, admin, env, ca):
        task_id = "cwp-" + "1" * 24
        # Public trust roots do not trust the disposable self-signed CA.
        with HttpsWorkerApi(route=HttpsWorkerRoute(route.origin),
                            token_provider=PosixWorkerTokenFile(token)) as untrusted:
            with pytest.raises(NativeExecutorBlocked, match="^worker_transport_unverified$"):
                untrusted.get_task(task_id)
        # Valid trust but wrong bearer must also fail without creating/claiming.
        with HttpsWorkerApi(route=route,
                            token_provider=lambda: "wrong-disposable-token-value") as bad_auth:
            with pytest.raises(NativeExecutorBlocked, match="^worker_auth_rejected$"):
                bad_auth.get_task(task_id)
        # Trust is correct but certificate does not contain DNS SAN localhost.
        wrong_name = HttpsWorkerRoute(
            route.origin.replace("127.0.0.1", "localhost"), route.ca_pem)
        with HttpsWorkerApi(route=wrong_name,
                            token_provider=PosixWorkerTokenFile(token)) as wrong_host:
            with pytest.raises(NativeExecutorBlocked, match="^worker_transport_unverified$"):
                wrong_host.get_task(task_id)
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as independent:
            assert independent.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        assert server.poll() is None


if __name__ == "__main__":
    assert len(sys.argv) == 7 and sys.argv[1] == "--fixture-worker"
    worker_a(*sys.argv[2:])
