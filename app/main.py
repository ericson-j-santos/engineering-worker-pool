from __future__ import annotations

import hmac
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.contract import CONTRACT_VERSION, descriptor as contract_descriptor
from app.reconcile import ReconciliationController
from app.store import ConflictError, NotFoundError, WorkerPoolStore
from app.task_diagnostics import (
    TaskDiagnosticNotFound,
    TaskDiagnosticUnavailable,
    read_task_diagnostics,
)
from app.work import WorkOrchestrator


SERVICE_NAME = "codex-worker-pool"
DB_PATH = Path(os.getenv("CODEX_WORKER_POOL_DB", "./data/codex-worker-pool.db"))
TOKEN_FILE = os.getenv("CODEX_WORKER_POOL_API_TOKEN_FILE", "").strip()
RECONCILIATION_TOKEN_FILE = os.getenv(
    "CODEX_WORKER_POOL_RECONCILIATION_TOKEN_FILE", ""
).strip()
HEARTBEAT_TTL_SECONDS = int(os.getenv("CODEX_WORKER_POOL_HEARTBEAT_TTL_SECONDS", "90"))
LEASE_SECONDS = int(os.getenv("CODEX_WORKER_POOL_LEASE_SECONDS", "120"))
MAX_ATTEMPTS = int(os.getenv("CODEX_WORKER_POOL_MAX_ATTEMPTS", "3"))
PROGRESS_STALL_SECONDS = int(os.getenv("CODEX_WORKER_POOL_PROGRESS_STALL_SECONDS", "300"))
RECONCILE_INTERVAL_SECONDS = float(os.getenv("CODEX_WORKER_POOL_RECONCILE_INTERVAL_SECONDS", "5"))
EXPECTED_RULES_SHA = os.getenv("CODEX_WORKER_POOL_EXPECTED_RULES_SHA", "").strip().lower() or None
FENCED_CUTOVER_ATTESTED = (
    os.getenv("CODEX_WORKER_POOL_FENCED_CUTOVER_ATTESTED", "").strip() == "1"
)

store = WorkerPoolStore(
    DB_PATH,
    heartbeat_ttl_seconds=HEARTBEAT_TTL_SECONDS,
    default_lease_seconds=LEASE_SECONDS,
    default_max_attempts=MAX_ATTEMPTS,
    progress_stall_seconds=PROGRESS_STALL_SECONDS,
    expected_rules_sha=EXPECTED_RULES_SHA,
)
if FENCED_CUTOVER_ATTESTED:
    store.enable_fenced_cutover(
        correlation_id="runtime-fenced-cutover-attestation",
        legacy_claimers_drained=True,
        shared_queue_exclusive=True,
    )
work_orchestrator = WorkOrchestrator(DB_PATH, store)
reconciliation_controller = ReconciliationController(
    store,
    interval_seconds=RECONCILE_INTERVAL_SECONDS,
)

logger = logging.getLogger(SERVICE_NAME)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await reconciliation_controller.start()
    try:
        yield
    finally:
        await reconciliation_controller.stop()


app = FastAPI(
    title="Engineering Worker Pool",
    version="1.4.0",
    description="Fila governada para workers Codex distribuídos com lease, idempotência e validação independente.",
    lifespan=lifespan,
)


def _audit(event: str, correlation_id: str, **fields: Any) -> None:
    payload = {
        "service": SERVICE_NAME,
        "event": event,
        "correlation_id": correlation_id,
        **fields,
    }
    logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _read_api_token() -> str:
    if not TOKEN_FILE:
        return ""
    path = Path(TOKEN_FILE)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return token


def _read_reconciliation_token() -> str:
    if not RECONCILIATION_TOKEN_FILE:
        return ""
    path = Path(RECONCILIATION_TOKEN_FILE)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return token


def _reconciliation_auth_ready() -> bool:
    reconciliation_token = _read_reconciliation_token()
    api_token = _read_api_token()
    return bool(
        reconciliation_token
        and api_token
        and not hmac.compare_digest(reconciliation_token, api_token)
    )


