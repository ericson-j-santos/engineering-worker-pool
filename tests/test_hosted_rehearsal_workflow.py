"""Regression controls for the real hosted-continuity workflow trigger contract."""
from __future__ import annotations

import copy
import fnmatch
from pathlib import Path
import unittest

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/hosted-continuity-rehearsal.yml"
REQUIRED_PATHS = (
    "app/store.py",
    "app/__init__.py",
    "app/future_dependency.py",
    "requirements.txt",
    "requirements-s3-artifact.txt",
    "scripts/hosted_rehearsal.py",
    "tests/test_hosted_rehearsal.py",
    "tests/test_hosted_rehearsal_workflow.py",
    "config/hosted-rehearsal-gateway.json",
    ".github/workflows/hosted-continuity-rehearsal.yml",
    "docs/HOSTED_CONTINUITY_REHEARSAL.md",
)


class HostedWorkflowTests(unittest.TestCase):
    def setUp(self):
        # BaseLoader preserves the literal GitHub key "on", unlike YAML 1.1 booleans.
        self.workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def assert_coverage(self, workflow):
        trigger = workflow["on"]["pull_request"]
        self.assertEqual(set(trigger["types"]), {"opened", "synchronize", "reopened"})
        patterns = trigger["paths"]
        self.assertIsInstance(patterns, list)
        self.assertFalse(any(p.startswith("!") for p in patterns))
        for path in REQUIRED_PATHS:
            self.assertTrue(any(fnmatch.fnmatchcase(path, p) for p in patterns),
                            f"continuity rehearsal trigger misses {path}")

    def test_store_dependencies_and_controls_trigger_rehearsal(self):
        self.assert_coverage(self.workflow)

    def test_missing_core_or_requirements_trigger_is_detected(self):
        for path in ("app/store.py", "requirements.txt",
                     "tests/test_hosted_rehearsal_workflow.py"):
            broken = copy.deepcopy(self.workflow)
            broken["on"]["pull_request"]["paths"] = [
                p for p in broken["on"]["pull_request"]["paths"]
                if not fnmatch.fnmatchcase(path, p)]
            with self.subTest(path=path), self.assertRaises(AssertionError):
                self.assert_coverage(broken)

    def test_workflow_has_no_added_privileges_or_failure_bypass(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(set(self.workflow["on"]), {"pull_request"})
        self.assertNotIn("secrets.", WORKFLOW.read_text(encoding="utf-8"))
        self.assertNotIn("continue-on-error", WORKFLOW.read_text(encoding="utf-8"))
        self.assertEqual(self.workflow["concurrency"]["cancel-in-progress"], "false")

    def test_same_sha_real_two_job_dependency_is_preserved(self):
        jobs = self.workflow["jobs"]
        self.assertEqual(set(jobs), {"produce", "resume"})
        self.assertEqual(jobs["resume"]["needs"], "produce")
        for job in jobs.values():
            self.assertEqual(job["runs-on"], "ubuntu-24.04")
            self.assertLessEqual(int(job["timeout-minutes"]), 6)
            self.assertEqual(job["env"]["REHEARSAL_EXPECTED_SHA"],
                             "${{ github.event.pull_request.head.sha }}")
            checkouts = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
            self.assertEqual(len(checkouts), 2)
            self.assertEqual(checkouts[0]["with"]["ref"], "${{ github.event.pull_request.head.sha }}")
            self.assertTrue(all(s["with"]["persist-credentials"] == "false" for s in checkouts))

    def test_owner_only_admission_is_preserved(self):
        admission = self.workflow["jobs"]["produce"]["if"]
        for predicate in (
            "github.repository == 'ericson-j-santos/engineering-worker-pool'",
            "github.event.pull_request.head.repo.full_name == github.repository",
            "github.event.pull_request.user.login == 'ericson-j-santos'",
            "github.actor == 'ericson-j-santos'",
        ):
            self.assertIn(predicate, admission)
        self.assertNotIn("||", admission)

    def test_capsule_transport_and_governed_entrypoints_are_preserved(self):
        jobs = self.workflow["jobs"]
        self.assertEqual(jobs["resume"]["env"]["REHEARSAL_CAPSULE"],
                         "${{ needs.produce.outputs.capsule }}")
        self.assertEqual(jobs["resume"]["env"]["REHEARSAL_CAPSULE_SHA256"],
                         "${{ needs.produce.outputs.capsule_sha256 }}")
        for phase, job in jobs.items():
            commands = [s["run"] for s in job["steps"] if "run" in s]
            self.assertEqual(commands, [f"python -B scripts/hosted_rehearsal.py run-{phase}"])


if __name__ == "__main__":
    unittest.main()
