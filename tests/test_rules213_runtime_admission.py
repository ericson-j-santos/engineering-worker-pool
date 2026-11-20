"""Rules213 authority/CAS admission contracts; no external provider or physical claim."""
from __future__ import annotations

import unittest
from unittest.mock import Mock

from app.native_one_shot import (
    AdapterResult, DurableArtifact, ExecutionBoundary, NativeExecutorBlocked,
    TaskManifest,
)
from app.rules213_native_adapter import NATIVE_DESCRIPTOR, NATIVE_PROFILE
from app.rules213_runtime_admission import Rules213RuntimeAuthority, Rules213ScopedCAS
from app.rules213_updater_profile import BASE_SHA, ISSUE, REPOSITORY, SOURCE_FUNCTIONS
from app.s3_git_bundle_artifact import S3ArtifactConfig, S3GitBundleArtifactTransport

WORKER_SHA = "4c25083841863f2341cd3cb59310b5d4c31ac5c4"
AUTH_ID = "1226e0ad-0923-48b1-806a-f013ac1e03e8"
TASK = "cwp-" + "a" * 24
PRODUCED = "b" * 40


class FakeObjectStore:
    def put_object(self, **kwargs):
        raise AssertionError("NO_STORAGE_WRITE_IN_CONTRACT_TEST")

    def get_object(self, **kwargs):
        raise AssertionError("NO_STORAGE_READ_IN_CONTRACT_TEST")


