"""Fail-closed controls for the disposable hosted cold-resume capsule."""
from __future__ import annotations
import base64
import copy
import importlib.util
import json
from pathlib import Path
import unittest
import zlib

SPEC = importlib.util.spec_from_file_location(
    "hosted_checkpoint_pure",
    Path(__file__).resolve().parents[1] / "scripts/hosted_checkpoint_continuity.py",
)
assert SPEC and SPEC.loader
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class CapsuleControls(unittest.TestCase):
    def setUp(self):
        self.identity = {"run": "1001", "attempt": "1", "sha": "a" * 40,
                         "rules_sha": m.RULES_SHA}
        payload = json.dumps({"value": 5, "source_sha": "a" * 40}, sort_keys=True)
        self.value = {
            "schema": "hosted-cold-resume/v1", "identity": self.identity,
            "producer": "GitHub Actions fixture-A", "task_id": "cwp-" + "b" * 24,
            "lease": {"worker_id": "cold-builder-a", "lease_generation": 1,
                      "lease_token": "disposable-test-only-lease",
                      "attempt_id": "fixture-attempt-1"},
            "payload": payload, "payload_sha256": m.digest(payload.encode()),
            # Encoding contract fixture only, never an actual database/E2E.
            "pool": base64.b64encode(b"SQLite format 3\x00fixture").decode(),
            "sink": base64.b64encode(b"SQLite format 3\x00fixture").decode(),
            "enqueue": {"repository": m.FIXTURE_REPOSITORY, "issue_number": 1,
                        "request_id": "cold-1001-1", "fencing_required": True,
                        "base_sha": "a" * 40},
        }

    def decode(self, value):
        encoded, checksum = m.pack(value)
        return m.unpack(encoded, checksum, self.identity)

    def test_roundtrip_idempotent_without_mutating_input(self):
        original = copy.deepcopy(self.value)
        self.assertEqual(self.decode(self.value), original)
        self.assertEqual(m.pack(self.value), m.pack(self.value))
        self.assertEqual(self.value, original)

    def test_wrong_digest_is_rejected(self):
        encoded, checksum = m.pack(self.value)
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            m.unpack(encoded, "0" * 64, self.identity)

    def test_cross_run_attempt_sha_and_rules_are_rejected(self):
        encoded, checksum = m.pack(self.value)
        for field in self.identity:
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "identity"):
                m.unpack(encoded, checksum, {**self.identity, field: "wrong"})

    def test_invalid_encoding_digest_and_size_are_rejected(self):
        for value, checksum in [("", "a" * 64), ("not base64!", "a" * 64),
                                ("abc", "invalid"), ("a" * (m.MAX_CAPSULE + 1), "a" * 64)]:
            with self.subTest(length=len(value)), self.assertRaises(RuntimeError):
                m.unpack(value, checksum, self.identity)

    def test_missing_or_extra_field_is_rejected(self):
        for key in self.value:
            altered = copy.deepcopy(self.value)
            del altered[key]
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.decode(altered)
        with self.assertRaises(RuntimeError):
            self.decode({**self.value, "command": "not permitted"})

    def test_production_scope_and_old_lease_are_rejected(self):
        for parent, key, bad in [
            ("enqueue", "repository", "ericson-j-santos/reqsys-v2-enterprise-real"),
            ("enqueue", "issue_number", 2546), ("enqueue", "fencing_required", False),
            ("enqueue", "base_sha", "c" * 40), ("enqueue", "request_id", "other"),
            ("lease", "lease_generation", 2), ("lease", "lease_generation", True),
            ("lease", "worker_id", "other"), ("lease", "lease_token", "short"),
            ("lease", "attempt_id", "../escape"),
        ]:
            altered = copy.deepcopy(self.value)
            altered[parent][key] = bad
            with self.subTest(parent=parent, key=key, bad=bad), self.assertRaises(RuntimeError):
                self.decode(altered)

    def test_bad_types_are_controlled_errors(self):
        for key in ("producer", "task_id", "lease", "payload", "pool", "enqueue"):
            altered = copy.deepcopy(self.value)
            altered[key] = None
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.decode(altered)

    def test_tampered_payload_and_database_rejected(self):
        for key, bad in [("payload", "{}"), ("pool", "broken"),
                         ("sink", base64.b64encode(b"not a database").decode())]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.decode({**self.value, key: bad})

    def test_trailing_compressed_stream_is_rejected(self):
        encoded, checksum = m.pack(self.value)
        bad = base64.b64encode(base64.b64decode(encoded) + zlib.compress(b"extra")).decode()
        with self.assertRaisesRegex(RuntimeError, "compression"):
            m.unpack(bad, checksum, self.identity)

    def test_decompression_bomb_is_bounded(self):
        raw = b"a" * (m.MAX_RAW + 1)
        encoded = base64.b64encode(zlib.compress(raw)).decode()
        with self.assertRaisesRegex(RuntimeError, "compression"):
            m.unpack(encoded, m.digest(raw), self.identity)

    def test_encoder_refuses_large_raw_data(self):
        with self.assertRaisesRegex(RuntimeError, "raw_too_large"):
            m.pack({"oversize": "x" * m.MAX_RAW})

    def test_unqualified_environment_cannot_start(self):
        from unittest.mock import patch
        with patch.dict(m.os.environ, {}, clear=True), \
                patch.object(m, "git", side_effect=AssertionError("no command before admission")):
            with self.assertRaisesRegex(RuntimeError, "hosted_fixture_only"):
                m.identity()


if __name__ == "__main__":
    unittest.main()
