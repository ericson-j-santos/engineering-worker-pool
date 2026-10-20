e artifact")
    produced = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "--detach", manifest["base_sha"])
    checkpoint = recovered_builder_checkpoint(produced)
    api.recovery = {"latest_checkpoint": checkpoint, "effects": [checkpoint]}
    api.latest_checkpoint = checkpoint
    api.task["lease_generation"] = 1
    transport.materialized_sha = produced

    result = executor.run(manifest, role="builder", worker_id="fixture-builder")

    assert result["produced_sha"] == produced
    assert result["lease_generation"] == 2
    assert adapter.calls == []
    assert [call[0] for call in transport.calls] == [
        "preflight", "materialize", "verify",
    ]
    assert not any(call[0] == "publish" for call in transport.calls)
    assert api.task["state"] == "validating"


def test_fenced_concrete_s3_transport_resumes_in_independent_workspace(tmp_path):
    executor, manifest, api, adapter, root, _fixture_transport = fenced_setup(tmp_path)
    target_root = tmp_path / "portable-resume"
    git(tmp_path, "clone", "--quiet", "--no-hardlinks", str(root), str(target_root))
    git(target_root, "remote", "remove", "origin")
    git(target_root, "checkout", "--detach", manifest["workspace_sha"])
    assert (root / ".git").resolve() != (target_root / ".git").resolve()

    client = InMemoryArtifactS3()
    transport = S3GitBundleArtifactTransport(
        S3ArtifactConfig(
            bucket="durable-artifacts", region="us-east-2",
            key_prefix="worker/checkpoints",
        ),
        client,
    )
    executor.artifact_transport = transport
    built = executor.run(
        manifest, role="builder", worker_id="fixture-builder",
    )
    produced = built["produced_sha"]
    checkpoint = api.latest_checkpoint
    assert checkpoint["stage"] == "builder_result"
    assert checkpoint["effect_state"] == "confirmed"
    assert LocalWorkspace(target_root).head() == manifest["workspace_sha"]

    recovery = {"latest_checkpoint": checkpoint, "effects": [checkpoint]}
    resume_api = FencedApi(
        {
            "task_id": TASK,
            "repository": REPO,
            "base_sha": manifest["base_sha"],
            "state": "queued",
            "produced_sha": None,
        },
        recovery=recovery,
    )
    resume_adapter = Adapter()
    resume_executor = NativeOneShotExecutor(
        api=resume_api,
        workspace=LocalWorkspace(target_root),
        receipts=ReceiptStore(tmp_path / "portable-resume-state"),
        adapters={resume_adapter.descriptor.adapter_id: resume_adapter},
        command_registries={resume_adapter.descriptor.adapter_id: {
            "fixture.build": lambda: None,
            "fixture.validate": lambda: "valid",
        }},
        boundary=ExecutionBoundary(
            "portable-resume", "native-one-shot-test",
            manifest["workspace_sha"], True, True,
        ),
        artifact_transport=transport,
        fenced_adapter_ids=frozenset({resume_adapter.descriptor.adapter_id}),
    )

    resumed = resume_executor.run(
        manifest, role="builder", worker_id="replacement-builder",
    )

    assert resumed["produced_sha"] == produced
    assert resumed["lease_generation"] == 2
    assert resume_adapter.calls == []
    assert LocalWorkspace(target_root).head() == produced
    assert LocalWorkspace(target_root).status() == ""
    assert git(target_root, "remote") == ""
    assert len(client.objects) == 2
    assert client.put_calls == 3
    assert resume_api.task["state"] == "validating"


