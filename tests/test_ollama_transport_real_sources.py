"""Independent, read-only integration checks against exact Desktop Git objects.

These checks use only Python stdlib; the workflow fetches exact SHA sources.
No live Ollama/host, model generation, Docker, credentials, or network fallback.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error

from app import ollama_transport_repair_profile as policy

ROOT = Path(__file__).resolve().parents[1]
BEFORE = ROOT / "_desktop_before" / "scripts" / "ollama_cpu_pilot.py"
AFTER = ROOT / "_desktop_candidate" / "scripts" / "ollama_cpu_pilot.py"
BEFORE_BLOB = "0b4df798cdcd72e7f86e1cf884424edcd8afe8c9"
AFTER_BLOB = "227884c61847604c5f008c947347f0342d2c"


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def load_script(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError("script_import_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FailingOpener:
    def __init__(self, error: Exception):
        self.error = error

    def open(self, _request, timeout=10):
        raise self.error


class GoodOpener:
    def open(self, _request, timeout=10):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return b'{"test":"bounded-local"}'


class RealDesktopSourceContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Fail, never skip, when either exact checkout is missing.
        if not BEFORE.is_file() or not AFTER.is_file():
            raise AssertionError("exact_desktop_sources_missing")
        cls.before, cls.after = BEFORE.read_bytes(), AFTER.read_bytes()
        if git_blob_sha(cls.before) != BEFORE_BLOB or git_blob_sha(cls.after) != AFTER_BLOB:
            raise AssertionError("desktop_source_blob_sha_mismatch")
        cls.original_script = load_script(BEFORE, "ollama_pilot_original_contract")
        cls.candidate_script = load_script(AFTER, "ollama_pilot_candidate_contract")

    def test_exact_git_blobs_and_static_scope(self):
        result = policy.classify_candidate(
            repository=policy.REPOSITORY,
            base_sha=policy.BASE_SHA,
            path=policy.PATH,
            before=self.before,
            after=self.after,
        )
        self.assertEqual(result["state"], "static_profile_admitted_only")
        self.assertEqual(result["before_sha256"], hashlib.sha256(self.before).hexdigest())
        self.assertEqual(result["after_sha256"], hashlib.sha256(self.after).hexdigest())

    def test_original_reproduces_undifferentiated_http_failure(self):
        for status in (400, 503):
            with self.subTest(status=status):
                error = urllib.error.HTTPError("http://127.0.0.1:11434/api/generate",
                                               status, "PRIVATE_DETAIL", {}, None)
                with patch.object(self.original_script.urllib.request, "build_opener",
                                  return_value=FailingOpener(error)):
                    with self.assertRaises(self.original_script.PilotBlocked) as captured:
                        self.original_script.http_json("/api/generate", payload={"model": "qwen2.5-coder:7b"})
                self.assertEqual(str(captured.exception), "ollama_unreachable")

    def test_candidate_classifies_http_errors_without_leaking_details(self):
        for status in (400, 404, 500, 503):
            with self.subTest(status=status):
                error = urllib.error.HTTPError("http://127.0.0.1:11434/api/generate",
                                               status, "PRIVATE_DETAIL", {}, None)
                with patch.object(self.candidate_script.urllib.request, "build_opener",
                                  return_value=FailingOpener(error)):
                    with self.assertRaises(self.candidate_script.PilotBlocked) as captured:
                        self.candidate_script.http_json("/api/generate", payload={"model": "qwen2.5-coder:7b"})
                self.assertEqual(str(captured.exception), f"ollama_http_{status}")
                self.assertNotIn("PRIVATE_DETAIL", str(captured.exception))

    def test_candidate_classifies_other_transport_errors(self):
        cases = (
            (urllib.error.URLError(TimeoutError("PRIVATE_DETAIL")), "ollama_timeout"),
            (urllib.error.URLError(ConnectionRefusedError("PRIVATE_DETAIL")), "ollama_connection_error"),
            (TimeoutError("PRIVATE_DETAIL"), "ollama_timeout"),
            (ConnectionResetError("PRIVATE_DETAIL"), "ollama_connection_error"),
            (OSError("PRIVATE_DETAIL"), "ollama_os_error"),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                with patch.object(self.candidate_script.urllib.request, "build_opener",
                                  return_value=FailingOpener(error)):
                    with self.assertRaises(self.candidate_script.PilotBlocked) as captured:
                        self.candidate_script.http_json("/api/generate", payload={"model": "qwen2.5-coder:7b"})
                self.assertEqual(str(captured.exception), expected)
                self.assertNotIn("PRIVATE_DETAIL", str(captured.exception))

    def test_candidate_still_accepts_bounded_json(self):
        with patch.object(self.candidate_script.urllib.request, "build_opener",
                          return_value=GoodOpener()):
            data = self.candidate_script.http_json("/api/tags")
        self.assertEqual(data, {"test": "bounded-local"})

    def test_forbidden_endpoint_still_fails_before_network(self):
        with patch.object(self.candidate_script.urllib.request, "build_opener",
                          side_effect=AssertionError("network_must_not_be_called")):
            with self.assertRaises(self.candidate_script.PilotBlocked) as captured:
                self.candidate_script.http_json("/api/pull")
        self.assertEqual(str(captured.exception), "endpoint_not_allowed")

    def test_wrong_sha_cannot_reuse_previous_approval(self):
        with self.assertRaises(policy.ProfileBlocked) as captured:
            policy.classify_candidate(repository=policy.REPOSITORY,
                                      base_sha="f" * 40, path=policy.PATH,
                                      before=self.before, after=self.after)
        self.assertEqual(str(captured.exception), "identity_or_base_drift")

    def test_out_of_scope_code_change_rejected(self):
        changed = self.after.replace(b'"/api/generate"', b'"/api/pull"', 1)
        self.assertNotEqual(changed, self.after)
        with self.assertRaises(policy.ProfileBlocked) as captured:
            policy.classify_candidate(repository=policy.REPOSITORY,
                                      base_sha=policy.BASE_SHA, path=policy.PATH,
                                      before=self.before, after=changed)
        self.assertEqual(str(captured.exception), "candidate_scope_changed")


if __name__ == "__main__":
    unittest.main()
