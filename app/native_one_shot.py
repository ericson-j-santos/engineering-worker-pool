_task_drift",
                                  **fence)
                except Exception:
                    pass
                raise NativeExecutorBlocked("claimed_task_drift")
            artifact: DurableArtifact | None = None
            try:
                if role == "builder":
                    self.api.start(manifest.task_id, worker_id, token,
                                   self.boundary.correlation_id, **fence)
                    if fenced and latest is not None:
                        _need(latest.get("stage") == "builder_result"
                              and latest.get("next_action") == "submit_validation",
                              "fenced_resume_checkpoint_unsupported")
                        artifact = self._artifact_from_checkpoint(latest)
                        result = self.artifact_transport.materialize(
                            task_id=manifest.task_id, manifest=manifest,
                            workspace=self.workspace,
                            artifact=artifact,
                        )
                        self._validate_recovered_result(result, descriptor)
                    else:
                        result = adapter.build(context)
                        self._validate_result(result, descriptor, context, role)
                    _need(self.workspace.head() == result.produced_sha,
                          "builder_produced_sha_drift")
                    _need(self.workspace.status() == "", "builder_workspace_dirty")
                    _need(self.workspace.changed_paths(manifest.base_sha, result.produced_sha)
                          == result.changed_paths, "builder_diff_mismatch")
                    if latest is None:
                        _need(set(context.invoked) == set(descriptor.commands_for(role)),
                              "required_command_missing")
                    if fenced:
                        effect_id = self._artifact_effect_id(
                            manifest.task_id, result.produced_sha,
                        )
                        if latest is not None:
                            _need(latest.get("effect_id") == effect_id,
                                  "artifact_effect_identity_drift")
                        if artifact is None:
                            self.api.record_checkpoint(
                                manifest.task_id, worker_id, token, generation, attempt_id,
                                self._checkpoint_id(attempt_id, role, "artifact-pending"),
                                1, "artifact_publish", "publish_artifact",
                                self.boundary.correlation_id, effect_id=effect_id,
                                effect_state="pending",
                            )
                            try:
                                raw_artifact = self.artifact_transport.publish(
                                    task_id=manifest.task_id,
                                    manifest=manifest,
                                    lease_generation=generation,
                                    attempt_id=attempt_id,
                                    effect_id=effect_id,
                                    workspace=self.workspace,
                                    result=result,
                                )
                                artifact = DurableArtifact.parse(
                                    raw_artifact.checkpoint_fields()
                                    if type(raw_artifact) is DurableArtifact
                                    else raw_artifact
                                )
                                self.artifact_transport.verify(
                                    task_id=manifest.task_id,
                                    manifest=manifest,
                                    workspace=self.workspace,
                                    produced_sha=result.produced_sha,
                                    artifact=artifact,
                                )
                            except Exception:
                                # Publication may already have happened. Mark the
                                # effect unknown and block; never retry or fail it.
                                try:
                                    self.api.record_checkpoint(
                                        manifest.task_id, worker_id, token,
                                        generation, attempt_id,
                                        self._checkpoint_id(
                                            attempt_id, role, "artifact-unknown",
                                        ),
                                        2, "artifact_publish",
                                        "reconcile_artifact",
                                        self.boundary.correlation_id,
                                        effect_id=effect_id,
                                        effect_state="unknown",
                                    )
                                except Exception:
                                    pass
                                try:
                                    self.api.block(
                                        manifest.task_id, worker_id, token,
                                        self.boundary.correlation_id,
                                        "artifact_publish_ambiguous", **fence,
                                    )
                                except Exception:
                                    pass
                                raise NativeExecutorBlocked(
                                    "artifact_publish_ambiguous"
                                ) from None
                        else:
                            self.artifact_transport.verify(
                                task_id=manifest.task_id, manifest=manifest,
                                workspace=self.workspace,
                                produced_sha=result.produced_sha, artifact=artifact,
                            )
                        self.api.record_checkpoint(
                            manifest.task_id, worker_id, token, generation, attempt_id,
                            self._checkpoint_id(attempt_id, role, "result"),
                            2 if latest is None else 1, "builder_result",
                            "submit_validation", self.boundary.correlation_id,
                            **artifact.checkpoint_fields(),
                            effect_id=effect_id, effect_state="confirmed",
                            effect_result_ref=artifact.artifact_ref,
                        )
                    self.api.submit_validation(manifest.task_id, worker_id, token,
                                               result.produced_sha,
                                               self.boundary.correlation_id, **fence)
                else:
                    produced = claimed_task.get("produced_sha")
                    _need(type(produced) is str and bool(SHA40.fullmatch(produced)),
                          "validation_sha_missing")
                    _need(manifest.workspace_sha == produced, "validator_workspace_sha_drift")
                    if fenced:
                        _need(latest is not None
                              and latest.get("stage") == "builder_result"
                              and latest.get("next_action") == "submit_validation",
                              "validator_artifact_checkpoint_missing")
                        artifact = self._artifact_from_checkpoint(latest)
                        self.artifact_transport.verify(
                            task_id=manifest.task_id, manifest=manifest,
                            workspace=self.workspace,
                            produced_sha=produced, artifact=artifact,
                        )
                    before_head, before_status = self.workspace.head(), self.workspace.status()
                    result = adapter.validate(context, produced)
                    self._validate_result(result, descriptor, context, role)
                    _need(result.produced_sha == produced
                          and self.workspace.head() == before_head == produced
                          and self.workspace.status() == before_status == "",
                          "validator_sha_drift")
                    _need(set(context.invoked) == set(descriptor.commands_for(role)),
                          "required_command_missing")
                    if fenced:
                        self.api.record_checkpoint(
                            manifest.task_id, worker_id, token, generation, attempt_id,
                            self._checkpoint_id(attempt_id, role, "result"),
                            1, "validator_result",
                            "complete", self.boundary.correlation_id,
                            **artifact.checkpoint_fields(),
                        )
                    self.api.complete(manifest.task_id, worker_id, token,
                                      self.boundary.correlation_id, **fence)
            except Exception as exc:
                allowed = {"adapter_result_sha", "adapter_result_paths",
                           "adapter_result_commands", "adapter_result_evidence",
                           "builder_produced_sha_drift", "builder_workspace_dirty",
                           "builder_diff_mismatch", "required_command_missing",
                           "validation_sha_missing", "validator_workspace_sha_drift",
                           "validator_sha_drift", "command_not_allowlisted",
                           "artifact_publish_ambiguous"}
                candidate = exc.args[0] if isinstance(exc, NativeExecutorBlocked) else None
                reason = candidate if candidate in allowed else "adapter_failed"
                fail_rejected = False
                if candidate != "artifact_publish_ambiguous":
                    try:
                        self.api.fail(manifest.task_id, worker_id, token,
                                      self.boundary.correlation_id, str(reason), **fence)
                    except Exception:
                        # A stale failure is expected after lease loss. Preserve
                        # a closed original error rather than replacing it.
                        fail_rejected = True
                if fail_rejected and candidate is not None:
                    raise
                if candidate not in allowed:
                    raise NativeExecutorBlocked("adapter_failed") from None
                raise
            receipt = {
                "schema_version": 2 if fenced else 1,
                "result": "NATIVE_ONE_SHOT_BUILT" if role == "builder"
                          else "NATIVE_ONE_SHOT_VALIDATED",
                "task_id": manifest.task_id, "role": role, "worker_id": worker_id,
                "manifest_sha256": _digest(raw_manifest),
                "adapter_contract_sha256": descriptor.contract_sha256,
                "produced_sha": result.produced_sha,
                "evidence_sha256": result.evidence_sha256,
                "changed_paths": list(result.changed_paths),
                "invoked_commands": list(result.invoked_commands),
                "session_id": self.boundary.session_id,
                "correlation_id": self.boundary.correlation_id,
                "gateway_verified": True, "replayed": False,
                "workspace_id": hashlib.sha256(str(self.workspace.root).encode()).hexdigest(),
            }
            if fenced:
                receipt.update({
                    "fencing_required": True,
                    "lease_generation": generation,
                    "attempt_id": attempt_id,
                    **artifact.checkpoint_fields(),
                })
            self.receipts.write(receipt_path, receipt)
            return receipt
        finally:
            lock.unlink(missing_ok=True)
