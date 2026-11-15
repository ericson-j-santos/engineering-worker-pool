"""Rules #213 contract: allowlisted updater only, no Builder execution.

Self-contained unittest suite: Windows native Python and pytest-compatible.
These contract tests do not attest model calls, CAS, lease or physical Builder.
"""
from __future__ import annotations

import hashlib
import unittest

from app.native_one_shot import TaskManifest
from app.rules213_updater_profile import (
    BASE_SHA, DESCRIPTOR, ISSUE, PROFILE, REPOSITORY, SOURCE_FUNCTIONS,
    Rules213Blocked, bind_base_manifest, check_generated_scope,
    verify_model_identity,
)

PATHS = sorted(SOURCE_FUNCTIONS)
BEFORE = {
    PATHS[0]: b"header = 1\n\ndef install_bundle():\n    return 1\n\nfooter = 2\n",
    PATHS[1]: b"header = 1\n\ndef preflight_runtime_replaceability():\n    return 1\n",
}
UPDATED = {
    PATHS[0]: b"header = 1\n\ndef install_bundle():\n    return 2\n\nfooter = 2\n",
    PATHS[1]: b"header = 1\n\ndef preflight_runtime_replaceability():\n    return 2\n",
}


def manifest(**changes):
    raw = {
        "schema_version": 1, "task_id": "cwp-" + "a" * 24,
        "repository": REPOSITORY, "base_sha": BASE_SHA,
        "workspace_sha": BASE_SHA, "adapter_id": PROFILE,
        "adapter_contract_sha256": DESCRIPTOR.contract_sha256,
        "before_sha256": {
            path: hashlib.sha256(content).hexdigest()
            for path, content in BEFORE.items()
        },
    }
    raw.update(changes)
    return TaskManifest.parse(raw)


class Rules213ContractTests(unittest.TestCase):
    def test_exact_source_does_not_grant_builder(self):
        receipt = bind_base_manifest(
            manifest(), issue_number=ISSUE, live_main_sha=BASE_SHA, original=BEFORE
        )
        self.assertEqual(receipt.repository, REPOSITORY)
        self.assertFalse(receipt.builder_executed)
        self.assertFalse(receipt.ready_for_pr)
        self.assertEqual(DESCRIPTOR.allowed_paths, tuple(PATHS))

    def test_all_identity_mismatches_block(self):
        for case in ("issue", "main", "repo", "profile", "hash", "path"):
            with self.subTest(case=case):
                args = {
                    "issue_number": ISSUE, "live_main_sha": BASE_SHA,
                    "original": BEFORE,
                }
                value = manifest()
                if case == "issue":
                    args["issue_number"] = ISSUE + 1
                elif case == "main":
                    args["live_main_sha"] = "f" * 40
                elif case == "repo":
                    value = manifest(repository="someone/other")
                elif case == "profile":
                    value = manifest(adapter_id="bridge.property.v1")
                elif case == "hash":
                    args["original"] = {**BEFORE, PATHS[0]: b"tampered"}
                elif case == "path":
                    args["original"] = {**BEFORE, "app/portfolio_bridge.py": b"malicious"}
                with self.assertRaises(Rules213Blocked):
                    bind_base_manifest(value, **args)

    def test_only_fixed_functions_can_change(self):
        report = check_generated_scope(BEFORE, UPDATED)
        self.assertEqual(report["changed_paths"], PATHS)
        self.assertEqual(report["model_calls"], 0)
        self.assertFalse(report["ready_for_pr"])

    def test_module_level_mutations_block(self):
        candidate = {
            **UPDATED,
            PATHS[0]: UPDATED[PATHS[0]].replace(b"header = 1", b"header = 3"),
        }
        with self.assertRaisesRegex(Rules213Blocked, "outside_function_changed"):
            check_generated_scope(BEFORE, candidate)

    def test_extra_path_and_empty_patch_block(self):
        with self.assertRaisesRegex(Rules213Blocked, "paths_invalid"):
            check_generated_scope(
                BEFORE, {**UPDATED, "app/portfolio_bridge.py": b"malicious"}
            )
        with self.assertRaisesRegex(Rules213Blocked, "patch_empty"):
            check_generated_scope(BEFORE, BEFORE)

    def test_model_shape_is_not_owner_or_live_attestation(self):
        with self.assertRaises(Rules213Blocked):
            verify_model_identity("other", "a" * 64, 1)
        with self.assertRaises(Rules213Blocked):
            verify_model_identity("qwen2.5-coder:7b", "dae161e27b0e" + "0" * 52, 0)
        verify_model_identity("qwen2.5-coder:7b", "dae161e27b0e" + "0" * 52, 1)
        self.assertFalse(check_generated_scope(BEFORE, UPDATED)["builder_executed"])

    def test_malformed_candidate_syntax_blocks(self):
        wrong = {**UPDATED, PATHS[0]: b"def install_bundle(:\n    pass\n"}
        with self.assertRaisesRegex(Rules213Blocked, "python_invalid"):
            check_generated_scope(BEFORE, wrong)

    def test_replay_same_scope_never_becomes_ready(self):
        first = check_generated_scope(BEFORE, UPDATED)
        second = check_generated_scope(BEFORE, UPDATED)
        self.assertEqual(first, second)
        self.assertFalse(second["validator_approved"])
        self.assertFalse(second["ready_for_pr"])


if __name__ == "__main__":
    unittest.main()
