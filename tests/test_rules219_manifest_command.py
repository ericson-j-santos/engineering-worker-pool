"""Local command contracts: not a physical Builder/Gateway attestation."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from app.native_one_shot import (
    AdapterContext, AdapterDescriptor, NativeExecutorBlocked, TaskManifest,
)
from app.rules219_manifest_command import (
    ADAPTER_ID, COMMAND_KEY, COMMIT_KEY, SOURCE_STAGE_KEY,
    REPOSITORY, RULES_HEAD, WRAPPER_PATH, WRAPPER_SHA256,
    make_stage_spec, stage_manifest_then_commit,
)


def descriptor():
    return AdapterDescriptor(
        adapter_id=ADAPTER_ID,
        allowed_paths=("MANIFEST.json", "scripts/builder_manifest_stage.py"),
        builder_commands=("rules219.builder.commit", COMMAND_KEY),
        validator_commands=("rules219.validate",),
    )


class ManifestSpecTests(unittest.TestCase):
    def make(self, **changes):
        args = dict(repository=REPOSITORY, rules_head=RULES_HEAD,
                    role="builder", descriptor=descriptor())
        args.update(changes)
        return make_stage_spec(**args)

    def test_fixed_command(self):
        spec = self.make()
        self.assertEqual(spec.argv, ("python", WRAPPER_PATH))
        self.assertEqual(spec.script_sha256, WRAPPER_SHA256)
        self.assertEqual(spec.key, COMMAND_KEY)
        self.assertEqual(spec.risk, 2)
        self.assertTrue(spec.allow_dirty)
        self.assertFalse(spec.allow_head_change)
        spec.validate(descriptor())

    def test_replay_produces_identical_command(self):
        self.assertEqual(self.make(), self.make())

    def test_wrong_repository_blocks(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "binding_not_admitted"):
            self.make(repository="ericson-j-santos/noteri-runtime")

    def test_wrong_rules_sha_blocks(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "binding_not_admitted"):
            self.make(rules_head="0" * 40)

    def test_wrong_role_blocks(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "binding_not_admitted"):
            self.make(role="validator")

    def test_wrong_adapter_id_blocks(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "binding_not_admitted"):
            self.make(descriptor=replace(descriptor(), adapter_id="ci.native.patch.v1"))

    def test_manifest_outside_scope_blocks(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "binding_not_admitted"):
            self.make(descriptor=replace(descriptor(), allowed_paths=("scripts/builder_manifest_stage.py",)))

    def test_missing_command_key_blocks(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "binding_not_admitted"):
            self.make(descriptor=replace(descriptor(), builder_commands=("rules219.builder.commit",)))

    def test_profile_without_validator_blocks(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "binding_not_admitted"):
            self.make(descriptor=replace(descriptor(), roles=("builder",), validator_commands=()))

    def test_extra_python_argument_blocks(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "native_script_contract"):
            replace(self.make(), argv=("python", WRAPPER_PATH, "--stage-for-builder")).validate(descriptor())

    def test_missing_script_digest_blocks(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "native_script_contract"):
            replace(self.make(), script_sha256=None).validate(descriptor())

    def test_inline_python_blocks(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "native_script_contract"):
            replace(self.make(), argv=("python", "-c", "pass")).validate(descriptor())

    def test_path_traversal_blocks(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "native_script_path"):
            replace(self.make(), argv=("python", "../bad.py")).validate(descriptor())

    def test_risk_three_blocks(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "native_command_risk"):
            replace(self.make(), risk=3).validate(descriptor())

    def test_head_mutation_blocks(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "native_script_contract"):
            replace(self.make(), allow_head_change=True).validate(descriptor())



class ManifestCommitOrderingTests(unittest.TestCase):
    """Real local Git index/commit in disposable CI; callbacks are NOT Gateway."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "scripts").mkdir()
        (self.root / "sample.txt").write_text("original\n", encoding="utf-8")
        (self.root / WRAPPER_PATH).write_text("# inert fixture\n", encoding="utf-8")
        (self.root / "MANIFEST.json").write_text('{"version":"fixture","files":[]}\n')
        self.git("init", "-q")
        self.git("add", "--all")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture")
        self.base = self.git("rev-parse", "HEAD").decode().strip()
        self.desc = AdapterDescriptor(
            adapter_id=ADAPTER_ID,
            allowed_paths=("MANIFEST.json", "sample.txt", WRAPPER_PATH),
            builder_commands=(COMMIT_KEY, COMMAND_KEY, SOURCE_STAGE_KEY),
            validator_commands=("rules219.validate",),
        )
        self.original_sha = {
            path: hashlib.sha256((self.root / path).read_bytes()).hexdigest()
            for path in self.desc.allowed_paths
        }
        self.manifest = TaskManifest.parse({
            "schema_version": 1,
            "task_id": "cwp-" + "1" * 24,
            "repository": REPOSITORY,
            "base_sha": self.base,
            "workspace_sha": self.base,
            "adapter_id": ADAPTER_ID,
            "adapter_contract_sha256": self.desc.contract_sha256,
            "before_sha256": self.original_sha,
        })
        self.events = []
        self.stage_override = None
        self.commit_override = None
        self.source_override = None
        self.ctx = AdapterContext(
            self.root, self.manifest, self.desc,
            {
                SOURCE_STAGE_KEY: self.source_stage,
                COMMAND_KEY: self.manifest_stage,
                COMMIT_KEY: self.commit,
                "rules219.validate": self.invalid_validator,
            }, "builder",
        )

    def git(self, *argv):
        return subprocess.run(
            ["git", *argv], cwd=self.root,
            capture_output=True, check=True,
        ).stdout

    def invalid_validator(self):
        self.fail("Validator may never be invoked by Builder")

    def source_stage(self):
        self.events.append("source")
        if self.source_override:
            return self.source_override()
        self.git("add", "--", "sample.txt")
        return {"result": "GOVERNED_COMMAND_VERIFIED", "head": self.base}

    def _valid_manifest_data(self):
        entries = []
        for path in ("sample.txt", WRAPPER_PATH):
            payload = self.git("show", ":" + path)
            entries.append({
                "path": path,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            })
        return {"version": "fixture", "files": entries}

    def manifest_stage(self):
        self.events.append("manifest")
        if self.stage_override:
            return self.stage_override()
        (self.root / "MANIFEST.json").write_text(
            json.dumps(self._valid_manifest_data(), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.git("add", "--", "MANIFEST.json")
        return {"result": "GOVERNED_COMMAND_VERIFIED", "head": self.base}

    def commit(self):
        self.events.append("commit")
        if self.commit_override:
            return self.commit_override()
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture source and manifest")
        return {
            "result": "GOVERNED_COMMAND_VERIFIED",
            "head": self.git("rev-parse", "HEAD").decode().strip(),
        }

    def prepare(self):
        (self.root / "sample.txt").write_text("changed\n", encoding="utf-8")
        self.ctx.invoke(SOURCE_STAGE_KEY)

    def test_real_git_index_manifest_and_source_in_same_commit(self):
        self.prepare()
        result = stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source", "manifest", "commit"])
        self.assertEqual(result.changed_paths, ("MANIFEST.json", "sample.txt"))
        self.assertEqual(result.produced_sha,
                         self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(self.git("status", "--porcelain"), b"")
        self.assertEqual(self.git("diff", "--name-only", self.base, "HEAD").decode().splitlines(),
                         ["MANIFEST.json", "sample.txt"])
        self.assertEqual(result.invoked_commands,
                         (SOURCE_STAGE_KEY, COMMAND_KEY, COMMIT_KEY))
        self.assertEqual(len(result.evidence_sha256), 64)

    def test_second_invocation_cannot_duplicate_commit(self):
        self.prepare()
        stage_manifest_then_commit(self.ctx)
        sha = self.git("rev-parse", "HEAD")
        with self.assertRaises(NativeExecutorBlocked):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.git("rev-parse", "HEAD"), sha)
        self.assertEqual(self.events.count("commit"), 1)

    def test_remote_configured_blocks_before_invocation(self):
        self.prepare()
        self.git("remote", "add", "origin", "https://example.invalid/forbidden.git")
        with self.assertRaisesRegex(NativeExecutorBlocked, "workspace_head_invalid"):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source"])

    def test_missing_source_stage_receipt_fails_before_manifest(self):
        (self.root / "sample.txt").write_text("changed\n")
        self.git("add", "sample.txt")
        with self.assertRaisesRegex(NativeExecutorBlocked, "source_stage_not_attested"):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, [])

    def test_unstaged_change_fails_before_manifest(self):
        self.prepare()
        (self.root / "sample.txt").write_text("unstaged\n")
        with self.assertRaisesRegex(NativeExecutorBlocked, "source_not_cleanly_staged"):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source"])

    def test_untracked_file_fails_before_manifest(self):
        self.prepare()
        (self.root / "unexpected.py").write_text("x = 1\n")
        with self.assertRaisesRegex(NativeExecutorBlocked, "source_not_cleanly_staged"):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source"])

    def test_other_staged_file_blocks_outside_allowlist(self):
        self.prepare()
        (self.root / "extra.txt").write_text("not allowed\n")
        self.git("add", "extra.txt")
        with self.assertRaisesRegex(NativeExecutorBlocked, "source_scope_invalid"):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source"])

    def test_prestaged_manifest_blocks(self):
        self.prepare()
        (self.root / "MANIFEST.json").write_text('{"other":true}\n')
        self.git("add", "MANIFEST.json")
        with self.assertRaisesRegex(NativeExecutorBlocked, "source_scope_invalid"):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source"])

    def test_missing_manifest_in_index_prevents_commit(self):
        self.prepare()
        self.stage_override = lambda: {
            "result": "GOVERNED_COMMAND_VERIFIED", "head": self.base,
        }
        with self.assertRaisesRegex(NativeExecutorBlocked, "stage_scope_drift"):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source", "manifest"])

    def test_bad_gateway_receipt_prevents_commit(self):
        self.prepare()
        self.stage_override = lambda: {"result": "NOT_GOVERNED", "head": self.base}
        with self.assertRaisesRegex(NativeExecutorBlocked, "manifest_command_unverified"):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source", "manifest"])

    def test_manifest_mutated_after_git_add_prevents_commit(self):
        self.prepare()
        def bad():
            self.manifest_stage_real()
            (self.root / "MANIFEST.json").write_text('{"tampered":true}\n')
            return {"result": "GOVERNED_COMMAND_VERIFIED", "head": self.base}
        self.stage_override = bad
        with self.assertRaises(NativeExecutorBlocked):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source", "manifest"])

    def manifest_stage_real(self):
        (self.root / "MANIFEST.json").write_text(
            json.dumps(self._valid_manifest_data(), sort_keys=True) + "\n",
        )
        self.git("add", "MANIFEST.json")

    def test_manifest_stage_replaces_source_index_blocks(self):
        self.prepare()
        def changed():
            (self.root / "sample.txt").write_text("malicious changed\n")
            self.git("add", "sample.txt")
            self.manifest_stage_real()
            return {"result": "GOVERNED_COMMAND_VERIFIED", "head": self.base}
        self.stage_override = changed
        with self.assertRaisesRegex(NativeExecutorBlocked, "stage_scope_drift"):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source", "manifest"])

    def test_manifest_json_missing_source_hash_blocks(self):
        self.prepare()
        def missing():
            (self.root / "MANIFEST.json").write_text(
                '{"version":"tampered","files":[]}\n'
            )
            self.git("add", "MANIFEST.json")
            return {"result": "GOVERNED_COMMAND_VERIFIED", "head": self.base}
        self.stage_override = missing
        with self.assertRaisesRegex(NativeExecutorBlocked, "content_drift"):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source", "manifest"])

    def test_manifest_stages_extra_outside_scope_blocks(self):
        self.prepare()
        def sneaky():
            self.manifest_stage_real()
            (self.root / "other.txt").write_text("extra\n")
            self.git("add", "other.txt")
            return {"result": "GOVERNED_COMMAND_VERIFIED", "head": self.base}
        self.stage_override = sneaky
        with self.assertRaisesRegex(NativeExecutorBlocked, "stage_scope_drift"):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source", "manifest"])

    def test_forged_commit_head_does_not_count_as_success(self):
        self.prepare()
        def forged():
            self.git("-c", "user.name=Test", "-c",
                     "user.email=test@example.invalid",
                     "commit", "-qm", "fixture source and manifest")
            return {"result": "GOVERNED_COMMAND_VERIFIED", "head": "0" * 40}
        self.commit_override = forged
        with self.assertRaisesRegex(NativeExecutorBlocked, "commit_unverified"):
            stage_manifest_then_commit(self.ctx)

    def test_commit_missing_cannot_report_success(self):
        self.prepare()
        self.commit_override = lambda: {
            "result": "GOVERNED_COMMAND_VERIFIED", "head": "f" * 40,
        }
        with self.assertRaisesRegex(NativeExecutorBlocked, "commit_unverified"):
            stage_manifest_then_commit(self.ctx)

    def test_validator_context_is_never_admitted(self):
        self.prepare()
        validator = AdapterContext(
            self.root, self.manifest, self.desc,
            {
                SOURCE_STAGE_KEY: self.source_stage,
                COMMAND_KEY: self.manifest_stage,
                COMMIT_KEY: self.commit,
                "rules219.validate": self.invalid_validator,
            }, "validator",
        )
        validator.invoked.append(SOURCE_STAGE_KEY)
        with self.assertRaisesRegex(NativeExecutorBlocked, "stage_identity_invalid"):
            stage_manifest_then_commit(validator)
        self.assertEqual(self.events, ["source"])

    def test_source_stage_forged_without_index_blocks(self):
        (self.root / "sample.txt").write_text("changed\n")
        self.source_override = lambda: {
            "result": "GOVERNED_COMMAND_VERIFIED", "head": self.base,
        }
        self.ctx.invoke(SOURCE_STAGE_KEY)
        with self.assertRaisesRegex(NativeExecutorBlocked, "source_scope_invalid"):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source"])

    def test_source_stage_replay_tampering_blocks(self):
        self.prepare()
        self.ctx.invoke(SOURCE_STAGE_KEY)
        with self.assertRaisesRegex(NativeExecutorBlocked, "source_stage_not_attested"):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source", "source"])

    def test_wrong_profile_sha_rejected(self):
        self.prepare()
        self.ctx.manifest = replace(self.manifest, adapter_contract_sha256="0" * 64)
        with self.assertRaisesRegex(NativeExecutorBlocked, "stage_identity_invalid"):
            stage_manifest_then_commit(self.ctx)
        self.assertEqual(self.events, ["source"])



if __name__ == "__main__":
    unittest.main()
