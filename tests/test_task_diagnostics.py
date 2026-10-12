"""Read-only focused preflight tests independent of live host."""

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.task_diagnostics import (
    TaskDiagnosticNotFound,
    TaskDiagnosticUnavailable,
    read_task_diagnostics,
)

NOW = datetime(2026, 10, 12, 21, 0, tzinfo=timezone.utc)
TASK_ID = "cwp-" + "a" * 24
REPO = "ericson-j-santos/reqsys-v2-enterprise-real"


def stamp(delta: int = 0) -> str:
    return (NOW + timedelta(seconds=delta)).isoformat().replace("+00:00", "Z")


def fixture_db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE tasks(task_id TEXT PRIMARY KEY,repository TEXT,issue_number INTEGER,
              state TEXT,base_sha TEXT,produced_sha TEXT,leased_by TEXT,lease_expires_at TEXT,
              attempt_count INTEGER,max_attempts INTEGER,blocked_reason TEXT,updated_at TEXT,
              last_material_progress_at TEXT,builder_worker_id TEXT,validator_worker_id TEXT);
            CREATE TABLE repository_lanes(repository TEXT,enabled INTEGER,max_in_flight INTEGER);
            CREATE TABLE workers(worker_id TEXT,role TEXT,profile TEXT,gateway_ok INTEGER,
              state_validated INTEGER,rules_sha TEXT,last_heartbeat_at TEXT);
            CREATE TABLE worker_repository_affinity(worker_id TEXT,repository TEXT);
        """)
        db.execute("INSERT INTO repository_lanes VALUES(?,1,1)", (REPO,))
        db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (TASK_ID, REPO, 2546, "queued", "1" * 40, None, None, None, 0, 3, None,
                    stamp(), stamp(), None, None))
        db.execute("INSERT INTO workers VALUES(?,?,?,?,?,?,?)",
                   ("desktop-builder", "builder", "NORMAL", 1, 1, "a" * 40, stamp()))
        db.execute("INSERT INTO workers VALUES(?,?,?,?,?,?,?)",
                   ("noteri-validator", "validator", "NORMAL", 1, 1, "a" * 40, stamp()))


def read(path: Path):
    return read_task_diagnostics(
        path, TASK_ID, expected_rules_sha="a" * 40, heartbeat_ttl_seconds=60,
        progress_stall_seconds=300, clock=lambda: NOW,
    )


def test_read_only_no_mutation_and_replay(tmp_path: Path):
    path = tmp_path / "pool.db"
    fixture_db(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    first, second = read(path), read(path)
    assert first == second
    assert first["task"]["state"] == "queued"
    assert first["required_role"] == "builder"
    assert first["precheck_eligible_workers"] == 1
    assert first["workers"][0]["eligibility_precheck"] is True
    assert first["workers"][1]["eligibility_precheck"] is False
    assert "lease_token" not in str(first)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert first["read_mode"] == "sqlite_mode_ro"


def test_explains_missing_and_ineligible_workers(tmp_path: Path):
    path = tmp_path / "pool.db"
    fixture_db(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE repository_lanes SET enabled=0")
        db.execute("UPDATE workers SET profile='ESTUDO', gateway_ok=0, last_heartbeat_at=?",
                   (stamp(-300),))
        db.execute("INSERT INTO worker_repository_affinity VALUES(?,?)",
                   ("desktop-builder", "another/repo"))
    result = read(path)
    assert result["precheck_eligible_workers"] == 0
    assert result["lane"]["enabled"] is False
    reasons = result["workers"][0]["reasons"]
    assert "offline_or_stale" in reasons
    assert "profile_not_normal" in reasons
    assert "governance_not_ready" in reasons
    assert "repository_affinity_mismatch" in reasons
    assert "lane_disabled_or_missing" in reasons


def test_observes_expired_lease_without_recovery(tmp_path: Path):
    path = tmp_path / "pool.db"
    fixture_db(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE tasks SET state='running',leased_by='desktop-builder',lease_expires_at=?,last_material_progress_at=?",
                   (stamp(-1), stamp(-301)))
    before = path.read_bytes()
    result = read(path)
    assert result["task"]["state"] == "running"
    assert result["task"]["lease_expired_observed"] is True
    assert result["task"]["progress_stalled_observed"] is True
    assert result["claim_performed"] is False
    assert result["reconciliation_requested"] is False
    assert result["required_role"] is None
    assert path.read_bytes() == before


def test_bad_id_missing_task_and_missing_database_fail_closed(tmp_path: Path):
    path = tmp_path / "pool.db"
    fixture_db(path)
    with pytest.raises(ValueError, match="task_id"):
        read_task_diagnostics(path, "../../data.db", expected_rules_sha=None,
                              heartbeat_ttl_seconds=60, progress_stall_seconds=300)
    with pytest.raises(TaskDiagnosticNotFound):
        read_task_diagnostics(path, "cwp-" + "b" * 24, expected_rules_sha=None,
                              heartbeat_ttl_seconds=60, progress_stall_seconds=300)
    missing = tmp_path / "missing.db"
    with pytest.raises(TaskDiagnosticUnavailable):
        read(missing)
    assert not missing.exists()


def test_sanitized_block_reason_and_sha_mismatch(tmp_path: Path):
    path = tmp_path / "pool.db"
    fixture_db(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE tasks SET state='blocked',blocked_reason=?",
                   ("unsafe free form reason!",))
        db.execute("UPDATE workers SET rules_sha=? WHERE role='builder'", ("b" * 40,))
    result = read(path)
    assert result["task"]["blocked_reason"] == "redacted"
    assert result["precheck_eligible_workers"] == 0
    assert "unsafe free form reason!" not in str(result)
    assert "rules_sha_mismatch" in result["workers"][0]["reasons"]


def test_authenticated_http_diagnostics_without_reconciliation(tmp_path: Path, monkeypatch):
    """Real FastAPI route reads one queued task without invoking recovery."""
    import importlib
    import sys

    from fastapi.testclient import TestClient

    db_path = tmp_path / "worker-pool.db"
    token_path = tmp_path / "api-token"
    token_path.write_text("fixture-authorization-value\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_WORKER_POOL_DB", str(db_path))
    monkeypatch.setenv("CODEX_WORKER_POOL_API_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("CODEX_WORKER_POOL_EXPECTED_RULES_SHA", "a" * 40)
    monkeypatch.setenv("CODEX_WORKER_POOL_HEARTBEAT_TTL_SECONDS", "60")
    sys.modules.pop("app.main", None)
    api = importlib.import_module("app.main")
    client = TestClient(api.app)
    task, created = api.store.enqueue_task(
        repository=REPO,
        issue_number=2546,
        request_id="diagnostics-api-only",
        correlation_id="diagnostics-api-test",
        base_sha="1" * 40,
    )
    assert created is True
    api.store.register_worker(
        worker_id="test-ready-builder",
        host="test-host",
        role="builder",
        profile="NORMAL",
        rules_sha="a" * 40,
        gateway_ok=True,
        state_validated=True,
        correlation_id="api-ready",
    )
    task_id = task["task_id"]

    def must_not_recover(*args, **kwargs):
        raise AssertionError("read-only diagnostic called recovery")

    monkeypatch.setattr(api.store, "recover_expired_leases", must_not_recover)
    monkeypatch.setattr(api.store, "recover_stalled_tasks", must_not_recover)
    headers = {"Authorization": "Bearer fixture-authorization-value"}

    # Invalid callers must not see private worker/task state.
    assert client.get(f"/v1/tasks/{task_id}/diagnostics").status_code == 401
    assert client.get(
        f"/v1/tasks/{task_id}/diagnostics",
        headers={"Authorization": "Bearer invalid"},
    ).status_code == 401

    with sqlite3.connect(db_path) as db:
        before = db.execute(
            "SELECT state,attempt_count,leased_by,updated_at FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()

    good = client.get(f"/v1/tasks/{task_id}/diagnostics", headers=headers)
    assert good.status_code == 200
    payload = good.json()
    assert payload["task"]["task_id"] == task_id
    assert payload["task"]["state"] == "queued"
    assert payload["required_role"] == "builder"
    assert payload["precheck_eligible_workers"] == 1
    assert payload["claim_performed"] is False
    assert payload["reconciliation_requested"] is False
    assert payload["read_mode"] == "sqlite_mode_ro"
    assert payload["workers"][0]["why_idle"] == "available_precheck_only"
    assert "lease_token" not in str(payload)
    assert "fixture-authorization-value" not in str(payload)

    replay = client.get(f"/v1/tasks/{task_id}/diagnostics", headers=headers)
    assert replay.status_code == 200
    assert replay.json()["task"] == payload["task"]

    assert client.get(
        "/v1/tasks/cwp-" + "f" * 24 + "/diagnostics", headers=headers
    ).status_code == 404
    assert client.get("/v1/tasks/not-a-task/diagnostics", headers=headers).status_code == 422

    with sqlite3.connect(db_path) as db:
        after = db.execute(
            "SELECT state,attempt_count,leased_by,updated_at FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
    assert after == before == ("queued", 0, None, before[3])
