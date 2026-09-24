from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from app.store import ConflictError, NotFoundError, WorkerPoolStore, iso, now_utc, sanitize


SHA40 = re.compile(r"^[0-9a-f]{40}$")
MERGE_POLICIES = {"governed"}


class WorkOrchestrator:
    """Persistent facade over the existing Worker Pool lifecycle."""

    def __init__(
        self,
        db_path: str | Path,
        pool_store: WorkerPoolStore,
        *,
        clock: Callable = now_utc,
    ) -> None:
        self.db_path = Path(db_path)
        self.pool_store = pool_store
        self.clock = clock
        self._init()

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        db = self._db()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init(self) -> None:
        db = self._db()
        try:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_runs(
                  work_id TEXT PRIMARY KEY,
                  task_id TEXT NOT NULL UNIQUE,
                  merge_policy TEXT NOT NULL CHECK(merge_policy IN ('governed')),
                  evidence_state TEXT NOT NULL DEFAULT 'pending'
                    CHECK(evidence_state IN ('pending','verified')),
                  merge_state TEXT NOT NULL DEFAULT 'pending'
                    CHECK(merge_state IN ('pending','ready','blocked','merged')),
                  validation_run_id TEXT,
                  validation_sha TEXT,
                  evidence_reference TEXT,
                  independent_readback INTEGER NOT NULL DEFAULT 0,
                  positive_control INTEGER NOT NULL DEFAULT 0,
                  negative_control INTEGER NOT NULL DEFAULT 0,
                  expected_head_sha TEXT,
                  merge_commit_sha TEXT,
                  merge_reason TEXT,
                  correlation_id TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  merged_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_work_task ON work_runs(task_id);
                CREATE INDEX IF NOT EXISTS idx_work_merge_state ON work_runs(merge_state,updated_at);
                """
            )
        finally:
            db.close()

    @staticmethod
    def _work_id(task: dict[str, Any]) -> str:
        return f"work-{task['idempotency_key'][:24]}"

    @staticmethod
    def _public_task(task: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in task.items() if key != "lease_token"}

    def _row(self, work_id: str) -> dict[str, Any]:
        db = self._db()
        try:
            row = db.execute(
                "SELECT * FROM work_runs WHERE work_id=?",
                (work_id,),
            ).fetchone()
        finally:
            db.close()
        if row is None:
            raise NotFoundError(f"work não encontrado: {work_id}")
        return dict(row)

    @staticmethod
    def _phase(task: dict[str, Any], row: dict[str, Any]) -> str:
        if row["merge_state"] == "merged":
            return "merged"
        if row["merge_state"] == "blocked":
            return "merge_blocked"
        if row["evidence_state"] == "verified":
            return "merge"
        state = task["state"]
        if state == "queued":
            return "queue"
        if state in {"leased", "running"}:
            return "worker"
        if state == "validating":
            return "validator"
        if state == "completed":
            return "evidence"
        return state

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        task = self.pool_store.get_task(row["task_id"])
        return {
            "work_id": row["work_id"],
            "task_id": row["task_id"],
            "phase": self._phase(task, row),
            "merge_policy": row["merge_policy"],
            "correlation_id": row["correlation_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "task": self._public_task(task),
            "evidence": {
                "state": row["evidence_state"],
                "validation_run_id": row["validation_run_id"],
                "validation_sha": row["validation_sha"],
                "evidence_reference": row["evidence_reference"],
                "independent_readback": bool(row["independent_readback"]),
                "positive_control": bool(row["positive_control"]),
                "negative_control": bool(row["negative_control"]),
            },
            "merge": {
                "state": row["merge_state"],
                "expected_head_sha": row["expected_head_sha"],
                "merge_commit_sha": row["merge_commit_sha"],
                "reason": row["merge_reason"],
                "merged_at": row["merged_at"],
            },
        }

    def create_work(
        self,
        *,
        repository: str,
        issue_number: int,
        request_id: str,
        correlation_id: str,
        base_sha: str,
        priority: int = 100,
        target_branch: str | None = None,
        max_attempts: int | None = None,
        merge_policy: str = "governed",
    ) -> tuple[dict[str, Any], bool]:
        if merge_policy not in MERGE_POLICIES:
            raise ValueError("merge_policy inválida")
        task, _task_created = self.pool_store.enqueue_task(
            repository=repository,
            issue_number=issue_number,
            request_id=request_id,
            correlation_id=correlation_id,
            priority=priority,
            base_sha=base_sha,
            target_branch=target_branch,
            max_attempts=max_attempts,
        )
        work_id = self._work_id(task)
        stamp = iso(self.clock())
        with self._tx() as db:
            existing = db.execute(
                "SELECT * FROM work_runs WHERE work_id=?",
                (work_id,),
            ).fetchone()
            created = existing is None
            if created:
                db.execute(
                    """INSERT INTO work_runs(
                         work_id,task_id,merge_policy,correlation_id,created_at,updated_at)
                       VALUES(?,?,?,?,?,?)""",
                    (work_id, task["task_id"], merge_policy, correlation_id, stamp, stamp),
                )
            elif existing["task_id"] != task["task_id"] or existing["merge_policy"] != merge_policy:
                raise ConflictError("work divergente no replay")
        return self.get_work(work_id), created

    def get_work(self, work_id: str) -> dict[str, Any]:
        return self._view(self._row(work_id))

    def record_evidence(
        self,
        *,
        work_id: str,
        validation_run_id: str,
        validation_sha: str,
        evidence_reference: str,
        independent_readback: bool,
        positive_control: bool,
        negative_control: bool,
        correlation_id: str,
    ) -> dict[str, Any]:
        validation_sha = validation_sha.strip().lower()
        if not SHA40.fullmatch(validation_sha):
            raise ValueError("validation_sha inválido")
        if not validation_run_id.strip() or not evidence_reference.strip():
            raise ValueError("identidade da evidência inválida")
        if not (independent_readback and positive_control and negative_control):
            raise ConflictError("controles de evidência incompletos")

        row = self._row(work_id)
        task = self.pool_store.get_task(row["task_id"])
        if task["state"] != "completed" or not task.get("produced_sha"):
            raise ConflictError("task ainda não está validada")
        if validation_sha != str(task["produced_sha"]).lower():
            raise ConflictError("validation_sha divergente do produced_sha")

        if row["evidence_state"] == "verified":
            same = (
                row["validation_run_id"] == validation_run_id.strip()
                and row["validation_sha"] == validation_sha
                and row["evidence_reference"] == evidence_reference.strip()
                and bool(row["independent_readback"]) is independent_readback
                and bool(row["positive_control"]) is positive_control
                and bool(row["negative_control"]) is negative_control
            )
            if not same:
                raise ConflictError("evidência divergente no replay")
            return self._view(row)

        stamp = iso(self.clock())
        with self._tx() as db:
            db.execute(
                """UPDATE work_runs
                   SET evidence_state='verified',merge_state='ready',
                       validation_run_id=?,validation_sha=?,evidence_reference=?,
                       independent_readback=1,positive_control=1,negative_control=1,
                       expected_head_sha=?,correlation_id=?,updated_at=?
                   WHERE work_id=? AND evidence_state='pending'""",
                (
                    validation_run_id.strip(),
                    validation_sha,
                    evidence_reference.strip(),
                    validation_sha,
                    correlation_id.strip(),
                    stamp,
                    work_id,
                ),
            )
        return self.get_work(work_id)

    def record_merge_result(
        self,
        *,
        work_id: str,
        expected_head_sha: str,
        merged: bool,
        correlation_id: str,
        merge_commit_sha: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        expected_head_sha = expected_head_sha.strip().lower()
        if not SHA40.fullmatch(expected_head_sha):
            raise ValueError("expected_head_sha inválido")

        row = self._row(work_id)
        task = self.pool_store.get_task(row["task_id"])
        produced_sha = str(task.get("produced_sha") or "").lower()
        if row["evidence_state"] != "verified" or row["merge_state"] == "pending":
            raise ConflictError("evidência verificada obrigatória antes do merge")
        if expected_head_sha != produced_sha or expected_head_sha != row["expected_head_sha"]:
            raise ConflictError("expected_head_sha divergente da evidência")

        normalized_commit = (merge_commit_sha or "").strip().lower() or None
        normalized_reason = sanitize((reason or "").strip()) or None
        if merged:
            if not normalized_commit or not SHA40.fullmatch(normalized_commit):
                raise ValueError("merge_commit_sha obrigatório e válido quando merged=true")
            next_state = "merged"
        else:
            if normalized_commit is not None:
                raise ValueError("merge_commit_sha deve ser omitido quando merged=false")
            if not normalized_reason:
                raise ValueError("reason obrigatório quando merged=false")
            next_state = "blocked"

        if row["merge_state"] == "merged":
            if not merged or row["merge_commit_sha"] != normalized_commit:
                raise ConflictError("resultado de merge divergente no replay")
            return self._view(row)
        if row["merge_state"] == "blocked" and not merged:
            if row["merge_reason"] != normalized_reason:
                raise ConflictError("bloqueio de merge divergente no replay")
            return self._view(row)

        stamp = iso(self.clock())
        with self._tx() as db:
            db.execute(
                """UPDATE work_runs
                   SET merge_state=?,merge_commit_sha=?,merge_reason=?,
                       correlation_id=?,updated_at=?,merged_at=?
                   WHERE work_id=?""",
                (
                    next_state,
                    normalized_commit,
                    normalized_reason,
                    correlation_id.strip(),
                    stamp,
                    stamp if merged else None,
                    work_id,
                ),
            )
        return self.get_work(work_id)