class Rules213CASAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.issue = {"number": ISSUE, "repository": REPOSITORY, "state": "open"}
        self.worker = {
            "authority_id": AUTH_ID, "source_sha": WORKER_SHA,
            "fenced_cutover_enabled": True,
            "legacy_claimers_drained": True,
            "gateway_verified": True, "state_validated": True,
            "session_launch_ok": True,
        }
        self.cost = {
            "incremental_brl": 0, "provider_verified": True,
            "hard_stop_verified": True,
        }
        self.main = BASE_SHA
        self.authority = Rules213RuntimeAuthority(
            lambda: self.main, lambda: self.issue,
            lambda: self.worker, lambda: self.cost, WORKER_SHA,
        )
        self.backend = S3GitBundleArtifactTransport(
            S3ArtifactConfig(bucket="test-cas", region="github",
                             key_prefix="rules213-cas-v1"), FakeObjectStore(),
        )
        self.cas = Rules213ScopedCAS(self.backend, self.authority)
        self.manifest = TaskManifest.parse({
            "schema_version": 1, "task_id": TASK,
            "repository": REPOSITORY, "base_sha": BASE_SHA,
            "workspace_sha": BASE_SHA, "adapter_id": NATIVE_PROFILE,
            "adapter_contract_sha256": NATIVE_DESCRIPTOR.contract_sha256,
            "before_sha256": {path: "c" * 64 for path in SOURCE_FUNCTIONS},
        })
        self.result = AdapterResult(
            PRODUCED, tuple(sorted(SOURCE_FUNCTIONS)), ("rules213.propose",),
            "d" * 64,
        )

    def test_admission_checks_identity_without_claim_or_cas_write(self):
        self.authority.check()
        self.cas._manifest(self.manifest, TASK, builder=True)
        self.assertEqual(self.backend.client.__class__, FakeObjectStore)

    def test_main_issue_and_worker_source_drift_fail_closed(self):
        cases = [
            ("main", "f" * 40),
            ("issue", dict(self.issue, state="closed")),
            ("worker", dict(self.worker, source_sha="e" * 40)),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                original = getattr(self, field)
                setattr(self, field, value)
                with self.assertRaises(NativeExecutorBlocked):
                    self.authority.check()
                setattr(self, field, original)

    def test_fencing_or_session_absent_blocks(self):
        for key in ("fenced_cutover_enabled", "legacy_claimers_drained",
                    "gateway_verified", "state_validated", "session_launch_ok"):
            with self.subTest(key=key):
                self.worker[key] = False
                with self.assertRaisesRegex(
                    NativeExecutorBlocked, "rules213_worker_authority_unverified"
                ):
                    self.authority.check()
                self.worker[key] = True

    def test_cost_uncertainty_blocks_even_when_numeric_zero(self):
        for changed in (
            {"incremental_brl": None},
            {"incremental_brl": False},
            {"incremental_brl": 1},
            {"hard_stop_verified": False},
            {"provider_verified": False},
        ):
            with self.subTest(changed=changed):
                original = self.cost.copy()
                self.cost.update(changed)
                with self.assertRaisesRegex(
                    NativeExecutorBlocked, "rules213_cost_unverified"
                ):
                    self.authority.check()
                self.cost = original

    def test_legacy_qwen_cas_profile_is_not_reusable(self):
        foreign = S3GitBundleArtifactTransport(
            S3ArtifactConfig(bucket="test-cas", region="github",
                             key_prefix="qwen7b-cas-v1"), FakeObjectStore(),
        )
        with self.assertRaisesRegex(NativeExecutorBlocked,
                                    "rules213_foreign_cas_profile"):
            Rules213ScopedCAS(foreign, self.authority)

    def test_wrong_profile_repository_path_or_task_blocks(self):
        original = self.manifest
        changes = [
            {"repository": "foreign/repo"},
            {"adapter_id": "bridge.property.v1"},
            {"task_id": "cwp-" + "b" * 24},
            {"before_sha256": {"app/portfolio_bridge.py": "c" * 64}},
        ]
        for update in changes:
            with self.subTest(update=update):
                raw = {
                    "schema_version": 1, "task_id": TASK, "repository": REPOSITORY,
                    "base_sha": BASE_SHA, "workspace_sha": BASE_SHA,
                    "adapter_id": NATIVE_PROFILE,
                    "adapter_contract_sha256": NATIVE_DESCRIPTOR.contract_sha256,
                    "before_sha256": {path: "c" * 64 for path in SOURCE_FUNCTIONS},
                }
                raw.update(update)
                changed = TaskManifest.parse(raw)
                with self.assertRaises(NativeExecutorBlocked):
                    self.cas._manifest(changed, TASK, builder=True)
        self.assertIs(self.manifest, original)

    def test_no_publish_when_candidate_contains_foreign_path(self):
        bad = AdapterResult(PRODUCED, ("app/portfolio_bridge.py",),
                            ("rules213.propose",), "d" * 64)
        with self.assertRaisesRegex(NativeExecutorBlocked,
                                    "rules213_cas_patch_scope_invalid"):
            self.cas.publish(task_id=TASK, manifest=self.manifest,
                             lease_generation=1, attempt_id="attempt-1",
                             effect_id="effect-1", workspace=Mock(), result=bad)

    def test_publish_and_verify_delegate_without_bypassing_scope(self):
        backend = Mock(spec=S3GitBundleArtifactTransport)
        cas = object.__new__(Rules213ScopedCAS)
        cas.transport = backend
        cas.authority = self.authority
        artifact = DurableArtifact("fixture", "fixture://artifact", "e" * 64)
        backend.publish.return_value = artifact
        result = cas.publish(task_id=TASK, manifest=self.manifest,
                             lease_generation=1, attempt_id="attempt-1",
                             effect_id="effect-1", workspace=Mock(), result=self.result)
        self.assertEqual(result, artifact)
        backend.publish.assert_called_once()
        cas.verify(task_id=TASK, manifest=self.manifest, workspace=Mock(),
                   produced_sha=PRODUCED, artifact=artifact)
        backend.verify.assert_called_once()

    def test_validator_manifest_bound_to_produced_sha(self):
        raw = {
            "schema_version": 1, "task_id": TASK, "repository": REPOSITORY,
            "base_sha": BASE_SHA, "workspace_sha": PRODUCED,
            "adapter_id": NATIVE_PROFILE,
            "adapter_contract_sha256": NATIVE_DESCRIPTOR.contract_sha256,
            "before_sha256": {path: "c" * 64 for path in SOURCE_FUNCTIONS},
        }
        validator = TaskManifest.parse(raw)
        self.cas._manifest(validator, TASK, produced_sha=PRODUCED)
        with self.assertRaises(NativeExecutorBlocked):
            self.cas._manifest(validator, TASK, produced_sha="f" * 40)

    def test_replay_scope_readback_is_deterministic(self):
        self.cas._manifest(self.manifest, TASK, builder=True)
        self.cas._manifest(self.manifest, TASK, builder=True)
        self.authority.check()


if __name__ == "__main__":
    unittest.main()