def test_fenced_validator_verifies_artifact_checkpoints_and_completes(tmp_path):
    builder_executor, manifest, api, _builder, root, transport = fenced_setup(tmp_path)
    built = builder_executor.run(
        manifest, role="builder", worker_id="fixture-builder"
    )
    produced = built["produced_sha"]
    validator_root = tmp_path / "fenced-validator-repo"
    git(root, "worktree", "add", "--detach", str(validator_root), produced)
    validator_manifest = {
        **manifest,
        "workspace_sha": produced,
        "before_sha256": {
            "src/task.py": hashlib.sha256(
                (validator_root / "src" / "task.py").read_bytes()
            ).hexdigest(),
        },
    }
    validator_adapter = Adapter()
    validator = NativeOneShotExecutor(
        api=api,
        workspace=LocalWorkspace(validator_root),
        receipts=builder_executor.receipts,
        adapters={validator_adapter.descriptor.adapter_id: validator_adapter},
        command_registries={validator_adapter.descriptor.adapter_id: {
            "fixture.build": lambda: None,
            "fixture.validate": lambda: "valid",
        }},
        boundary=ExecutionBoundary(
            "native-test", "native-one-shot-test", produced, True, True,
        ),
        artifact_transport=transport,
        fenced_adapter_ids=frozenset({validator_adapter.descriptor.adapter_id}),
    )

    result = validator.run(
        validator_manifest, role="validator", worker_id="fixture-validator"
    )

    assert result["result"] == "NATIVE_ONE_SHOT_VALIDATED"
    assert result["lease_generation"] == 2
    assert validator_adapter.calls == ["validate"]
    validator_checkpoint = next(
        call for call in api.calls
        if call[0] == "checkpoint" and call[2] == "validator_result"
    )
    assert validator_checkpoint[1] == 1
    complete = next(call for call in api.calls if call[0] == "complete")
    assert complete[1] == {"lease_generation": 2, "attempt_id": "attempt-2"}
    assert api.task["state"] == "completed"

    before = len(api.calls)
    replay = validator.run(
        validator_manifest, role="validator", worker_id="fixture-validator"
    )
    assert replay["replayed"] is True
    assert api.calls[before:] == [("get", TASK), ("checkpoint_get", TASK)]
    assert validator_adapter.calls == ["validate"]


