"""Immutable, per-task eligibility preflight (not a claim or reconciliation).

Reads the existing Worker Pool SQLite database through a mode=ro connection.
The coordinator's background reconciler is independent and may change state
concurrently; this endpoint never requests reconciliation or mutates records.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

TASK_ID_PATTERN = re.compile(r"^cwp-[0-9a-f]{24}$")
ACTIVE = ("leased", "running", "validating")


class TaskDiagnosticNotFound(Exception):
    pass


class TaskDiagnosticUnavailable(Exception):
    pass


def _age_seconds(value: str | None, now: datetime) -> float | None:
    try:
        if not value:
            return None
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return None
        return round(max(0.0, (now - timestamp.astimezone(timezone.utc)).total_seconds()), 3)
    except (ValueError, TypeError, OverflowError):
        return None


def read_task_diagnostics(
    db_path: str | Path,
    task_id: str,
    *,
    expected_rules_sha: str | None,
    heartbeat_ttl_seconds: int,
    progress_stall_seconds: int,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Read one task, current lane and worker eligibility without any DB writes.

    A worker's precheck eligibility is not proof that a claim has occurred or that
    the scheduling algorithm would assign this specific task next.
    """
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError("task_id inválido")
    if heartbeat_ttl_seconds <= 0 or progress_stall_seconds <= 0:
        raise ValueError("timeouts inválidos")

    db = None
    try:
        # SQLite's mode=ro prevents implicit DB creation and ALL SQL writes.
        # Unlike WorkerPoolStore._db, this does not set journal_mode=WAL.
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        task = db.execute(
            """SELECT task_id,repository,issue_number,state,base_sha,produced_sha,
                      leased_by,lease_expires_at,attempt_count,max_attempts,
                      blocked_reason,updated_at,last_material_progress_at,
                      builder_worker_id,validator_worker_id
               FROM tasks WHERE task_id=?""", (task_id,),
        ).fetchone()
        if task is None:
            raise TaskDiagnosticNotFound("task_not_found")
        lane = db.execute(
            "SELECT enabled,max_in_flight FROM repository_lanes WHERE repository=?",
            (task["repository"],),
        ).fetchone()
        workers = db.execute(
            """SELECT worker_id,role,profile,gateway_ok,state_validated,
                      rules_sha,last_heartbeat_at
               FROM workers WHERE role IN ('builder','validator')
               ORDER BY role,worker_id"""
        ).fetchall()
        affinities = db.execute(
            "SELECT worker_id,repository FROM worker_repository_affinity ORDER BY worker_id",
        ).fetchall()
        busy = db.execute(
            """SELECT leased_by FROM tasks WHERE leased_by IS NOT NULL
               AND state IN ('leased','running','validating')"""
        ).fetchall()
        active_in_lane = db.execute(
            """SELECT COUNT(*) AS n FROM tasks
               WHERE repository=? AND state IN ('leased','running','validating')""",
            (task["repository"],),
        ).fetchone()["n"]
        db.rollback()  # End the read transaction. No writes can have occurred.
    except TaskDiagnosticNotFound:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise TaskDiagnosticUnavailable("task_diagnostics_unavailable") from exc
    finally:
        if db is not None:
            db.close()

    now = clock().astimezone(timezone.utc)
    state = str(task["state"])
    lease_active = task["leased_by"] is not None
    required_role = (
        "builder" if state == "queued" and not lease_active else
        "validator" if state == "validating" and not lease_active else None
    )
    progress_age = _age_seconds(task["last_material_progress_at"], now)
    lease_expired = False
    if task["lease_expires_at"]:
        try:
            lease_expired = datetime.fromisoformat(
                str(task["lease_expires_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc) <= now
        except (ValueError, TypeError, OverflowError):
            lease_expired = False
    affinity_map: dict[str, set[str]] = {}
    for item in affinities:
        affinity_map.setdefault(item["worker_id"], set()).add(item["repository"])
    busy_ids = {row["leased_by"] for row in busy}
    views: list[dict[str, Any]] = []
    for worker in workers:
        reasons: list[str] = []
        age = _age_seconds(worker["last_heartbeat_at"], now)
        if age is None or age > heartbeat_ttl_seconds:
            reasons.append("offline_or_stale")
        if worker["profile"] != "NORMAL":
            reasons.append("profile_not_normal")
        if not worker["gateway_ok"] or not worker["state_validated"]:
            reasons.append("governance_not_ready")
        if expected_rules_sha and worker["rules_sha"] != expected_rules_sha:
            reasons.append("rules_sha_mismatch")
        if worker["worker_id"] in busy_ids:
            reasons.append("worker_busy")
        allowed_repos = affinity_map.get(worker["worker_id"], set())
        if allowed_repos and task["repository"] not in allowed_repos:
            reasons.append("repository_affinity_mismatch")
        if lane is None or not lane["enabled"]:
            reasons.append("lane_disabled_or_missing")
        if worker["role"] == "builder" and lane and active_in_lane >= lane["max_in_flight"]:
            reasons.append("lane_capacity_reached")
        if worker["role"] == "validator" and worker["worker_id"] == task["builder_worker_id"]:
            reasons.append("validator_same_as_builder")
        if worker["role"] != required_role:
            reasons.append("role_not_required_for_task_state")
        views.append({
            "worker_id": worker["worker_id"],
            "role": worker["role"],
            "profile": worker["profile"],
            "heartbeat_age_seconds": age,
            "gateway_ok": bool(worker["gateway_ok"]),
            "state_validated": bool(worker["state_validated"]),
            "rules_sha_matches": bool(not expected_rules_sha or worker["rules_sha"] == expected_rules_sha),
            "eligibility_precheck": len(reasons) == 0,
            "why_idle": reasons[0] if reasons else "available_precheck_only",
            "reasons": reasons,
        })
    return {
        "schema_version": "1.0.0",
        "read_mode": "sqlite_mode_ro",
        "observed_at": now.isoformat().replace("+00:00", "Z"),
        "task": {
            "task_id": task["task_id"],
            "repository": task["repository"],
            "issue_number": task["issue_number"],
            "state": state,
            "base_sha": task["base_sha"],
            "produced_sha": task["produced_sha"],
            "leased_by": task["leased_by"],
            "lease_expires_at": task["lease_expires_at"],
            "lease_expired_observed": lease_expired,
            "attempt_count": task["attempt_count"],
            "max_attempts": task["max_attempts"],
            "blocked_reason": (
                task["blocked_reason"] if task["blocked_reason"] and
                re.fullmatch(r"[a-z][a-z0-9_:-]{0,127}", str(task["blocked_reason"]))
                else "redacted" if task["blocked_reason"] else None
            ),
            "updated_at": task["updated_at"],
            "last_material_progress_at": task["last_material_progress_at"],
            "progress_age_seconds": progress_age,
            "progress_stalled_observed": bool(
                state in ACTIVE and progress_age is not None and progress_age >= progress_stall_seconds
            ),
        },
        "required_role": required_role,
        "lane": {
            "configured": lane is not None,
            "enabled": bool(lane["enabled"]) if lane else False,
            "max_in_flight": lane["max_in_flight"] if lane else None,
            "active_count": active_in_lane,
        },
        "workers": views,
        "precheck_eligible_workers": sum(x["eligibility_precheck"] for x in views),
        "claim_performed": False,
        "reconciliation_requested": False,
    }
