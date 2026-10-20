   task_id, "native-validator", lease_b["lease_token"],
                "native-fenced-complete",
                lease_generation=lease_b["lease_generation"],
                attempt_id=lease_b["attempt_id"],
            )
            final = api.get_task(task_id)
            assert final["state"] == "completed"
            assert final["lease_generation"] == 2

            ambiguous_created = app.post("/v1/tasks", headers=headers, json={
                "repository": REPO, "issue_number": 19,
                "request_id": "native-http-ambiguous", "base_sha": BASE,
                "correlation_id": "native-ambiguous-create",
                "fencing_required": True,
            })
            assert ambiguous_created.status_code == 201
            ambiguous_id = ambiguous_created.json()["task"]["task_id"]
            ambiguous_claim = api.claim_fenced_exact(
                "native-builder", ambiguous_id, "native-ambiguous-claim",
            )
            ambiguous_lease = ambiguous_claim["lease"]
            ambiguous_fence = {
                "lease_generation": ambiguous_lease["lease_generation"],
                "attempt_id": ambiguous_lease["attempt_id"],
            }
            api.start(
                ambiguous_id, "native-builder", ambiguous_lease["lease_token"],
                "native-ambiguous-start", **ambiguous_fence,
            )
            for sequence, state, action in (
                (1, "pending", "publish_artifact"),
                (2, "unknown", "reconcile_artifact"),
            ):
                api.record_checkpoint(
                    ambiguous_id, "native-builder",
                    ambiguous_lease["lease_token"],
                    ambiguous_lease["lease_generation"],
                    ambiguous_lease["attempt_id"],
                    f"native-ambiguous-{state}", sequence,
                    "artifact_publish", action,
                    f"native-ambiguous-checkpoint-{state}",
                    effect_id="native-ambiguous-artifact",
                    effect_state=state,
                )
            api.block(
                ambiguous_id, "native-builder", ambiguous_lease["lease_token"],
                "native-ambiguous-block", "artifact_publish_ambiguous",
                **ambiguous_fence,
            )
            blocked = api.get_task(ambiguous_id)
            assert blocked["state"] == "blocked"
            assert blocked["blocked_reason"] == "effect_reconciliation_required"


@pytest.mark.parametrize("port", [0, 65536, True, "8097"])
def test_invalid_port_blocks_without_network(tmp_path, port):
    with pytest.raises(NativeExecutorBlocked, match="worker_port_invalid"):
        LocalWorkerApi(port=port, token_file=token_file(tmp_path))


def test_missing_permissive_and_symlink_token_block(tmp_path):
    missing = tmp_path / "missing"
    with LocalWorkerApi(port=8097, token_file=missing) as api:
        with pytest.raises(NativeExecutorBlocked, match="worker_token_file_unavailable"):
            api.get_task("cwp-" + "1" * 24)
    unsafe = token_file(tmp_path)
    unsafe.chmod(0o644)
    with LocalWorkerApi(port=8097, token_file=unsafe) as api:
        with pytest.raises(NativeExecutorBlocked, match="worker_token_permissions"):
            api.get_task("cwp-" + "1" * 24)
    unsafe.chmod(0o600)
    alias = tmp_path / "token-link"
    alias.symlink_to(unsafe)
    with LocalWorkerApi(port=8097, token_file=alias) as api:
        with pytest.raises(NativeExecutorBlocked, match="worker_token_file_unavailable"):
            api.get_task("cwp-" + "1" * 24)


@pytest.mark.parametrize("status,data,code", [
    (401, {"detail": "secret-should-not-appear"}, "worker_auth_rejected"),
    (307, {"detail": "redirect"}, "worker_http_unverified"),
    (503, {"detail": "service"}, "worker_http_unverified"),
    (200, ["wrong"], "worker_response_invalid"),
    (200, {"state": "queued", "task_id": "cwp-" + "f" * 24},
     "worker_task_readback_invalid"),
])
def test_status_contract_and_identity_fail_closed(tmp_path, status, data, code):
    token=[REDACTED]
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=data, request=request)
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          trust_env=False, follow_redirects=False)
    try:
        with LocalWorkerApi(port=8097, token_file=token, client=client) as api:
            with pytest.raises(NativeExecutorBlocked, match=code) as error:
                api.get_task("cwp-" + "1" * 24)
        assert len(calls) == 1
        assert "secret-should-not-appear" not in str(error.value)
        assert calls[0].url.host == "127.0.0.1"
    finally:
        client.close()


def test_transport_failure_never_retries_mutation(tmp_path):
    token=[REDACTED]
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectTimeout("private-network-detail")
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          trust_env=False, follow_redirects=False)
    try:
        with LocalWorkerApi(port=8097, token_file=token, client=client) as api:
            with pytest.raises(NativeExecutorBlocked, match="worker_transport_unverified") as error:
                api.claim_exact("native-builder", "cwp-" + "1" * 24,
                                "native-http-timeout")
        assert len(calls) == 1
        assert "private-network-detail" not in str(error.value)
    finally:
        client.close()


