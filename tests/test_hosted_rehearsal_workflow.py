"""Protect trigger coverage and canonical governance in the consolidated PR chain."""
from __future__ import annotations

import copy
import fnmatch
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/hosted-continuity-rehearsal.yml"
CALLER = ROOT / ".github/workflows/hosted-checkpoint-continuity.yml"
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
RULES_SHA = "a430688c499077f5472841560f224c40766b0b78"
TRUST = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "safe.directory",
    "GIT_CONFIG_VALUE_0": "${{ github.workspace }}",
}


class HostedWorkflowTests(unittest.TestCase):
    def setUp(self):
        # BaseLoader preserves GitHub's literal "on" key and string scalars.
        self.workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.caller = yaml.load(CALLER.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    def assert_coverage(self, workflow):
        trigger = workflow["on"]["pull_request"]
        self.assertEqual(set(trigger.get("types", ("opened", "synchronize", "reopened"))),
                         {"opened", "synchronize", "reopened"})
        patterns = trigger["paths"]
        self.assertIsInstance(patterns, list)
        self.assertFalse(any(p.startswith("!") for p in patterns))
        for path in REQUIRED_PATHS:
            self.assertTrue(any(fnmatch.fnmatchcase(path, p) for p in patterns),
                            f"continuity rehearsal trigger misses {path}")

    def test_store_dependencies_and_controls_trigger_rehearsal(self):
        # The caller owns event filters; the reusable scenario must not trigger twice.
        self.assert_coverage(self.caller)
        self.assertEqual(set(self.workflow["on"]), {"workflow_call"})
        self.assertEqual(self.caller["jobs"]["store_rehearsal"]["uses"],
                         "./.github/workflows/hosted-continuity-rehearsal.yml")

    def test_missing_core_or_requirements_trigger_is_detected(self):
        for path in ("app/store.py", "requirements.txt",
                     "tests/test_hosted_rehearsal_workflow.py"):
            broken = copy.deepcopy(self.caller)
            broken["on"]["pull_request"]["paths"] = [
                p for p in broken["on"]["pull_request"]["paths"]
                if not fnmatch.fnmatchcase(path, p)]
            with self.subTest(path=path), self.assertRaises(AssertionError):
                self.assert_coverage(broken)

    def test_workflow_has_no_added_privileges_or_failure_bypass(self):
        self.assertEqual(set(self.caller["on"]), {"push", "pull_request"})
        self.assertEqual(set(self.workflow["on"]), {"workflow_call"})
        for workflow, path in ((self.workflow, WORKFLOW), (self.caller, CALLER)):
            self.assertEqual(workflow["permissions"], {"contents": "read"})
            self.assertNotIn("secrets.", path.read_text(encoding="utf-8"))
            self.assertNotIn("continue-on-error", path.read_text(encoding="utf-8"))
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
            checkouts = [s for s in job["steps"]
                         if s.get("uses", "").startswith("actions/checkout@")]
            self.assertEqual(len(checkouts), 2)
            self.assertEqual(checkouts[0]["with"]["ref"],
                             "${{ github.event.pull_request.head.sha }}")
            self.assertTrue(all(s["with"]["persist-credentials"] == "false"
                                for s in checkouts))

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

    def assert_canonical_job(self, job):
        self.assertEqual(job["container"], {
            "image": "python:3.12.15-bookworm", "volumes": ["/workspace"]})
        for key, value in (
            ("PYTHONDONTWRITEBYTECODE", "1"),
            ("REQSYS_WORKSPACE_ROOT", "/workspace"),
            ("REQSYS_GATEWAY_STATE_DIR", "/workspace/.reqsys-command-gateway-posix"),
        ):
            self.assertEqual(job["env"][key], value)
        governed = [step for step in job["steps"] if "run" in step]
        self.assertEqual(len(governed), 1)
        self.assertEqual(governed[0]["env"], TRUST)
        rules = [step for step in job["steps"]
                 if step.get("with", {}).get("repository") ==
                 "ericson-j-santos/chatgpt-operational-rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["with"]["ref"], RULES_SHA)
        self.assertEqual(rules[0]["with"]["path"], ".rehearsal-rules")

    def test_canonical_policy_environment_and_step_trust_are_preserved(self):
        for job in self.workflow["jobs"].values():
            self.assert_canonical_job(job)

    def test_canonical_environment_regressions_are_detected(self):
        for phase in ("produce", "resume"):
            for fault in ("container", "root", "state", "bytecode", "trust-count",
                          "trust-key", "trust-wildcard", "trust-path",
                          "rules-sha", "rules-path"):
                job = copy.deepcopy(self.workflow["jobs"][phase])
                step = next(s for s in job["steps"] if "run" in s)
                rules = next(s for s in job["steps"]
                             if s.get("with", {}).get("repository") ==
                             "ericson-j-santos/chatgpt-operational-rules")
                if fault == "container":
                    job["container"]["volumes"] = ["/tmp"]
                elif fault == "root":
                    job["env"]["REQSYS_WORKSPACE_ROOT"] = "/tmp"
                elif fault == "state":
                    job["env"]["REQSYS_GATEWAY_STATE_DIR"] = "/tmp/state"
                elif fault == "bytecode":
                    job["env"]["PYTHONDONTWRITEBYTECODE"] = "0"
                elif fault == "trust-count":
                    step["env"]["GIT_CONFIG_COUNT"] = "2"
                elif fault == "trust-key":
                    step["env"]["GIT_CONFIG_KEY_0"] = "core.bare"
                elif fault == "trust-wildcard":
                    step["env"]["GIT_CONFIG_VALUE_0"] = "*"
                elif fault == "trust-path":
                    step["env"]["GIT_CONFIG_VALUE_0"] = "/tmp/other-source"
                elif fault == "rules-sha":
                    rules["with"]["ref"] = "main"
                else:
                    rules["with"]["path"] = "_unrelated_rules"
                with self.subTest(phase=phase, fault=fault), self.assertRaises(AssertionError):
                    self.assert_canonical_job(job)


if __name__ == "__main__":
    unittest.main()
