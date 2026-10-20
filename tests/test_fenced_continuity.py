rker-{later_state}",
                checkpoint_id=f"unsafe-worker-{later_state}", sequence=2,
                stage="publish", next_action="must use separate reconciliation",
                effect_id="publish-release", effect_state=later_state,
                effect_result_ref=result_ref, **lease_args(lease)
            )
    with pytest.raises(ValueError, match="effect_id incompatível"):
        store.record_checkpoint(
            task_id=task["task_id"], worker_id="builder-a",
            correlation_id="unsafe-worker-none",
            checkpoint_id="unsafe-worker-none", sequence=2,
            stage="publish", next_action="cannot erase effect identity",
            effect_id="publish-release", effect_state="none", **lease_args(lease)
        )
    with pytest.raises(ValueError, match="effect_result_ref obrigatório"):
        store.record_checkpoint(
            task_id=task["task_id"], worker_id="builder-a",
            correlation_id="resolved-without-evidence",
            checkpoint_id="resolved-without-evidence", sequence=2,
            stage="publish", next_action="must include evidence",
            effect_id="different-effect", effect_state="confirmed",
            **lease_args(lease)
        )

    clock.advance(11)
    store.heartbeat_worker("builder-b", correlation_id="heartbeat-b")
    assert store.recover_expired_leases() == 1
    blocked = store.get_task(task["task_id"])
    assert blocked["state"] == "blocked"
    assert blocked["blocked_reason"] == "effect_reconciliation_required"

    with pytest.raises(ConflictError, match="efeito requer reconciliação"):
        store.requeue_blocked(task["task_id"], correlation_id="unsafe-requeue")
    next_task, next_lease = store.claim_task(
        worker_id="builder-b",
        role="builder",
        correlation_id="claim-b",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert next_task is None
    assert next_lease is None

    reconciliation, created = store.reconcile_effect(
        task_id=task["task_id"],
        effect_id="publish-release",
        lease_generation=lease.lease_generation,
        outcome="not_applied",
        readback_ref="readback://release/missing",
        correlation_id="reconcile-release",
    )
    replay, replay_created = store.reconcile_effect(
        task_id=task["task_id"],
        effect_id="publish-release",
        lease_generation=lease.lease_generation,
        outcome="not_applied",
        readback_ref="readback://release/missing",
        correlation_id="reconcile-release-replay",
    )
    assert created is True
    assert replay_created is False
    assert replay["content_digest"] == reconciliation["content_digest"]
    recovery_checkpoint = store.get_latest_checkpoint(task["task_id"])
    assert recovery_checkpoint
    assert recovery_checkpoint["reconciliation_outcome"] == "not_applied"
    assert recovery_checkpoint["reconciliation_readback_ref"] == (
        "readback://release/missing"
    )
    with pytest.raises(ConflictError, match="reconciliação divergente"):
        store.reconcile_effect(
            task_id=task["task_id"],
            effect_id="publish-release",
            lease_generation=lease.lease_generation,
            outcome="confirmed",
            readback_ref="readback://release/present",
            correlation_id="reconcile-release-divergent",
        )

    requeued = store.requeue_blocked(
        task["task_id"], correlation_id="safe-requeue"
    )
    assert requeued["state"] == "queued"
    resumed, resumed_lease = store.claim_task(
        worker_id="builder-b",
        role="builder",
        correlation_id="claim-b-after-reconcile",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert resumed and resumed_lease
    assert resumed_lease.lease_generation == 2
    assert resumed_lease.attempt_id != lease.attempt_id

    store.start_task(
        task_id=task["task_id"], worker_id="builder-b",
        correlation_id="start-b-after-reconcile", **lease_args(resumed_lease)
    )
    store.record_checkpoint(
        task_id=task["task_id"], worker_id="builder-b",
        correlation_id="same-effect-new-generation",
        checkpoint_id="publish-unknown-generation-2", sequence=1,
        stage="publish", next_action="reconcile generation two",
        effect_id="publish-release", effect_state="unknown",
        **lease_args(resumed_lease)
    )
    blocked_again = store.block_task(
        task_id=task["task_id"], worker_id="builder-b",
        reason="uncertain response", correlation_id="block-generation-two",
        **lease_args(resumed_lease)
    )
    assert blocked_again["blocked_reason"] == "effect_reconciliation_required"
    generation_two, generation_two_created = store.reconcile_effect(
        task_id=task["task_id"],
        effect_id="publish-release",
        lease_generation=resumed_lease.lease_generation,
        outcome="confirmed",
        readback_ref="readback://release/present",
        correlation_id="reconcile-generation-two",
    )
    assert generation_two_created is True
    assert generation_two["lease_generation"] == 2
    recovery_checkpoint = store.get_latest_checkpoint(task["task_id"])
    assert recovery_checkpoint
    assert recovery_checkpoint["reconciliation_outcome"] == "confirmed"
    assert recovery_checkpoint["reconciliation_readback_ref"] == (
        "readback://release/present"
    )
    assert store.requeue_blocked(
        task["task_id"], correlation_id="requeue-generation-two"
    )["state"] == "queued"


def test_unresolved_effect_cannot_escape_through_normal_transitions(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = make_store(tmp_path, clock)
    register(store, "builder-a", "builder")
    register(store, "validator-b", "validator")

    task = enqueue_fenced(store, "transition-gate-builder")
    _claimed, lease = store.claim_task(
        worker_id="builder-a",
        role="builder",
        correlation_id="claim-builder",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert lease
    store.start_task(
        task_id=task["task_id"], worker_id="builder-a",
        correlation_id="start-builder", **lease_args(lease)
    )
    store.record_checkpoint(
        task_id=task["task_id"], worker_id="builder-a",
        correlation_id="unknown-builder-effect", checkpoint_id="unknown-builder-effect",
        sequence=1, stage="publish", next_action="reconcile before release",
        effect_id="external-write", effect_state="pending", **lease_args(lease)
    )
    with pytest.raises(ConflictError, match="efeito requer reconciliação"):
        store.submit_for_validation(
            task_id=task["task_id"], worker_id="builder-a",
            produced_sha="3" * 40, correlation_id="unsafe-submit", **lease_args(lease)
        )
    with pytest.raises(ConflictError, match="efeito requer reconciliação"):
        store.fail_task(
            task_id=task["task_id"], worker_id="builder-a",
            reason="unsafe retry", correlation_id="unsafe-fail", **lease_args(lease)
        )
    still_owned = store.get_task(task["task_id"])
    assert still_owned["state"] == "running"
    assert still_owned["leased_by"] == "builder-a"
    blocked = store.block_task(
        task_id=task["task_id"], worker_id="builder-a",
        reason="ordinary block", correlation_id="safe-block", **lease_args(lease)
    )
    assert blocked["blocked_reason"] == "effect_reconciliation_required"

    validation_task = enqueue_fenced(store, "transition-gate-validator")
    _claimed, builder_lease = store.claim_task(
        worker_id="builder-a",
        role="builder",
        correlation_id="claim-validation-builder",
        only_task_id=validation_task["task_id"],
        fencing_required=True,
    )
    assert builder_lease
    store.start_task(
        task_id=validation_task["task_id"], worker_id="builder-a",
        correlation_id="start-validation-builder", **lease_args(builder_lease)
    )
    store.submit_for_validation(
        task_id=validation_task["task_id"], worker_id="builder-a",
        produced_sha="4" * 40, correlation_id="safe-submit",
        **lease_args(builder_lease)
    )
    _claimed, validator_lease = store.claim_task(
        worker_id="validator-b",
        role="validator",
        correlation_id="claim-validator",
        only_task_id=validation_task["task_id"],
        fencing_required=True,
    )
    assert validator_lease
    store.record_checkpoint(
        task_id=validation_task["task_id"], worker_id="validator-b",
        correlation_id="unknown-validator-effect",
        checkpoint_id="unknown-validator-effect", sequence=1,
        stage="validation", next_action="reconcile validation effect",
        effect_id="validation-write", effect_state="unknown",
        **lease_args(validator_lease)
    )
    with pytest.raises(ConflictError, match="efeito requer reconciliação"):
        store.complete_task(
            task_id=validation_task["task_id"], worker_id="validator-b",
            correlation_id="unsafe-complete", **lease_args(validator_lease)
        )
    assert store.get_task(validation_task["task_id"])["state"] == "validating"


def test_recovery_context_preserves_distinct_multi_effect_outcomes(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = make_store(tmp_path, clock)
    register(store, "builder-a", "builder")
    register(store, "builder-b", "builder")
    task = enqueue_fenced(store, "multi-effect-recovery-context")
    _claimed, lease_a = store.claim_task(
        worker_id="builder-a",
        role="builder",
        correlation_id="claim-a",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert lease_a
    store.start_task(
        task_id=task["task_id"], worker_id="builder-a",
        correlation_id="start-a", **lease_args(lease_a)
    )
    for sequence, effect_id in enumerate(("effect-a", "effect-b"), start=1):
        store.record_checkpoint(
            task_id=task["task_id"], worker_id="builder-a",
            correlation_id=f"checkpoint-{effect_id}",
            checkpoint_id=f"checkpoint-{effect_id}", sequence=sequence,
            stage="publish", next_action="reconcile before resume",
            effect_id=effect_id, effect_state="unknown", **lease_args(lease_a)
        )
    store.block_task(
        task_id=task["task_id"], worker_id="builder-a",
        reason="uncertain effects", correlation_id="block-a", **lease_args(lease_a)
    )
    for effect_id, outcome in (
        ("effect-a", "confirmed"),
        ("effect-b", "not_applied"),
    ):
        store.reconcile_effect(
            task_id=task["task_id"], effect_id=effect_id,
            lease_generation=lease_a.lease_generation, outcome=outcome,
            readback_ref=f"readback://{effect_id}/{outcome}",
            correlation_id=f"reconcile-{effect_id}",
        )
    store.requeue_blocked(task["task_id"], correlation_id="safe-requeue")
    _claimed, lease_b = store.claim_task(
        worker_id="builder-b",
        role="builder",
        correlation_id="claim-b",
        only_task_id=task["task_id"],
        fencing_required=True,
    )
    assert lease_b and lease_b.lease_generation == 2

    context = store.get_recovery_context(task["task_id"])
    effect_ledger = {
        row["effect_id"]: row for row in context["effects"]
    }
    assert set(effect_ledger) == {"effect-a", "effect-b"}
    assert effect_ledger["effect-a"]["reconciliation_outcome"] == "confirmed"
    assert effect_ledger["effect-b"]["reconciliation_outcome"] == "not_applied"
    skip_effects = {
        effect_id for effect_id, row in effect_ledger.items()
        if row["reconciliation_outcome"] == "confirmed"
    }
    retryable_effects = {
        effect_id for effect_id, row in effect_ledger.items()
        if row["reconciliation_outcome"] == "not_applied"
    }
    assert skip_effects == {"effect-a"}
    assert retryable_effects == {"effect-b"}
