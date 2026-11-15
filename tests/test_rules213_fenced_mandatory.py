"""Rules213 native agent must NEVER fall back to the unfenced legacy queue.

This test uses an in-memory fake API, isolated temporary receipts and a
fake workspace. No HTTP, credentials, job dispatch or model calls occur.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.native_one_shot import (
    ExecutionBoundary, NativeExecutorBlocked, NativeOneShotExecutor, ReceiptStore,
)
from app.rules213_native_adapter import NATIVE_DESCRIPTOR, NATIVE_PROFILE
from app.rules213_updater_profile import BASE_SHA, REPOSITORY, SOURCE_FUNCTIONS

TASK_ID = "cwp-" + "a" * 24


class ObservedApi:
    def __init__(self, fencing):
        self.fencing = fencing
        self.legacy_claims = 0
        self.fenced_claims = 0

    def get_task(self, task_id):
        assert task_id == TASK_ID
        return {"task_id": TASK_ID, "base_sha": BASE_SHA,
                "repository": REPOSITORY, "fencing_required": self.fencing}

    def claim_exact(self, *args, **kwargs):
        self.legacy_claims += 1
        raise AssertionError("LEGACY_CLAIM_MUST_NOT_BE_CALLED_FOR_RULES213")

    def claim_fenced_exact(self, *args, **kwargs):
        self.fenced_claims += 1
        raise AssertionError("FENCED_CLAIM_REACHED_TEST_STUB")


class FakeWorkspace:
    def __init__(self, root):
        self.root = root
        self.validated = 0

    def validate_before_claim(self, manifest, descriptor):
        self.validated += 1


class FakeAdapter:
    descriptor = NATIVE_DESCRIPTOR

    def build(self, context):
        raise AssertionError("REPAIR_COMMAND_MUST_NOT_RUN")

    def validate(self, context, sha):
        raise AssertionError("VALIDATOR_COMMAND_MUST_NOT_RUN")


class FakeCAS:
    def preflight(self):
        return None


class FencedRules213Tests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.manifest = {
            "schema_version": 1,
            "task_id": TASK_ID, "repository": REPOSITORY,
            "base_sha": BASE_SHA, "workspace_sha": BASE_SHA,
            "adapter_id": NATIVE_PROFILE,
            "adapter_contract_sha256": NATIVE_DESCRIPTOR.contract_sha256,
            "before_sha256": {path: "0" * 64 for path in SOURCE_FUNCTIONS},
        }
        self.workspace = FakeWorkspace(self.root)
        self.receipts = ReceiptStore(self.root / "receipts")
        self.commands = {key: lambda: None for key in NATIVE_DESCRIPTOR.command_keys}

    def executor(self, api, *, attest=False, cas=False):
        return NativeOneShotExecutor(
            api=api, workspace=self.workspace,
            receipts=self.receipts,
            adapters={NATIVE_PROFILE: FakeAdapter()},
            command_registries={NATIVE_PROFILE: self.commands},
            boundary=ExecutionBoundary(
                session_id="ewp-r213-fence-tests",
                correlation_id="ewp-r213-fence-test-20261115",
                expected_head=BASE_SHA,
                state_validated=True, gateway_verified=True),
            artifact_transport=FakeCAS() if cas else None,
            fenced_adapter_ids=frozenset({NATIVE_PROFILE}) if attest
            else frozenset(),
        )

    def test_rejects_explicit_unfenced_task_before_any_legacy_claim(self):
        api = ObservedApi(False)
        with self.assertRaisesRegex(NativeExecutorBlocked, "rules213_requires_fenced_task"):
            self.executor(api).run(self.manifest, role="builder", worker_id="builder.dev")
        self.assertEqual(api.legacy_claims, 0)
        self.assertEqual(api.fenced_claims, 0)

    def test_rejects_sqlite_zero_fencing_before_any_legacy_claim(self):
        api = ObservedApi(0)
        with self.assertRaisesRegex(NativeExecutorBlocked, "rules213_requires_fenced_task"):
            self.executor(api).run(self.manifest, role="builder", worker_id="builder.dev")
        self.assertEqual(api.legacy_claims, 0)

    def test_rejects_fenced_task_without_trusted_adapter_attestation(self):
        api = ObservedApi(True)
        with self.assertRaisesRegex(NativeExecutorBlocked, "fenced_adapter_not_attested"):
            self.executor(api).run(self.manifest, role="builder", worker_id="builder.dev")
        self.assertEqual(api.legacy_claims, 0)
        self.assertEqual(api.fenced_claims, 0)

    def test_rejects_fenced_task_without_durable_cas(self):
        api = ObservedApi(True)
        with self.assertRaisesRegex(NativeExecutorBlocked, "durable_artifact_transport_required"):
            self.executor(api, attest=True).run(
                self.manifest, role="builder", worker_id="builder.dev")
        self.assertEqual(api.legacy_claims, 0)
        self.assertEqual(api.fenced_claims, 0)

    def test_routes_only_to_fenced_claim_after_all_preconditions(self):
        api = ObservedApi(True)
        with self.assertRaisesRegex(AssertionError, "FENCED_CLAIM_REACHED_TEST_STUB"):
            self.executor(api, attest=True, cas=True).run(
                self.manifest, role="builder", worker_id="builder.dev")
        self.assertEqual(api.legacy_claims, 0)
        self.assertEqual(api.fenced_claims, 1)


if __name__ == "__main__":
    unittest.main()
