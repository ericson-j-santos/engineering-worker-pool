"""Real disposable Git tests for the inert pre-PR candidate inspector."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from app.prepr_candidate_verifier import (
    CandidateBlocked, CandidateSpec, inspect_candidate,
)


class CandidateInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "app").mkdir()
        (self.root / "app" / "demo.py").write_text("VALUE = 1\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
                 "commit", "-qm", "baseline")
        self.base = self.git("rev-parse", "HEAD").decode().strip()
        (self.root / "app" / "demo.py").write_text("VALUE = 2\n")
        self.git("add", ".")
        self.git("-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
                 "commit", "-qm", "candidate")
        self.head = self.git("rev-parse", "HEAD").decode().strip()
        self.spec = CandidateSpec(
            repository="ericson-j-santos/desktop-pc24x7-runtime",
            base_sha=self.base, head_sha=self.head,
            observed_main_sha=self.base, observed_branch_sha=self.head,
            allowed_paths=("app/demo.py",), expected_paths=("app/demo.py",),
        )

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root,
                              capture_output=True, check=True).stdout

    def verify(self, **overrides):
        return inspect_candidate(self.root, replace(self.spec, **overrides))

    def test_readonly_git_patch_hash_paths_and_deterministic_replay(self):
        start = self.git("status", "--porcelain")
        first, again = self.verify(), self.verify()
        self.assertEqual(first, again)
        self.assertEqual(first["result"], "CANDIDATE_INSPECTED")
        self.assertEqual(first["changed_paths"], ["app/demo.py"])
        self.assertEqual(len(first["patch_sha256"]), 64)
        self.assertNotEqual(first["before_sha256"], first["after_sha256"])
        self.assertFalse(first["ready_for_pr"])
        self.assertFalse(first["model_calls_verified"])
        self.assertFalse(first["validator_independent_verified"])
        self.assertIsNone(first["cas_artifact_ref"])
        self.assertTrue(first["read_only"])
        self.assertEqual(self.git("status", "--porcelain"), start)

    def test_authoritative_main_or_branch_drift_is_denied(self):
        for key in ("observed_main_sha", "observed_branch_sha",
                    "base_sha", "head_sha"):
            with self.subTest(key=key), self.assertRaisesRegex(
                CandidateBlocked, "candidate_authoritative_head_mismatch"
            ):
                self.verify(**{key: "f" * 40})

    def test_invalid_sha_and_fake_repository_are_denied(self):
        for change in ({"observed_main_sha": "main"},
                       {"repository": "other"},
                       {"head_sha": "4" * 39}):
            with self.subTest(change=change), self.assertRaises(CandidateBlocked):
                self.verify(**change)

    def test_wrong_checkout_head_and_dirty_worktree_fail_closed(self):
        with self.assertRaisesRegex(CandidateBlocked,
                                    "candidate_checkout_head_mismatch"):
            inspect_candidate(self.root, replace(
                self.spec, base_sha=self.head, head_sha=self.base,
                observed_main_sha=self.head, observed_branch_sha=self.base,
            ))
        (self.root / "app" / "demo.py").write_text("TAMPERED = True\n")
        with self.assertRaisesRegex(CandidateBlocked, "candidate_checkout_dirty"):
            self.verify()

    def test_empty_diff_and_unexpected_changed_path_are_denied(self):
        with self.assertRaisesRegex(CandidateBlocked,
                                    "candidate_authoritative_head_mismatch"):
            self.verify(base_sha=self.head, observed_main_sha=self.head)
        with self.assertRaisesRegex(CandidateBlocked,
                                    "candidate_changed_scope_mismatch"):
            self.verify(expected_paths=("app/other.py",),
                        allowed_paths=("app/other.py",))

    def test_duplicate_or_traversal_paths_are_denied(self):
        for paths in (("app/demo.py", "app/demo.py"), ("../bad.py",),
                      ("app/./demo.py",), ("-cmd",)):
            with self.subTest(paths=paths), self.assertRaises(CandidateBlocked):
                self.verify(allowed_paths=paths, expected_paths=paths)

    def test_extra_committed_path_is_denied(self):
        (self.root / "app" / "extra.py").write_text("VALUE = 3\n")
        self.git("add", ".")
        self.git("-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
                 "commit", "-qm", "extra")
        new = self.git("rev-parse", "HEAD").decode().strip()
        with self.assertRaisesRegex(CandidateBlocked,
                                    "candidate_changed_scope_mismatch"):
            self.verify(head_sha=new, observed_branch_sha=new)

    def test_symlink_in_committed_candidate_is_denied(self):
        target = self.root / "app" / "demo.py"
        target.unlink()
        try:
            target.symlink_to("another.py")
        except OSError:
            self.skipTest("symlink unavailable")
        self.git("add", "-A")
        self.git("-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
                 "commit", "-qm", "symlink")
        new = self.git("rev-parse", "HEAD").decode().strip()
        with self.assertRaisesRegex(CandidateBlocked,
                                    "candidate_file_type_invalid"):
            self.verify(head_sha=new, observed_branch_sha=new)

    def test_added_regular_source_is_read_back_with_explicit_absence(self):
        (self.root / "app" / "extra.py").write_text("NEW = True\\n")
        self.git("add", ".")
        self.git("-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
                 "commit", "-qm", "added ordinary file")
        current = self.git("rev-parse", "HEAD").decode().strip()
        paths = ("app/demo.py", "app/extra.py")
        record = self.verify(head_sha=current, observed_branch_sha=current,
                             allowed_paths=paths, expected_paths=paths)
        replay = self.verify(head_sha=current, observed_branch_sha=current,
                             allowed_paths=paths, expected_paths=paths)
        self.assertEqual(record, replay)
        self.assertIsNone(record["before_sha256"]["app/extra.py"])
        self.assertEqual(len(record["after_sha256"]["app/extra.py"]), 64)
        self.assertFalse(record["ready_for_pr"])

    def test_deleted_source_is_still_denied(self):
        (self.root / "app" / "demo.py").unlink()
        self.git("add", "-A")
        self.git("-c", "user.name=CI", "-c", "user.email=ci@example.invalid",
                 "commit", "-qm", "delete source")
        now = self.git("rev-parse", "HEAD").decode().strip()
        with self.assertRaisesRegex(CandidateBlocked, "candidate_file_type_invalid"):
            self.verify(head_sha=now, observed_branch_sha=now)

    def test_untracked_file_fails_before_any_claim(self):
        (self.root / "NEW_UNTRACKED").write_text("not allowed\n")
        with self.assertRaisesRegex(CandidateBlocked, "candidate_checkout_dirty"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