def test_fenced_replay_requires_current_trust_and_canonical_artifact(tmp_path):
    executor, manifest, api, adapter, _root, transport = fenced_setup(tmp_path)
    executor.run(manifest, role="builder", worker_id="fixture-builder")
    receipt_path = executor.receipts.paths(TASK, "builder")[1]
    original = json.loads(receipt_path.read_text(encoding="utf-8"))

    executor.fenced_adapter_ids = frozenset()
    with pytest.raises(NativeExecutorBlocked, match="fenced_adapter_not_attested"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls[-1] == ("get", TASK)

    executor.fenced_adapter_ids = frozenset({adapter.descriptor.adapter_id})
    executor.artifact_transport = None
    with pytest.raises(
        NativeExecutorBlocked, match="durable_artifact_transport_required"
    ):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls[-1] == ("get", TASK)

    executor.artifact_transport = transport
    tampered = {**original, "artifact_ref": "artifact://fixture/tampered"}
    receipt_path.write_text(
        json.dumps(tampered, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    before_transport = len(transport.calls)
    with pytest.raises(NativeExecutorBlocked, match="receipt_checkpoint_drift"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls[-1] == ("checkpoint_get", TASK)
    assert len(transport.calls) == before_transport


@pytest.mark.parametrize("drift", ["task", "ledger"])
def test_fenced_recovery_rejects_cross_task_or_divergent_ledger(tmp_path, drift):
    checkpoint = recovered_builder_checkpoint("d" * 40)
    effect = dict(checkpoint)
    if drift == "task":
        checkpoint = {**checkpoint, "task_id": "cwp-" + "2" * 24}
        effect = dict(checkpoint)
    else:
        effect["checkpoint_id"] = "divergent-effect-checkpoint"
    recovery = {"latest_checkpoint": checkpoint, "effects": [effect]}
    executor, manifest, api, adapter, _root, transport = fenced_setup(
        tmp_path, recovery=recovery,
    )

    with pytest.raises(NativeExecutorBlocked, match="recovery_context_invalid"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert adapter.calls == []
    assert transport.calls == [("preflight",)]
    assert api.calls[-1][0:2] == ("block", "unsafe_recovery_context")


@pytest.mark.parametrize("effect_state,stage", [
    ("unknown", "external_effect"),
    ("pending", "external_effect"),
    ("confirmed", "external_effect"),
])
def test_fenced_recovery_blocks_unsafe_effect_before_adapter(tmp_path, effect_state, stage):
    effect = {
        "task_id": TASK,
        "checkpoint_id": "prior-effect-checkpoint",
        "lease_generation": 1,
        "attempt_id": "attempt-1",
        "sequence": 1,
        "effect_id": "external-effect",
        "effect_state": effect_state,
        "effect_result_ref": (
            "readback://external/effect" if effect_state == "confirmed" else None
        ),
        "reconciliation_outcome": None,
        "reconciliation_readback_ref": None,
        "stage": stage,
        "artifact_kind": None,
        "artifact_ref": None,
        "artifact_sha256": None,
        "next_action": "reconcile_external_effect",
    }
    recovery = {"latest_checkpoint": effect, "effects": [effect]}
    executor, manifest, api, adapter, _root, _transport = fenced_setup(
        tmp_path, recovery=recovery,
    )
    with pytest.raises(NativeExecutorBlocked, match="recovery_effect_unsafe"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert adapter.calls == []
    assert api.calls[-1][0:2] == ("block", "unsafe_recovery_context")


def test_fenced_reconciled_not_applied_blocks_before_start(tmp_path):
    latest = {
        "task_id": TASK,
        "checkpoint_id": "prior-artifact-publish",
        "lease_generation": 1,
        "attempt_id": "attempt-1",
        "sequence": 1,
        "effect_id": "prior-artifact-effect",
        "effect_state": "unknown",
        "effect_result_ref": None,
        "reconciliation_outcome": None,
        "reconciliation_readback_ref": None,
        "stage": "artifact_publish",
        "artifact_kind": None,
        "artifact_ref": None,
        "artifact_sha256": None,
        "next_action": "publish_artifact",
    }
    effect = {
        **latest,
        "reconciliation_outcome": "not_applied",
        "reconciliation_readback_ref": "readback://artifact/not-applied",
    }
    executor, manifest, api, adapter, _root, transport = fenced_setup(
        tmp_path,
        recovery={"latest_checkpoint": latest, "effects": [effect]},
    )

    with pytest.raises(
        NativeExecutorBlocked, match="recovery_effect_not_resumable"
    ):
        executor.run(manifest, role="builder", worker_id="fixture-builder")

    assert adapter.calls == []
    assert transport.calls == [("preflight",)]
    assert not any(call[0] in {"start", "fail"} for call in api.calls)
    assert api.calls[-1][0:2] == ("block", "unsafe_recovery_context")
    assert api.task["state"] == "blocked"


def test_stale_fail_rejection_does_not_mask_adapter_failure(tmp_path):
    executor, manifest, api, _adapter, _root, _transport = fenced_setup(
        tmp_path, adapter=Adapter(interrupt=True), reject_fail=True,
    )
    with pytest.raises(NativeExecutorBlocked, match="fixture_interrupted"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls[-1][0:2] == ("fail", "adapter_failed")


def test_stale_fail_rejection_preserves_closed_checkpoint_error(tmp_path):
    executor, manifest, api, _adapter, _root, _transport = fenced_setup(
        tmp_path, reject_fail=True,
    )

    def ambiguous_checkpoint(*_args, **_kwargs):
        raise NativeExecutorBlocked("worker_checkpoint_ambiguous")

    api.record_checkpoint = ambiguous_checkpoint
    with pytest.raises(NativeExecutorBlocked, match="worker_checkpoint_ambiguous"):
        executor.run(manifest, role="builder", worker_id="fixture-builder")
    assert api.calls[-1][0:2] == ("fail", "adapter_failed")
