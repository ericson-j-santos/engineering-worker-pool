"""Offline controls. These tests never claim real host or provider readiness."""
from __future__ import annotations
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hosted_rehearsal", ROOT / "scripts/hosted_rehearsal.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class CapsuleTests(unittest.TestCase):
    def setUp(self):
        self.context = dict(source_sha="a"*40, rules_sha=module.RULES_SHA,
                            run_id="12345", run_attempt="1")
        db = b"SQLite format 3\x00" + b"fixture-only"
        artifact = {"stage":"one","result":module.digest(module.PUBLIC_INPUT)}
        self.value = dict(self.context, schema=module.MAGIC, producer_host="producer-test",
                          task_id="cwp-"+"a"*24, old_generation=1, old_attempt="attempt-one",
                          artifact=artifact, artifact_sha256=module.digest(module.canonical(artifact)),
                          database=base64.b64encode(db).decode(), database_sha256=module.digest(db))

    def decode(self, value=None, context=None):
        raw, sha = module.encode_capsule(value or self.value)
        return module.decode_capsule(raw, sha, context or self.context)

    def test_roundtrip_is_deterministic(self):
        self.assertEqual(self.decode(), self.value)
        self.assertEqual(module.encode_capsule(self.value), module.encode_capsule(self.value))

    def test_corrupt_bytes_rejected(self):
        encoded, sha = module.encode_capsule(self.value)
        with self.assertRaises(module.RehearsalError):
            module.decode_capsule(encoded, "b"*64, self.context)

    def test_all_context_fields_bound(self):
        for key in self.context:
            wrong = dict(self.context, **{key:"2" if key != "source_sha" else "b"*40})
            with self.subTest(key=key), self.assertRaises(module.RehearsalError):
                self.decode(context=wrong)

    def test_unknown_missing_keys_and_schema(self):
        variants = [dict(self.value, arbitrary=True), dict(self.value, schema="wrong")]
        missing = dict(self.value)
        missing.pop("artifact")
        variants.append(missing)
        for value in variants:
            with self.assertRaises(module.RehearsalError):
                self.decode(value)

    def test_duplicate_json_rejected(self):
        with self.assertRaises(module.RehearsalError):
            module.strict_json(b'{"a":1,"a":2}\n')

    def test_noncanonical_json_rejected(self):
        with self.assertRaises(module.RehearsalError):
            module.strict_json(b'{ "a": 1 }\n')

    def test_decompression_bomb_rejected(self):
        encoded = base64.b64encode(zlib.compress(b"a"*(module.MAX_RAW+1))).decode()
        with self.assertRaises(module.RehearsalError):
            module.decode_capsule(encoded, "a"*64, self.context)

    def test_trailing_compressed_data_rejected(self):
        encoded, sha = module.encode_capsule(self.value)
        invalid = base64.b64encode(base64.b64decode(encoded)+b"trailing").decode()
        with self.assertRaises(module.RehearsalError):
            module.decode_capsule(invalid, sha, self.context)

    def test_generation_type_and_identity_are_strict(self):
        for generation in (True, "1", 0, 2, None):
            with self.assertRaises(module.RehearsalError):
                self.decode(dict(self.value, old_generation=generation))
        for key in ("task_id","old_attempt","producer_host"):
            with self.assertRaises(module.RehearsalError):
                self.decode(dict(self.value, **{key:"unsafe\nidentity"}))

    def test_artifact_divergence_rejected_even_with_outer_digest(self):
        for update in ({"artifact":{"stage":"one","result":"b"*64}},
                       {"artifact_sha256":"b"*64}):
            with self.assertRaises(module.RehearsalError):
                self.decode(dict(self.value, **update))

    def test_database_format_hash_and_encoding(self):
        for update in ({"database":"%%%"},
                       {"database":base64.b64encode(b"not-sqlite").decode()},
                       {"database_sha256":"b"*64}):
            with self.assertRaises(module.RehearsalError):
                self.decode(dict(self.value, **update))

    def test_encoded_size_and_digest_validation(self):
        for encoded, sha in (("", "a"*64), ("a"*(module.MAX_ENCODED+1),"a"*64),
                             ("invalid", "invalid")):
            with self.assertRaises(module.RehearsalError):
                module.decode_capsule(encoded, sha, self.context)

    def test_launch_receipt_must_bind_exact_sha_and_boolean(self):
        receipt = dict(result="SESSION_LAUNCH_OK", state_validated=True,
                       head=self.context["source_sha"], snapshot_sha256="a"*64,
                       session_id="rehearsal-test",
                       target_path=str(module.WORK_ROOT/"wt-chat-rehearsal-test"))
        self.assertEqual(module.validate_launch(receipt, self.context).name,
                         "wt-chat-rehearsal-test")
        for update in ({"state_validated":1},{"head":"b"*40},{"result":"ok"},
                       {"snapshot_sha256":""},{"session_id":"../escape"},
                       {"target_path":"/tmp/wt-chat-escape"}):
            with self.assertRaises(module.RehearsalError):
                module.validate_launch(dict(receipt, **update), self.context)

    def test_physical_host_cannot_run_entrypoint(self):
        with patch.dict(module.os.environ, {}, clear=True):
            with self.assertRaises(module.RehearsalError):
                module.context()

    def test_fixtures_cannot_be_claimed_as_live_failover(self):
        text = (ROOT/"scripts/hosted_rehearsal.py").read_text()
        self.assertIn('live_failover_verified=False', text)
        self.assertIn('s3_backend_verified=False', text)
        self.assertNotIn("boto3", text)
        self.assertNotIn("requests.", text)
        self.assertIn('TEST_REPOSITORY = "example/hosted-continuity-rehearsal"', text)

    def test_policy_preserves_security_controls(self):
        policy = json.loads((ROOT/"config/hosted-rehearsal-gateway.json").read_text())
        self.assertEqual(policy["platform"], "posix")
        self.assertTrue(policy["require_session_bootstrap"])
        self.assertTrue(policy["require_preflight_snapshot"])
        self.assertTrue(policy["deny_inline_code"])
        self.assertEqual(policy["governed_branch_updates"], [])
        self.assertEqual(policy["allowed_executables"], ["git","python","python3"])
        self.assertIn("sudo", policy["blocked_executables"])
        self.assertEqual(len(policy["allowed_roots"]), 2)

if __name__ == "__main__":
    unittest.main()
