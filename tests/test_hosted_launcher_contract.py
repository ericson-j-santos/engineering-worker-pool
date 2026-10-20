"""Transient checkouts require explicit remote synchronization, not relaxed gates."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "hosted_launcher_contract",
    Path(__file__).resolve().parents[1] / "scripts/hosted_checkpoint_continuity.py",
)
assert SPEC and SPEC.loader
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

PR_ENV = {
    "GITHUB_ACTIONS": "true",
    "GITHUB_REPOSITORY": "ericson-j-santos/engineering-worker-pool",
    "GITHUB_EVENT_NAME": "pull_request",
    "GITHUB_REF": "refs/pull/37/merge",
    "GITHUB_HEAD_REF": "feat/remote-result-readback-20261020",
}


class LauncherControls(unittest.TestCase):
    def test_launcher_pins_remote_branch_and_exact_source(self):
        with patch.dict(os.environ, PR_ENV, clear=True):
            argv = m.launcher_arguments(Path("/repo"), Path("/policy.json"),
                                       Path("/rules/scripts"), "fixture-session",
                                       "fixture-correlation", "a" * 40)
        self.assertEqual(argv[argv.index("--expected-head") + 1], "a" * 40)
        self.assertEqual(argv[argv.index("--sync-ref") + 1],
                         "origin/feat/remote-result-readback-20261020")
        self.assertIn("-B", argv)
        self.assertEqual(argv.count("--sync-ref"), 1)
        self.assertEqual(argv[argv.index("--repo") + 1], str(Path("/repo")))

    def test_launcher_rejects_unpinned_source(self):
        for sha in ("main", "", "a" * 39, None):
            with self.subTest(sha=sha), self.assertRaises(RuntimeError):
                m.launcher_arguments(Path("/repo"), Path("/policy"),
                                     Path("/rules/scripts"), "s", "c", sha)

    def test_launcher_diagnostic_does_not_export_raw_output(self):
        stderr = '{"phase":"validate_arguments","error":"sensitive marker"}'
        result = m.launcher_failure(26, stderr)
        self.assertEqual(result["phase"], "validate_arguments")
        self.assertEqual(result["exit_code"], 26)
        self.assertNotIn("sensitive marker", json.dumps(result))
        self.assertEqual(m.launcher_failure(26, "raw sensitive output")["phase"], "unavailable")

    def test_new_and_legacy_pr_branches_are_not_hardcoded(self):
        for branch in ("feat/remote-result-readback-20261020",
                       "feat/hosted-cold-checkpoint-resume-20261020",
                       "fix/next-safe-increment", "main"):
            with self.subTest(branch=branch), patch.dict(
                os.environ, {**PR_ENV, "GITHUB_HEAD_REF": branch}, clear=True,
            ):
                first = m.launcher_sync_ref()
                self.assertEqual(first, "origin/" + branch)
                self.assertEqual(m.launcher_sync_ref(), first)

    def test_missing_head_ref_never_falls_back_to_historical_branch(self):
        values = {key: value for key, value in PR_ENV.items() if key != "GITHUB_HEAD_REF"}
        with patch.dict(os.environ, values, clear=True):
            with self.assertRaisesRegex(RuntimeError, "^launcher_branch_invalid$"):
                m.launcher_sync_ref()

    def test_untrusted_events_are_rejected_before_launcher(self):
        for key, value in (("GITHUB_ACTIONS", ""), ("GITHUB_REPOSITORY", "outside/repo"),
                           ("GITHUB_EVENT_NAME", "pull_request_target"),
                           ("GITHUB_EVENT_NAME", "workflow_dispatch"),
                           ("GITHUB_EVENT_NAME", "issue_comment")):
            with self.subTest(key=key, value=value), patch.dict(
                os.environ, {**PR_ENV, key: value}, clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "^launcher_event_untrusted$"):
                    m.launcher_sync_ref()

    def test_pr_event_requires_canonical_pull_merge_ref(self):
        for ref in ("refs/heads/main", "refs/pull/0/merge", "refs/pull/37/head",
                    "refs/pull/37/merge\n", "", "refs/pull/37/merge/extra"):
            with self.subTest(ref=ref), patch.dict(
                os.environ, {**PR_ENV, "GITHUB_REF": ref}, clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "^launcher_event_ref_invalid$"):
                    m.launcher_sync_ref()

    def test_branch_path_and_ref_injection_are_rejected(self):
        for branch in ("", "-main", "a//b", "a/../b", "a..b", "a.lock",
                       "a.lock/b", "a.", "a/.b", "/a", "a/", "a:b", "a@{b",
                       "a b", "a\nb", "a\\b", "a*","a?","a[", "a" * 241):
            with self.subTest(branch=branch), patch.dict(
                os.environ, {**PR_ENV, "GITHUB_HEAD_REF": branch}, clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "^launcher_branch_invalid$"):
                    m.launcher_sync_ref()

    def test_existing_push_lane_remains_exact(self):
        values = {**PR_ENV, "GITHUB_EVENT_NAME": "push", "GITHUB_HEAD_REF": "",
                  "GITHUB_REF": "refs/heads/feat/hosted-cold-checkpoint-resume-20261020"}
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(m.launcher_sync_ref(),
                             "origin/feat/hosted-cold-checkpoint-resume-20261020")
        for ref in ("refs/heads/main", "refs/tags/v1", "refs/heads/other"):
            with self.subTest(ref=ref), patch.dict(
                os.environ, {**values, "GITHUB_REF": ref}, clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "^launcher_event_ref_invalid$"):
                    m.launcher_sync_ref()
