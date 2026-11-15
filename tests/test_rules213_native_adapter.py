"""Rules #213 native wiring: UNIT tests only, never physical Builder/CAS proof."""
from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.native_one_shot import AdapterContext, TaskManifest
from app.rules213_native_adapter import (
    BASE_SHA, NATIVE_DESCRIPTOR, NATIVE_PROFILE, OFFICIAL_DIGEST,
    REPOSITORY, SOURCE_FUNCTIONS, Rules213NativeAdapter,
    _read_sources,
)
from app.rules213_updater_profile import Rules213Blocked

TASK_ID = "cwp-" + "a" * 24
PRODUCED = "b" * 40
ORIGINAL = {
    "scripts/install_command_gateway_host.py":
        b"data = True\n\ndef install_bundle():\n    return 1\n",
    "scripts/update_command_gateway_host.py":
        b"meta = 1\n\ndef preflight_runtime_replaceability():\n    return 1\n",
}
CHANGED = {
    path: data.replace(b"return 1", b"return 2")
    for path, data in ORIGINAL.items()
}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_manifest(values, *, validator=False):
    return TaskManifest.parse({
        "schema_version": 1, "task_id": TASK_ID,
        "repository": REPOSITORY, "base_sha": BASE_SHA,
        "workspace_sha": PRODUCED if validator else BASE_SHA,
        "adapter_id": NATIVE_PROFILE,
        "adapter_contract_sha256": NATIVE_DESCRIPTOR.contract_sha256,
        "before_sha256": {p: sha(values[p]) for p in sorted(values)},
    })


def write_sources(root, values):
    for path, value in values.items():
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(value)


def attestor():
    return {
        "task_id": TASK_ID, "base_sha": BASE_SHA,
        "model": "qwen2.5-coder:7b", "digest": OFFICIAL_DIGEST,
        "process_verified": True, "origin": "http://127.0.0.1:11434",
        "model_calls": 1,
        "after_sha256": {path: sha(CHANGED[path]) for path in sorted(CHANGED)},
    }


def adapter(*, baseline=None, model=None, report=None, main=None, issue=None):
    return Rules213NativeAdapter(
        task_id=TASK_ID, original=ORIGINAL,
        live_main_reader=main or (lambda: BASE_SHA),
        issue_reader=issue or (lambda: {"number": 213, "state": "open",
                                        "repository": REPOSITORY}),
        baseline_checker=baseline or (lambda root: {
            "result": "FAILED_REPRODUCED", "base_sha": BASE_SHA,
            "failed_count": 1, "case_id": "win-same-bytes-locked"}),
        model_receipt_reader=model or attestor,
        validator_evidence_reader=report or (lambda root, sha: {
            "result": "INDEPENDENT_TESTS_PASSED", "produced_sha": sha,
            "source_sha": BASE_SHA, "validator_role": "validator",
            "tests_passed": 42, "tests_failed": 0,
            "negative_control_passed": True, "idempotency_passed": True,
        }),
    )


