"""Noteri #42 validator Git fixture: contracts, NOT Qwen/physical E2E."""
from __future__ import annotations
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import noteri42_independent_validate as verifier


class ValidatorGitTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.root = Path(self.t.name).resolve()
        (self.root / ".github" / "workflows").mkdir(parents=True)
        self.target = self.root / verifier.SOURCE
        self.target.write_text("if ($LASTEXITCODE -ne 0) { throw 'NOTERI_219_GATEWAY_BLOCKED' }\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
                 "commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").decode().strip()
        self.target.write_text(
            "if ($LASTEXITCODE -ne 0) { throw 'NOTERI_219_GATEWAY_BLOCKED' }\n"
            "throw 'NOTERI_219_GATEWAY_POLICY_EXIT_20'\n"
            "throw 'NOTERI_219_GATEWAY_FAILURE_UNCLASSIFIED'\n"
            "# state_changed secrets_read\n"
        )
        self.git("add", ".")
        self.git("-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
                 "commit", "-qm", "repair")
        self.head = self.git("rev-parse", "HEAD").decode().strip()

    def git(self, *args: str) -> bytes:
        return subprocess.run(["git", *args], cwd=self.root,
                              capture_output=True, check=True).stdout

    def verify(self):
        with patch.object(verifier, "BASE_SHA", self.base):
            return verifier.verify_git(self.root, base=self.base, produced=self.head)

    def test_positive_real_git_diff_and_replay_without_mutation(self):
        before = self.git("status", "--porcelain")
        a, b = self.verify(), self.verify()
        self.assertEqual(a, b)
        self.assertEqual(a["changed_paths"], [verifier.SOURCE])
        self.assertEqual(len(a["source_sha256"]), 64)
        self.assertFalse(a["ready_for_pr"])
        self.assertFalse(a["cas_verified"])
        self.assertFalse(a["model_provenance_verified"])
        self.assertEqual(self.git("status", "--porcelain"), before)

    def test_wrong_sha_blocked(self):
        with self.assertRaises(verifier.Blocked):
            verifier.verify_git(self.root, base="0" * 40, produced=self.head)
        with patch.object(verifier, "BASE_SHA", self.base):
            with self.assertRaises(verifier.Blocked):
                verifier.verify_git(self.root, base=self.base, produced="f" * 40)

    def test_wrong_parent_and_missing_fix_blocked(self):
        with patch.object(verifier, "BASE_SHA", self.head):
            with self.assertRaises(verifier.Blocked):
                verifier.verify_git(self.root, base=self.head, produced=self.head)
        self.target.write_text("if ($LASTEXITCODE -ne 0) { throw 'NOTERI_219_GATEWAY_BLOCKED' }\n")
        self.git("add", ".")
        self.git("-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
                 "commit", "-qm", "regression")
        h = self.git("rev-parse", "HEAD").decode().strip()
        with patch.object(verifier, "BASE_SHA", self.head):
            with self.assertRaises(verifier.Blocked):
                verifier.verify_git(self.root, base=self.head, produced=h)

    def test_unauthorized_path_and_dirty_tree_blocked(self):
        (self.root / "extra").write_text("not allowed\n")
        with self.assertRaisesRegex(verifier.Blocked, "CHECKOUT_DIRTY"):
            self.verify()
        self.git("add", ".")
        self.git("-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
                 "commit", "-qm", "unreviewed")
        h = self.git("rev-parse", "HEAD").decode().strip()
        with patch.object(verifier, "BASE_SHA", self.base):
            with self.assertRaisesRegex(verifier.Blocked, "SCOPE_CHANGED"):
                verifier.verify_git(self.root, base=self.base, produced=h)

    def test_rejects_unbounded_raw_gateway_log(self):
        self.target.write_text(
            self.target.read_text() + "Write-Output $raw\n"
        )
        self.git("add", ".")
        self.git("-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
                 "commit", "-qm", "untrusted log")
        h = self.git("rev-parse", "HEAD").decode().strip()
        with patch.object(verifier, "BASE_SHA", self.head):
            with self.assertRaises(verifier.Blocked):
                verifier.verify_git(self.root, base=self.head, produced=h)

    def test_test_runner_has_fixed_argv_no_arbitrary_shell(self):
        for path in verifier.TESTS:
            x = self.root / path
            x.parent.mkdir(exist_ok=True)
            x.write_text("# fixture test\n")
        completed = type("Result", (), {
            "returncode": 0, "stdout": b"15 passed in 1.00s\n", "stderr": b"",
        })()
        with patch.object(verifier.subprocess, "run", return_value=completed) as runner:
            label, passed = verifier.run_tests(self.root)
        self.assertEqual((label, passed), ("NOTERI42_AFFECTED_TESTS_PASSED", 15))
        invocation = runner.call_args
        args = invocation.args[0]
        self.assertEqual(args[:6], [verifier.sys.executable, "-B", "-m", "pytest",
                                    "-q", "-p"])
        self.assertFalse(invocation.kwargs.get("shell", False))

    def test_skips_and_failing_runner_blocked(self):
        for path in verifier.TESTS:
            x = self.root / path
            x.parent.mkdir(exist_ok=True)
            x.write_text("# fixture test\n")
        for stdout, status in ((b"5 passed, 1 skipped in 0.9s\n", 0),
                               (b"15 passed in 1.0s\n", 1)):
            completed = type("Result", (), {
                "returncode": status, "stdout": stdout, "stderr": b"",
            })()
            with patch.object(verifier.subprocess, "run", return_value=completed):
                with self.assertRaises(verifier.Blocked):
                    verifier.run_tests(self.root)


if __name__ == "__main__":
    unittest.main()