@pytest.mark.parametrize("readback_stage,accepted", [
    ("artifact_publish", True),
    ("different_stage", False),
])
def test_checkpoint_transport_ambiguity_uses_readback_without_post_replay(
    tmp_path, readback_stage, accepted,
):
    token=[REDACTED]
    task_id = "cwp-" + "1" * 24
    calls = []
    stored = None

    def handler(request):
        nonlocal stored
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            body = json.loads(request.content)
            stored = {
                "task_id": task_id,
                **{
                    key: value for key, value in body.items()
                    if key not in {"worker_id", "lease_token"}
                },
                "stage": readback_stage,
            }
            raise httpx.ConnectTimeout("private-network-detail")
        return httpx.Response(
            200, json={"checkpoint": stored}, request=request,
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler), trust_env=False,
        follow_redirects=False,
    )
    try:
        with LocalWorkerApi(port=8097, token_file=token, client=client) as api:
            arguments = (
                task_id, "native-builder", "isolated-lease-token-123", 1,
                "attempt-1", "checkpoint-1", 1, "artifact_publish",
                "publish_artifact", "native-checkpoint-ambiguity",
            )
            if accepted:
                checkpoint = api.record_checkpoint(*arguments)
                assert checkpoint["checkpoint_id"] == "checkpoint-1"
            else:
                with pytest.raises(
                    NativeExecutorBlocked, match="worker_checkpoint_ambiguous"
                ):
                    api.record_checkpoint(*arguments)
        assert calls == [
            ("POST", f"/v1/tasks/{task_id}/checkpoints"),
            ("GET", f"/v1/tasks/{task_id}/checkpoints/latest"),
        ]
    finally:
        client.close()


def test_reject_unbounded_or_wrong_claim_without_claim_replay(tmp_path):
    token=[REDACTED]
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "claimed": "true", "task": {"task_id": "cwp-" + "1" * 24},
            "lease": None,
        }, request=request)
    client = httpx.Client(transport=httpx.MockTransport(handler),
                          trust_env=False, follow_redirects=False)
    try:
        with LocalWorkerApi(port=8097, token_file=token, client=client) as api:
            with pytest.raises(NativeExecutorBlocked, match="worker_claim_invalid"):
                api.claim_exact("native-builder", "cwp-" + "1" * 24,
                                "native-http-reject")
        assert len(calls) == 1
    finally:
        client.close()


@pytest.mark.parametrize("drift", ["generation", "attempt", "recovery"])
def test_fenced_claim_rejects_missing_or_inconsistent_fence_context(tmp_path, drift):
    task_id = "cwp-" + "1" * 24
    worker_id = "native-builder"
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    task = {
        "task_id": task_id,
        "state": "leased",
        "leased_by": worker_id,
        "fencing_required": True,
        "lease_generation": 1,
        "attempt_id": "attempt-1",
    }
    lease = {
        "task_id": task_id,
        "worker_id": worker_id,
        "role": "builder",
        "lease_token": "isolated-lease-token-123",
        "lease_expires_at": future,
        "lease_generation": 1,
        "attempt_id": "attempt-1",
    }
    recovery = {"latest_checkpoint": None, "effects": []}
    if drift == "generation":
        lease["lease_generation"] = None
    elif drift == "attempt":
        task["attempt_id"] = "different-attempt"
    else:
        recovery["effects"] = "not-a-list"
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "claimed": True,
            "task": task,
            "lease": lease,
            "recovery_context": recovery,
        }, request=request)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), trust_env=False,
        follow_redirects=False,
    )
    try:
        with LocalWorkerApi(
            port=8097, token_file=token_file(tmp_path), client=client,
        ) as api:
            with pytest.raises(
                NativeExecutorBlocked, match="worker_fenced_claim_invalid"
            ):
                api.claim_fenced_exact(
                    worker_id, task_id, "native-fenced-claim-negative",
                )
        assert len(calls) == 1
        assert calls[0].url.path == "/v1/claims/fenced/exact"
    finally:
        client.close()


@pytest.mark.parametrize("lease_patch,task_patch", [
    ({"role": "operator"}, {}),
    ({"role": None}, {}),
    ({"lease_expires_at": None}, {}),
    ({"lease_expires_at": "not-an-iso-date"}, {}),
    ({"lease_expires_at": "2000-01-01T00:00:00+00:00"}, {}),
    ({"lease_expires_at": "2999-01-01T00:00:00"}, {}),
    ({}, {"state": "completed"}),
])
def test_claim_with_unverified_role_or_expiry_never_becomes_eligible(
    tmp_path, lease_patch, task_patch,
):
    task_id = "cwp-" + "1" * 24
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    lease = {
        "task_id": task_id, "worker_id": "native-builder",
        "role": "builder", "lease_token": "isolated-lease-token-123",
        "lease_expires_at": future,
    }
    task = {
        "task_id": task_id, "state": "leased",
        "leased_by": "native-builder",
    }
    lease.update(lease_patch)
    task.update(task_patch)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "claimed": True, "task": task, "lease": lease,
        }, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler),
                          trust_env=False, follow_redirects=False)
    try:
        with LocalWorkerApi(port=8097, token_file=token_file(tmp_path),
                            client=client) as api:
            with pytest.raises(NativeExecutorBlocked, match="worker_claim_invalid"):
                api.claim_exact("native-builder", task_id, "native-lease-negative")
        assert len(calls) == 1
        assert calls[0].url.path == "/v1/claims/exact"
    finally:
        client.close()
