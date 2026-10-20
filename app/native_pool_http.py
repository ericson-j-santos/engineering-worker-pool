ase_token"]) <= 128
              and type(lease.get("role")) is str
              and lease["role"] in {"builder", "validator"}
              and task.get("state") in {"leased", "validating"},
              "worker_claim_invalid")
        # A claimed lease must be time-bound and explicitly UTC-offset-aware.
        # Never trust the transport's claimed=true without an expiry contract.
        expires = lease.get("lease_expires_at")
        _need(type(expires) is str and 20 <= len(expires) <= 48,
              "worker_claim_invalid")
        try:
            deadline = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            raise NativeExecutorBlocked("worker_claim_invalid") from None
        _need(deadline.tzinfo is not None and deadline.utcoffset() is not None
              and deadline.astimezone(UTC) > datetime.now(UTC),
              "worker_claim_invalid")
        if fenced:
            generation, attempt_id = lease.get("lease_generation"), lease.get("attempt_id")
            recovery = reply.get("recovery_context")
            _need(task.get("fencing_required") is True
                  and type(generation) is int and not isinstance(generation, bool)
                  and generation >= 1
                  and type(attempt_id) is str and bool(SAFE_ID.fullmatch(attempt_id))
                  and task.get("lease_generation") == generation
                  and task.get("attempt_id") == attempt_id
                  and type(recovery) is dict
                  and set(recovery) == {"latest_checkpoint", "effects"}
                  and (recovery["latest_checkpoint"] is None
                       or type(recovery["latest_checkpoint"]) is dict)
                  and type(recovery["effects"]) is list
                  and len(recovery["effects"]) <= 64
                  and all(type(item) is dict for item in recovery["effects"]),
                  "worker_fenced_claim_invalid")
        self._task(task, task_id)
        return reply

    def claim_exact(self, worker_id: str, task_id: str,
                    correlation_id: str) -> dict[str, Any]:
        return self._claim_exact(
            worker_id, task_id, correlation_id, fenced=False,
        )

    def claim_fenced_exact(self, worker_id: str, task_id: str,
                           correlation_id: str) -> dict[str, Any]:
        return self._claim_exact(
            worker_id, task_id, correlation_id, fenced=True,
        )

    @staticmethod
    def _checkpoint_matches(checkpoint: Any, expected: dict[str, Any]) -> bool:
        return type(checkpoint) is dict and all(
            checkpoint.get(key) == value for key, value in expected.items()
        )

    def get_latest_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        _id(task_id)
        reply = self._request("GET", f"/v1/tasks/{task_id}/checkpoints/latest")
        _need(set(reply) == {"checkpoint"}
              and (reply["checkpoint"] is None or type(reply["checkpoint"]) is dict),
              "worker_checkpoint_readback_invalid")
        checkpoint = reply["checkpoint"]
        if checkpoint is not None:
            _need(checkpoint.get("task_id") == task_id,
                  "worker_checkpoint_readback_invalid")
        return checkpoint

    def record_checkpoint(
        self, task_id: str, worker_id: str, lease_token: str,
        lease_generation: int, attempt_id: str, checkpoint_id: str,
        sequence: int, stage: str, next_action: str, correlation_id: str, *,
        artifact_kind: str | None = None, artifact_ref: str | None = None,
        artifact_sha256: str | None = None, effect_id: str | None = None,
        effect_state: str = "none", effect_result_ref: str | None = None,
    ) -> dict[str, Any]:
        _worker(worker_id, correlation_id)
        _id(task_id)
        _need(type(lease_token) is str and 16 <= len(lease_token) <= 128
              and type(lease_generation) is int
              and not isinstance(lease_generation, bool) and lease_generation >= 1
              and type(attempt_id) is str and bool(SAFE_ID.fullmatch(attempt_id))
              and type(checkpoint_id) is str and bool(SAFE_ID.fullmatch(checkpoint_id))
              and type(sequence) is int and not isinstance(sequence, bool) and sequence >= 1
              and type(stage) is str and bool(SAFE_ID.fullmatch(stage))
              and type(next_action) is str and 1 <= len(next_action) <= 2048,
              "worker_checkpoint_invalid")
        artifact_values = (artifact_kind, artifact_ref, artifact_sha256)
        _need(all(value is None for value in artifact_values)
              or (type(artifact_kind) is str and bool(SAFE_ID.fullmatch(artifact_kind))
                  and type(artifact_ref) is str and 1 <= len(artifact_ref) <= 2048
                  and "\n" not in artifact_ref and "\r" not in artifact_ref
                  and type(artifact_sha256) is str
                  and bool(SHA256.fullmatch(artifact_sha256))),
              "worker_checkpoint_invalid")
        _need(effect_state in {"none", "pending", "unknown", "confirmed", "not_applied"}
              and ((effect_state == "none" and effect_id is None
                    and effect_result_ref is None)
                   or (effect_state != "none" and type(effect_id) is str
                       and bool(SAFE_ID.fullmatch(effect_id))
                       and ((effect_state in {"pending", "unknown"}
                             and effect_result_ref is None)
                            or (effect_state in {"confirmed", "not_applied"}
                                and type(effect_result_ref) is str
                                and 1 <= len(effect_result_ref) <= 2048)))),
              "worker_checkpoint_invalid")
        body = {
            "worker_id": worker_id,
            "lease_token": lease_token,
            "lease_generation": lease_generation,
            "attempt_id": attempt_id,
            "checkpoint_id": checkpoint_id,
            "sequence": sequence,
            "stage": stage,
            "artifact_kind": artifact_kind,
            "artifact_ref": artifact_ref,
            "artifact_sha256": artifact_sha256,
            "next_action": next_action,
            "effect_id": effect_id,
            "effect_state": effect_state,
            "effect_result_ref": effect_result_ref,
            "correlation_id": correlation_id,
        }
        expected = {
            key: value for key, value in body.items()
            if key not in {"worker_id", "lease_token"}
        }
        try:
            reply = self._request(
                "POST", f"/v1/tasks/{task_id}/checkpoints", body,
                expected_statuses=(200, 201),
            )
        except NativeExecutorBlocked as exc:
            if exc.args != ("worker_transport_unverified",):
                raise
            checkpoint = self.get_latest_checkpoint(task_id)
            _need(self._checkpoint_matches(checkpoint, expected),
                  "worker_checkpoint_ambiguous")
            return checkpoint
        _need(set(reply) == {"created", "checkpoint"}
              and type(reply["created"]) is bool
              and self._checkpoint_matches(reply["checkpoint"], expected),
              "worker_checkpoint_response_invalid")
        independent = self.get_latest_checkpoint(task_id)
        _need(self._checkpoint_matches(independent, expected),
              "worker_checkpoint_readback_mismatch")
        return independent

    def _mutate(self, task_id: str, worker_id: str, lease_token: str,
                correlation_id: str, operation: str, *,
                produced_sha: str | None = None,
                reason: str | None = None,
                lease_generation: int | None = None,
                attempt_id: str | None = None) -> None:
        _worker(worker_id, correlation_id)
        _id(task_id)
        _need(type(lease_token) is str and 16 <= len(lease_token) <= 128,
              "worker_lease_invalid")
        _need(operation in {"start", "validation", "complete", "fail", "block"},
              "worker_operation_invalid")
        if operation == "validation":
            _need(type(produced_sha) is str and bool(SHA40.fullmatch(produced_sha)),
                  "worker_validation_sha_invalid")
        if operation in {"fail", "block"}:
            _need(type(reason) is str and bool(re.fullmatch(r"[a-z_]{3,64}", reason)),
                  "worker_failure_reason_invalid")
        body: dict[str, Any] = {"worker_id": worker_id,
                                "lease_token": lease_token,
                                "correlation_id": correlation_id}
        _need((lease_generation is None and attempt_id is None)
              or (type(lease_generation) is int
                  and not isinstance(lease_generation, bool)
                  and lease_generation >= 1
                  and type(attempt_id) is str
                  and bool(SAFE_ID.fullmatch(attempt_id))),
              "worker_lease_fence_invalid")
        if lease_generation is not None:
            body.update(lease_generation=lease_generation, attempt_id=attempt_id)
        if operation == "validation":
            body["produced_sha"] = produced_sha
        if operation in {"fail", "block"}:
            body["reason"] = reason
        after = self._task(
            self._request("POST", f"/v1/tasks/{task_id}/{operation}", body), task_id,
        )
        expected_states = {
            "start": {"running"}, "validation": {"validating"},
            "complete": {"completed"}, "fail": {"queued", "blocked", "failed"},
            "block": {"blocked"},
        }[operation]
        _need(after["state"] in expected_states, "worker_mutation_unverified")
        independent = self.get_task(task_id)
        _need(independent["state"] == after["state"]
              and independent.get("produced_sha") == after.get("produced_sha"),
              "worker_mutation_readback_mismatch")

    def start(self, task_id: str, worker_id: str, lease_token: str,
              correlation_id: str, *, lease_generation: int | None = None,
              attempt_id: str | None = None) -> None:
        self._mutate(task_id, worker_id, lease_token, correlation_id, "start",
                     lease_generation=lease_generation, attempt_id=attempt_id)

    def submit_validation(self, task_id: str, worker_id: str, lease_token: str,
                          produced_sha: str, correlation_id: str, *,
                          lease_generation: int | None = None,
                          attempt_id: str | None = None) -> None:
        self._mutate(task_id, worker_id, lease_token, correlation_id,
                     "validation", produced_sha=produced_sha,
                     lease_generation=lease_generation, attempt_id=attempt_id)

    def complete(self, task_id: str, worker_id: str, lease_token: str,
                 correlation_id: str, *, lease_generation: int | None = None,
                 attempt_id: str | None = None) -> None:
        self._mutate(task_id, worker_id, lease_token, correlation_id, "complete",
                     lease_generation=lease_generation, attempt_id=attempt_id)

    def fail(self, task_id: str, worker_id: str, lease_token: str,
             correlation_id: str, reason: str, *,
             lease_generation: int | None = None,
             attempt_id: str | None = None) -> None:
        self._mutate(task_id, worker_id, lease_token, correlation_id,
                     "fail", reason=reason, lease_generation=lease_generation,
                     attempt_id=attempt_id)

    def block(self, task_id: str, worker_id: str, lease_token: str,
              correlation_id: str, reason: str, *,
              lease_generation: int | None = None,
              attempt_id: str | None = None) -> None:
        self._mutate(task_id, worker_id, lease_token, correlation_id,
                     "block", reason=reason, lease_generation=lease_generation,
                     attempt_id=attempt_id)