def require_auth(authorization: str | None = Header(default=None)) -> None:
    expected = _read_api_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="worker_pool_auth_not_configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def require_reconciliation_auth(
    authorization: str | None = Header(default=None),
) -> None:
    expected = _read_reconciliation_token()
    if not expected or not _reconciliation_auth_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="reconciliation_auth_not_configured_or_not_separated",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    supplied = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def correlation(value: str | None) -> str:
    return (value or "").strip() or str(uuid.uuid4())


def public_task(task: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in task.items() if key != "lease_token"}
    if "fencing_required" in result:
        result["fencing_required"] = bool(result["fencing_required"])
    return result


class WorkerRegistration(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    host: str = Field(min_length=1, max_length=128)
    role: Literal["builder", "validator"]
    profile: Literal["NORMAL", "ESTUDO"] = "NORMAL"
    capacity_score: int = Field(default=50, ge=0, le=100)
    controller_version: str | None = Field(default=None, max_length=64)
    rules_sha: str | None = Field(default=None, max_length=64)
    gateway_ok: bool
    state_validated: bool
    worktree_root: str | None = Field(default=None, max_length=512)
    correlation_id: str = Field(min_length=1, max_length=128)


class Heartbeat(BaseModel):
    correlation_id: str = Field(min_length=1, max_length=128)
    profile: Literal["NORMAL", "ESTUDO"] | None = None
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    memory_percent: float | None = Field(default=None, ge=0, le=100)
    gateway_ok: bool | None = None
    state_validated: bool | None = None


class RepositoryLaneConfig(BaseModel):
    repository: str = Field(min_length=3, max_length=256)
    enabled: bool = True
    max_in_flight: int = Field(default=1, ge=1, le=100)
    correlation_id: str = Field(min_length=1, max_length=128)


class WorkerAffinityUpdate(BaseModel):
    repositories: list[str] = Field(default_factory=list, max_length=100)
    correlation_id: str = Field(min_length=1, max_length=128)


class TaskCreate(BaseModel):
    repository: str = Field(min_length=3, max_length=256)
    issue_number: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=256)
    correlation_id: str = Field(min_length=1, max_length=128)
    priority: int = Field(default=100, ge=0, le=10000)
    base_sha: str = Field(min_length=40, max_length=40)
    target_branch: str | None = Field(default=None, max_length=200)
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    fencing_required: bool = False


class ClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=128)
    lease_seconds: int | None = Field(default=None, ge=1, le=3600)


class ExactClaimRequest(ClaimRequest):
    task_id: str = Field(min_length=28, max_length=28, pattern=r"^cwp-[0-9a-f]{24}$")