class AdapterTests(unittest.TestCase):
    def test_descriptor_has_distinct_builder_stage_commit_and_validator(self):
        self.assertEqual(NATIVE_DESCRIPTOR.builder_commands,
                         ("rules213.commit", "rules213.repair", "rules213.stage"))
        self.assertEqual(NATIVE_DESCRIPTOR.validator_commands,
                         ("rules213.validate",))
        self.assertEqual(set(NATIVE_DESCRIPTOR.allowed_paths), set(SOURCE_FUNCTIONS))

    def test_build_with_mocked_gateway_results_never_proves_live_cas(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            write_sources(root, ORIGINAL)
            a = adapter()
            def repair():
                write_sources(root, CHANGED)
                return {"result": "GOVERNED_COMMAND_VERIFIED"}
            commands = {
                "rules213.repair": repair,
                "rules213.stage": lambda: {"result": "GOVERNED_COMMAND_VERIFIED"},
                "rules213.commit": lambda: {
                    "result": "GOVERNED_COMMAND_VERIFIED", "head": PRODUCED},
                "rules213.validate": lambda: {
                    "result": "GOVERNED_COMMAND_VERIFIED", "head": PRODUCED},
            }
            ctx = AdapterContext(root, make_manifest(ORIGINAL),
                                 NATIVE_DESCRIPTOR, commands, "builder")
            with patch("app.rules213_native_adapter.LocalWorkspace") as git:
                git.return_value.head.return_value = PRODUCED
                git.return_value.status.return_value = ""
                git.return_value.changed_paths.return_value = tuple(sorted(CHANGED))
                result = a.build(ctx)
            self.assertEqual(result.produced_sha, PRODUCED)
            self.assertEqual(result.changed_paths, tuple(sorted(CHANGED)))
            self.assertEqual(result.invoked_commands, (
                "rules213.repair", "rules213.stage", "rules213.commit"))
            self.assertEqual(len(result.evidence_sha256), 64)

    def test_main_drift_blocks_before_command(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            write_sources(root, ORIGINAL)
            a = adapter(main=lambda: "c" * 40)
            calls = []
            ctx = AdapterContext(root, make_manifest(ORIGINAL),
                                 NATIVE_DESCRIPTOR, {
                key: lambda: calls.append(key)
                for key in NATIVE_DESCRIPTOR.command_keys}, "builder")
            with self.assertRaisesRegex(Rules213Blocked,"main_advanced"):
                a.build(ctx)
            self.assertEqual(calls, [])

    def test_missing_failing_baseline_blocks_before_model(self):
        with TemporaryDirectory() as td:
            root=Path(td)
            write_sources(root, ORIGINAL)
            a=adapter(baseline=lambda _: {
                "result":"PASSED","base_sha":BASE_SHA,
                "failed_count":0,"case_id":"none"})
            ctx=AdapterContext(root,make_manifest(ORIGINAL),NATIVE_DESCRIPTOR,
                {key:lambda: {"result":"GOVERNED_COMMAND_VERIFIED"}
                 for key in NATIVE_DESCRIPTOR.command_keys}, "builder")
            with self.assertRaisesRegex(Rules213Blocked,"failing_baseline_missing"):
                a.build(ctx)

    def test_bad_model_receipt_blocks_before_stage_and_commit(self):
        with TemporaryDirectory() as td:
            root=Path(td)
            write_sources(root,ORIGINAL)
            a=adapter(model=lambda: {"model":"wrong"})
            calls=[]
            def repair():
                calls.append("repair")
                write_sources(root,CHANGED)
                return {"result":"GOVERNED_COMMAND_VERIFIED"}
            def stage():
                calls.append("stage")
            commands={"rules213.repair":repair,"rules213.stage":stage,
                      "rules213.commit":stage,"rules213.validate":stage}
            ctx=AdapterContext(root,make_manifest(ORIGINAL),NATIVE_DESCRIPTOR,
                               commands,"builder")
            with self.assertRaisesRegex(Rules213Blocked,"provenance_unattested"):
                a.build(ctx)
            self.assertEqual(calls,["repair"])

    def test_scope_violation_blocks_before_commit(self):
        with TemporaryDirectory() as td:
            root=Path(td)
            write_sources(root,ORIGINAL)
            def repair():
                write_sources(root,CHANGED)
                (root / sorted(CHANGED)[0]).write_bytes(
                    b"unexpected = 1\n" + CHANGED[sorted(CHANGED)[0]])
                return {"result":"GOVERNED_COMMAND_VERIFIED"}
            ctx=AdapterContext(root,make_manifest(ORIGINAL),NATIVE_DESCRIPTOR,
              {"rules213.repair":repair,"rules213.stage":lambda:None,
               "rules213.commit":lambda:None,"rules213.validate":lambda:None},
              "builder")
            with self.assertRaisesRegex(Rules213Blocked,"outside_function_changed"):
                adapter().build(ctx)

    def test_missing_issue_blocks_before_model(self):
        with TemporaryDirectory() as td:
            root=Path(td)
            write_sources(root,ORIGINAL)
            a=adapter(issue=lambda: {"number":213,"state":"closed",
                                      "repository":REPOSITORY})
            ctx=AdapterContext(root,make_manifest(ORIGINAL),NATIVE_DESCRIPTOR,
              {key:lambda:None for key in NATIVE_DESCRIPTOR.command_keys},"builder")
            with self.assertRaisesRegex(Rules213Blocked,"issue_unproved"):
                a.build(ctx)

    def test_validator_requires_independent_report_and_distinct_role(self):
        with TemporaryDirectory() as td:
            root=Path(td)
            write_sources(root,CHANGED)
            a=adapter(report=lambda root,sha: {
                "result":"INDEPENDENT_TESTS_PASSED","produced_sha":sha,
                "source_sha":BASE_SHA,"validator_role":"validator",
                "tests_passed":42,"tests_failed":1,
                "negative_control_passed":True,"idempotency_passed":True})
            commands={"rules213.validate":lambda: {
                      "result":"GOVERNED_COMMAND_VERIFIED","head":PRODUCED},
                      "rules213.repair":lambda:None,
                      "rules213.stage":lambda:None,
                      "rules213.commit":lambda:None}
            ctx=AdapterContext(root,make_manifest(CHANGED,validator=True),
                               NATIVE_DESCRIPTOR,commands,"validator")
            with patch("app.rules213_native_adapter.LocalWorkspace") as git:
                git.return_value.head.return_value=PRODUCED
                git.return_value.status.return_value=""
                git.return_value.changed_paths.return_value=tuple(sorted(CHANGED))
                with self.assertRaisesRegex(Rules213Blocked,"validator_unproved"):
                    a.validate(ctx,PRODUCED)

    def test_default_rejects_unknown_callable_or_paths(self):
        with self.assertRaisesRegex(Rules213Blocked,"original_invalid"):
            Rules213NativeAdapter(
                task_id=TASK_ID,original={"other.py":b"foo"},
                live_main_reader=lambda:BASE_SHA,
                issue_reader=lambda:{"number":213},
                baseline_checker=lambda _:None,
                model_receipt_reader=lambda:None,
                validator_evidence_reader=lambda root,sha:None)
        with self.assertRaisesRegex(Rules213Blocked,"attestors_required"):
            Rules213NativeAdapter(
                task_id=TASK_ID,original=ORIGINAL,live_main_reader=None,
                issue_reader=lambda:{"number":213},
                baseline_checker=lambda _:None,
                model_receipt_reader=lambda:None,
                validator_evidence_reader=lambda root,sha:None)

if __name__ == "__main__":
    unittest.main()
