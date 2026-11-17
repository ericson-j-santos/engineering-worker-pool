"""Isolated typed-binding contracts; never simulate a physical Noteri receipt."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.native_gateway_commands import GovernedCommand, NativeGatewayCommandRegistry
from app.native_one_shot import (
    AdapterContext, AdapterResult, ExecutionBoundary, LocalWorkspace,
    NativeExecutorBlocked, NativeOneShotExecutor, ReceiptStore, TaskManifest,
)
from app.noteri42_native_binding import (
    ADAPTER_ID, BASE_SHA, COMMIT_KEY, COMMIT_MESSAGE, DESCRIPTOR, ISSUE_NUMBER,
    MODEL_KEY, MODEL_SCRIPT, REPOSITORY, SOURCE_PATH, STAGE_KEY, VALIDATOR_KEY,
    VALIDATOR_SCRIPT, _ScopedAdapter, bind_noteri42_executor,
)

TASK_ID = "cwp-" + "a" * 24
PRODUCED = "b" * 40


class InstalledAdapter:
    descriptor = DESCRIPTOR

    def build(self, ctx):
        for key in (MODEL_KEY, STAGE_KEY, COMMIT_KEY):
            ctx.invoke(key)
        return AdapterResult(PRODUCED, (SOURCE_PATH,), tuple(ctx.invoked), "b" * 64)

    def validate(self, ctx, produced_sha):
        ctx.invoke(VALIDATOR_KEY)
        return AdapterResult(produced_sha, (SOURCE_PATH,), tuple(ctx.invoked), "b" * 64)


class StandInRegistry(NativeGatewayCommandRegistry):
    # Contract-only stand-in: constructor deliberately does not attest Gateway.
    def __init__(self, root, head=BASE_SHA):
        self.boundary = ExecutionBoundary("session-123", "noteri42-dev-1234",
                                          head, True, True)
        self.workspace = root
        self.specs = {
            MODEL_KEY: GovernedCommand(MODEL_KEY, "builder",
                                       ("python", MODEL_SCRIPT), 2,
                                       script_sha256="1" * 64),
            STAGE_KEY: GovernedCommand(STAGE_KEY, "builder",
                                       ("git", "add", "--", SOURCE_PATH), 2,
                                       allow_dirty=True),
            COMMIT_KEY: GovernedCommand(COMMIT_KEY, "builder",
                                        ("git", "commit", "-m", COMMIT_MESSAGE), 2,
                                        allow_dirty=True, allow_head_change=True),
            VALIDATOR_KEY: GovernedCommand(VALIDATOR_KEY, "validator",
                                           ("python", VALIDATOR_SCRIPT), 1,
                                           script_sha256="2" * 64),
        }
        self.for_role_calls = []

    def for_role(self, descriptor, role):
        self.for_role_calls.append(role)
        return {key: (lambda: None) for key in self.specs}


class BindingContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.registry = StandInRegistry(self.root)
        self.api = Mock()
        self.api.get_task.return_value = {
            "task_id": TASK_ID, "repository": REPOSITORY, "base_sha": BASE_SHA,
            "issue_number": ISSUE_NUMBER, "fencing_required": True,
            "request_id": "noteri42-req-1", "idempotency_key": "noteri42-key-1",
        }
        self.baseline = {
            "issue_number": ISSUE_NUMBER, "repository": REPOSITORY,
            "base_sha": BASE_SHA, "result": "reproduced_failure",
            "independent_readback": True, "test_id": "test_real_failure",
            "evidence_sha256": "c" * 64,
        }
        self.workspace = LocalWorkspace(self.root)
        self.receipts = ReceiptStore(self.root / "receipts")
        self.cas = Mock()
        self.adapter = InstalledAdapter()

    def bind(self, **changes):
        args = {
            "registry": self.registry, "installed_adapter": self.adapter,
            "api": self.api, "task_id": TASK_ID,
            "baseline_reader": lambda: self.baseline,
            "workspace": self.workspace, "receipts": self.receipts,
            "artifact_transport": self.cas, "role": "builder",
        }
        args.update(changes)
        return bind_noteri42_executor(**args)

    def blocked(self, **kwargs):
        with self.assertRaises(NativeExecutorBlocked):
            self.bind(**kwargs)

    def test_positive_wiring_does_not_claim_or_invoke_model(self):
        result = self.bind()
        self.assertIsInstance(result, NativeOneShotExecutor)
        self.assertEqual(result.fenced_adapter_ids, frozenset({ADAPTER_ID}))
        self.assertEqual(set(result.adapters), {ADAPTER_ID})
        self.assertEqual(self.registry.for_role_calls, ["builder"])
        self.api.claim_exact.assert_not_called()
        self.api.claim_fenced_exact.assert_not_called()
        self.cas.publish.assert_not_called()

    def test_wrong_issue_rejected(self):
        self.api.get_task.return_value["issue_number"] = 41
        self.blocked()

    def test_wrong_source_sha_rejected(self):
        self.api.get_task.return_value["base_sha"] = "f" * 40
        self.blocked()

    def test_unfenced_task_rejected(self):
        self.api.get_task.return_value["fencing_required"] = False
        self.blocked()

    def test_canonical_identity_required(self):
        self.api.get_task.return_value.pop("idempotency_key")
        self.blocked()

    def test_stale_baseline_rejected(self):
        self.baseline["base_sha"] = "f" * 40
        self.blocked()

    def test_unverified_failure_rejected(self):
        self.baseline["independent_readback"] = False
        self.blocked()

    def test_wrong_model_command_rejected(self):
        self.registry.specs[MODEL_KEY] = GovernedCommand(
            MODEL_KEY, "builder", ("python", "scripts/other.py"), 2,
            script_sha256="1" * 64)
        self.blocked()

    def test_missing_script_digest_rejected(self):
        self.registry.specs[MODEL_KEY] = GovernedCommand(
            MODEL_KEY, "builder", ("python", MODEL_SCRIPT), 2)
        self.blocked()

    def test_wrong_stage_path_rejected(self):
        self.registry.specs[STAGE_KEY] = GovernedCommand(
            STAGE_KEY, "builder", ("git", "add", "--", "app/main.py"),
            2, allow_dirty=True)
        self.blocked()

    def test_missing_durable_transport_rejected(self):
        self.blocked(artifact_transport=None)

    def test_boundary_not_validated_rejected(self):
        self.registry.boundary = ExecutionBoundary(
            "session-123", "noteri42-dev-1234", BASE_SHA, False, True)
        self.blocked()

    def test_uninstalled_adapter_rejected(self):
        self.blocked(installed_adapter=object())

    def test_validator_registry_binding_is_separate(self):
        self.registry.boundary = ExecutionBoundary(
            "session-123", "noteri42-dev-1234", PRODUCED, True, True)
        self.workspace.head = lambda: PRODUCED
        result = self.bind(role="validator")
        self.assertIsInstance(result, NativeOneShotExecutor)
        self.assertEqual(self.registry.for_role_calls, ["validator"])

    def context(self, role="builder"):
        manifest = TaskManifest(
            1, TASK_ID, REPOSITORY, BASE_SHA,
            BASE_SHA if role == "builder" else PRODUCED,
            ADAPTER_ID, DESCRIPTOR.contract_sha256, {SOURCE_PATH: "d" * 64},
        )
        return AdapterContext(
            self.root, manifest, DESCRIPTOR,
            {key: (lambda: None) for key in DESCRIPTOR.command_keys}, role,
        )

    def test_scoped_builder_checks_exact_invocations(self):
        ctx = self.context()
        result = _ScopedAdapter(InstalledAdapter(), TASK_ID).build(ctx)
        self.assertEqual(result.changed_paths, (SOURCE_PATH,))
        self.assertEqual(result.invoked_commands, (MODEL_KEY, STAGE_KEY, COMMIT_KEY))

    def test_scoped_builder_rejects_fabricated_output(self):
        class Forged(InstalledAdapter):
            def build(self, ctx):
                return AdapterResult(PRODUCED, (SOURCE_PATH,),
                                     (MODEL_KEY, STAGE_KEY, COMMIT_KEY), "a" * 64)
        with self.assertRaises(NativeExecutorBlocked):
            _ScopedAdapter(Forged(), TASK_ID).build(self.context())

    def test_scoped_validator_checks_exact_role(self):
        ctx = self.context("validator")
        result = _ScopedAdapter(InstalledAdapter(), TASK_ID).validate(ctx, PRODUCED)
        self.assertEqual(result.invoked_commands, (VALIDATOR_KEY,))

    def test_scoped_validator_rejects_stale_sha(self):
        with self.assertRaises(NativeExecutorBlocked):
            _ScopedAdapter(InstalledAdapter(), TASK_ID).validate(
                self.context("validator"), "f" * 40)

    def test_no_second_scheduler_or_dispatcher(self):
        self.assertEqual(tuple(sorted(DESCRIPTOR.builder_commands)),
                         DESCRIPTOR.builder_commands)
        self.assertEqual(DESCRIPTOR.allowed_paths, (SOURCE_PATH,))


if __name__ == "__main__":
    unittest.main()
