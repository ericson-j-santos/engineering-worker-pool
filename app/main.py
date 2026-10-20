ository,
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