class LeaseMutation(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    lease_token: str = Field(min_length=16, max_length=128)
    correlation_id: str = Field(min_length=1, max_length=128)
    lease_generation: int | None = Field(default=None, ge=1)
    attempt_id: str | None = Field(
        default=None, min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )


class RenewLease(LeaseMutation):
    lease_seconds: int | None = Field(default=None, ge=1, le=3600)


class ValidationHandoff(LeaseMutation):
    produced_sha: str = Field(min_length=40, max_length=40)


class Failure(LeaseMutation):
    reason: str = Field(min_length=1, max_length=1000)


class CheckpointRecord(LeaseMutation):
    lease_generation: int = Field(ge=1)
    attempt_id: str = Field(
        min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    checkpoint_id: str = Field(
        min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    sequence: int = Field(ge=1)
    stage: str = Field(
        min_length=1, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    artifact_kind: str | None = Field(
        default=None, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    artifact_ref: str | None = Field(default=None, max_length=1000)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{64}$")
    next_action: str = Field(min_length=1, max_length=1000)
    effect_id: str | None = Field(
        default=None, max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    effect_state: Literal["none", "pending", "confirmed", "not_applied", "unknown"] = "none"
    effect_result_ref: str | None = Field(default=None, max_length=1000)


class EffectReconciliation(BaseModel):
    lease_generation: int = Field(ge=1)
    outcome: Literal["confirmed", "not_applied"]
    readback_ref: str = Field(min_length=1, max_length=1000)
    correlation_id: str = Field(min_length=1, max_length=128)


class Requeue(BaseModel):
    correlation_id: str = Field(min_length=1, max_length=128)


class WorkCreate(TaskCreate):
    merge_policy: Literal["governed"] = "governed"
    fencing_required: Literal[False] = False


class EvidenceRecord(BaseModel):
    validation_run_id: str = Field(min_length=1, max_length=256)
    validation_sha: str = Field(min_length=40, max_length=40)
    evidence_reference: str = Field(min_length=1, max_length=1000)
    independent_readback: bool
    positive_control: bool
    negative_control: bool
    correlation_id: str = Field(min_length=1, max_length=128)


class MergeResult(BaseModel):
    expected_head_sha: str = Field(min_length=40, max_length=40)
    merged: bool
    merge_commit_sha: str | None = Field(default=None, min_length=40, max_length=40)
    reason: str | None = Field(default=None, max_length=1000)
    correlation_id: str = Field(min_length=1, max_length=128)


@app.exception_handler(NotFoundError)
async def not_found_handler(_request, exc: NotFoundError):
    return _error_response(status.HTTP_404_NOT_FOUND, str(exc))


@app.exception_handler(ConflictError)
async def conflict_handler(_request, exc: ConflictError):
    return _error_response(status.HTTP_409_CONFLICT, str(exc))


@app.exception_handler(ValueError)
async def value_error_handler(_request, exc: ValueError):
    return _error_response(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


def _error_response(code: int, detail: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=code, content={"detail": detail})


@app.get("/health")
def health(response: Response) -> dict[str, Any]:
    auth_configured = bool(_read_api_token())
    reconciliation_auth_configured = _reconciliation_auth_ready()
    fenced_cutover = store.fenced_cutover_status()
    rules_sha_configured = bool(EXPECTED_RULES_SHA)
    ready = auth_configured and rules_sha_configured
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "healthy" if ready else "not_ready",
        "service": SERVICE_NAME,
        "auth_configured": auth_configured,
        "reconciliation_auth_configured": reconciliation_auth_configured,
        "fenced_cutover_enabled": fenced_cutover["enabled"],
        "expected_rules_sha_configured": rules_sha_configured,
        "db_path_configured": bool(str(DB_PATH)),
        "progress_stall_seconds": PROGRESS_STALL_SECONDS,
        "reconciliation": reconciliation_controller.status(),
        "contract_version": CONTRACT_VERSION,
    }


@app.get("/v1/contract", dependencies=[Depends(require_auth)])
def public_contract() -> dict[str, str]:
    return contract_descriptor()


@app.get("/v1/workers", dependencies=[Depends(require_auth)])
def workers() -> list[dict[str, Any]]:
    return store.snapshot()["workers"]


@app.post("/v1/workers", dependencies=[Depends(require_auth)])
def register_worker(payload: WorkerRegistration) -> dict[str, Any]:
    worker = store.register_worker(**payload.model_dump())
    _audit("worker.registered", payload.correlation_id, worker_id=payload.worker_id, host=payload.host, role=payload.role)
    return worker


@app.post("/v1/workers/{worker_id}/heartbeat", dependencies=[Depends(require_auth)])
def heartbeat(worker_id: str, payload: Heartbeat) -> dict[str, Any]:
    worker = store.heartbeat_worker(worker_id, **payload.model_dump(exclude_none=True))
    _audit("worker.heartbeat", payload.correlation_id, worker_id=worker_id, profile=worker["profile"])
    return worker


@app.get("/v1/repositories", dependencies=[Depends(require_auth)])
def repositories() -> list[dict[str, Any]]:
    return store.snapshot()["repositories"]


@app.post("/v1/repositories", dependencies=[Depends(require_auth)])
def configure_repository(payload: RepositoryLaneConfig) -> dict[str, Any]:
    configured = store.configure_repository(
        repository=payload.repository,
        enabled=payload.enabled,
        max_in_flight=payload.max_in_flight,
    )
    _audit(
        "repository.configured",
        payload.correlation_id,
        repository=payload.repository,
        enabled=payload.enabled,
        max_in_flight=payload.max_in_flight,
    )
    return configured


@app.put("/v1/workers/{worker_id}/affinities", dependencies=[Depends(require_auth)])
def worker_affinities(worker_id: str, payload: WorkerAffinityUpdate) -> dict[str, Any]:
    repositories = store.replace_worker_affinities(worker_id, payload.repositories)
    _audit(
        "worker.affinity.replaced",
        payload.correlation_id,
        worker_id=worker_id,
        repository_count=len(repositories),
    )
    return {"worker_id": worker_id, "repositories": repositories}


@app.post("/v1/tasks", dependencies=[Depends(require_auth)], status_code=201)
def enqueue_task(payload: TaskCreate, response: Response) -> dict[str, Any]:
    if payload.fencing_required and not _reconciliation_auth_ready():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="reconciliation_auth_not_configured_or_not_separated",
        )
    task, created = store.enqueue_task(**payload.model_dump())
    if not created:
        response.status_code = status.HTTP_200_OK
    _audit(
        "task.enqueued" if created else "task.replayed",
        payload.correlation_id,
        task_id=task["task_id"],
        issue_number=payload.issue_number,
        branch=task["branch"],
        created=created,
    )
    return {"created": created, "task": public_task(task)}


@app.get("/v1/tasks/{task_id}", dependencies=[Depends(require_auth)])
def get_task(task_id: str) -> dict[str, Any]:
    return public_task(store.get_task(task_id))


@app.get("/v1/tasks/{task_id}/diagnostics", dependencies=[Depends(require_auth)])
def task_diagnostics(task_id: str) -> dict[str, Any]:
    """Return a read-only per-task precheck; never claim or reconcile."""
    try:
        return read_task_diagnostics(
            DB_PATH,
            task_id,
            expected_rules_sha=store.expected_rules_sha,
            heartbeat_ttl_seconds=store.heartbeat_ttl_seconds,
            progress_stall_seconds=store.progress_stall_seconds,
        )
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid_task_id") from None
    except TaskDiagnosticNotFound:
        raise HTTPException(status_code=404, detail="task_not_found") from None
    except TaskDiagnosticUnavailable:
        raise HTTPException(status_code=503, detail="task_diagnostics_unavailable") from None


@app.post("/v1/work", dependencies=[Depends(require_auth)], status_code=201)
def create_work(payload: WorkCreate, response: Response) -> dict[str, Any]:
    work, created = work_orchestrator.create_work(
        **payload.model_dump(exclude={"fencing_required"})
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    _audit(
        "work.created" if created else "work.replayed",
        payload.correlation_id,
        work_id=work["work_id"],
        task_id=work["task_id"],
        phase=work["phase"],
    )
    return {"created": created, "work": work}


@app.get("/v1/work/{work_id}", dependencies=[Depends(require_auth)])
def get_work(work_id: str) -> dict[str, Any]:
    return work_orchestrator.get_work(work_id)


@app.post("/v1/work/{work_id}/evidence", dependencies=[Depends(require_auth)])
def record_work_evidence(work_id: str, payload: EvidenceRecord) -> dict[str, Any]:
    work = work_orchestrator.record_evidence(work_id=work_id, **payload.model_dump())
    _audit(
        "work.evidence_verified",
        payload.correlation_id,
        work_id=work_id,
        task_id=work["task_id"],
        validation_sha=payload.validation_sha.lower(),
    )
    return work


@app.post("/v1/work/{work_id}/merge-result", dependencies=[Depends(require_auth)])
def record_merge_result(work_id: str, payload: MergeResult) -> dict[str, Any]:
    work = work_orchestrator.record_merge_result(work_id=work_id, **payload.model_dump())
    _audit(
        "work.merge_result_recorded",
        payload.correlation_id,
        work_id=work_id,
        task_id=work["task_id"],
        merge_state=work["merge"]["state"],
        expected_head_sha=payload.expected_head_sha.lower(),
    )
    return work


def _claim_impl(
    payload: ClaimRequest, *, only_task_id: str | None = None,
    fencing_required: bool = False,
) -> dict[str, Any]:
    worker = store.get_worker(payload.worker_id)
    task, lease = store.claim_task(
        worker_id=payload.worker_id,
        role=worker["role"],
        correlation_id=payload.correlation_id,
        lease_seconds=payload.lease_seconds,
        only_task_id=only_task_id,
        fencing_required=fencing_required,
    )
    if task is None or lease is None:
        _audit("claim.empty", payload.correlation_id, worker_id=payload.worker_id, role=worker["role"])
        result = {"claimed": False, "task": None, "lease": None}
        if fencing_required:
            result["recovery_context"] = None
        return result
    _audit(
        "claim.acquired",
        payload.correlation_id,
        worker_id=payload.worker_id,
        role=worker["role"],
        task_id=task["task_id"],
    )
    result = {
        "claimed": True,
        "task": public_task(task),
        "lease": {
            "task_id": lease.task_id,
            "lease_token": lease.lease_token,
            "worker_id": lease.worker_id,
            "role": lease.role,
            "lease_expires_at": lease.lease_expires_at,
            "lease_generation": lease.lease_generation,
            "attempt_id": lease.attempt_id,
        },
    }
    if fencing_required:
        result["recovery_context"] = store.get_recovery_context(task["task_id"])
    return result


@app.post("/v1/claims", dependencies=[Depends(require_auth)])
def claim(payload: ClaimRequest) -> dict[str, Any]:
    """Legacy general-purpose claim for existing, fully capable workers."""
    return _claim_impl(payload)


@app.post("/v1/claims/exact", dependencies=[Depends(require_auth)])
def claim_exact(payload: ExactClaimRequest) -> dict[str, Any]:
    """Select one approved task ID rather than drain arbitrary queued work."""
    return _claim_impl(payload, only_task_id=payload.task_id)


@app.post("/v1/claims/fenced/exact", dependencies=[Depends(require_auth)])
def claim_fenced_exact(payload: ExactClaimRequest) -> dict[str, Any]:
    """Claim one opt-in fenced task without exposing it to legacy workers."""
    return _claim_impl(
        payload, only_task_id=payload.task_id, fencing_required=True,
    )


@app.post("/v1/tasks/{task_id}/lease/renew", dependencies=[Depends(require_auth)])
def renew(task_id: str, payload: RenewLease) -> dict[str, Any]:
    task = store.renew_lease(task_id=task_id, **payload.model_dump())
    _audit("lease.renewed", payload.correlation_id, worker_id=payload.worker_id, task_id=task_id)
    return public_task(task)


@app.post("/v1/tasks/{task_id}/start", dependencies=[Depends(require_auth)])
def start(task_id: str, payload: LeaseMutation) -> dict[str, Any]:
    task = store.start_task(task_id=task_id, **payload.model_dump())
    _audit("task.started", payload.correlation_id, worker_id=payload.worker_id, task_id=task_id)
    return public_task(task)


@app.post("/v1/tasks/{task_id}/validation", dependencies=[Depends(require_auth)])
def validation(task_id: str, payload: ValidationHandoff) -> dict[str, Any]:
    task = store.submit_for_validation(task_id=task_id, **payload.model_dump())
    _audit(
        "task.validation_requested",
        payload.correlation_id,
        worker_id=payload.worker_id,
        task_id=task_id,
        produced_sha=payload.produced_sha,
    )
    return public_task(task)


@app.post("/v1/tasks/{task_id}/complete", dependencies=[Depends(require_auth)])
def complete(task_id: str, payload: LeaseMutation) -> dict[str, Any]:
    task = store.complete_task(task_id=task_id, **payload.model_dump())
    _audit("task.completed", payload.correlation_id, worker_id=payload.worker_id, task_id=task_id)
    return public_task(task)


@app.post("/v1/tasks/{task_id}/fail", dependencies=[Depends(require_auth)])
def fail(task_id: str, payload: Failure) -> dict[str, Any]:
    task = store.fail_task(task_id=task_id, **payload.model_dump())
    _audit(
        "task.failed_attempt",
        payload.correlation_id,
        worker_id=payload.worker_id,
        task_id=task_id,
        state=task["state"],
    )
    return public_task(task)


@app.post("/v1/tasks/{task_id}/block", dependencies=[Depends(require_auth)])
def block(task_id: str, payload: Failure) -> dict[str, Any]:
    task = store.block_task(task_id=task_id, **payload.model_dump())
    _audit(
        "task.blocked_external",
        payload.correlation_id,
        worker_id=payload.worker_id,
        task_id=task_id,
        reason_code="external_block",
    )
    return public_task(task)


@app.post("/v1/tasks/{task_id}/checkpoints", dependencies=[Depends(require_auth)])
def checkpoint(
    task_id: str, payload: CheckpointRecord, response: Response,
) -> dict[str, Any]:
    checkpoint_record, created = store.record_checkpoint(
        task_id=task_id, **payload.model_dump()
    )
    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )
    _audit(
        "task.checkpoint_recorded",
        payload.correlation_id,
        worker_id=payload.worker_id,
        task_id=task_id,
        checkpoint_id=payload.checkpoint_id,
        lease_generation=payload.lease_generation,
        created=created,
        effect_state=payload.effect_state,
    )
    return {"created": created, "checkpoint": checkpoint_record}


@app.get("/v1/tasks/{task_id}/checkpoints/latest", dependencies=[Depends(require_auth)])
def latest_checkpoint(task_id: str) -> dict[str, Any]:
    return {"checkpoint": store.get_latest_checkpoint(task_id)}


@app.post(
    "/v1/tasks/{task_id}/effects/{effect_id}/reconcile",
    dependencies=[Depends(require_reconciliation_auth)],
)
def reconcile_effect(
    task_id: str, effect_id: str, payload: EffectReconciliation, response: Response,
) -> dict[str, Any]:
    reconciliation, created = store.reconcile_effect(
        task_id=task_id, effect_id=effect_id, **payload.model_dump()
    )
    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )
    _audit(
        "task.effect_reconciled",
        payload.correlation_id,
        task_id=task_id,
        effect_id=effect_id,
        lease_generation=payload.lease_generation,
        outcome=payload.outcome,
        created=created,
    )
    return {"created": created, "reconciliation": reconciliation}


@app.post("/v1/tasks/{task_id}/requeue", dependencies=[Depends(require_auth)])
def requeue(task_id: str, payload: Requeue) -> dict[str, Any]:
    task = store.requeue_blocked(task_id, correlation_id=payload.correlation_id)
    _audit("task.requeued", payload.correlation_id, task_id=task_id)
    return public_task(task)


@app.post("/v1/leases/recover", dependencies=[Depends(require_auth)])
def recover_leases(x_correlation_id: str | None = Header(default=None)) -> dict[str, Any]:
    correlation_id = correlation(x_correlation_id)
    recovered = store.recover_expired_leases()
    _audit("lease.recovery_cycle", correlation_id, recovered=recovered)
    return {"recovered": recovered, "correlation_id": correlation_id}


@app.post("/v1/watchdog/recover", dependencies=[Depends(require_auth)])
def recover_stalled(x_correlation_id: str | None = Header(default=None)) -> dict[str, Any]:
    correlation_id = correlation(x_correlation_id)
    result = store.recover_stalled_tasks()
    _audit("progress_watchdog.recovery_cycle", correlation_id, **result)
    return {**result, "correlation_id": correlation_id}


@app.get("/v1/snapshot", dependencies=[Depends(require_auth)])
def snapshot() -> dict[str, Any]:
    return store.snapshot()


@app.get("/v1/quarantine", dependencies=[Depends(require_auth)])
def quarantine() -> list[dict[str, Any]]:
    return store.list_quarantine()
