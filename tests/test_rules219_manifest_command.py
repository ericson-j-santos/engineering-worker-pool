"""Local command contracts: not a physical Builder/Gateway attestation."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import os
import shutil
import sys
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from app.native_one_shot import (
    AdapterContext, AdapterDescriptor, NativeExecutorBlocked, TaskManifest,
    LocalWorkspace, ReceiptStore,
)
from app.rules219_manifest_command import (
    ADAPTER_ID, COMMAND_KEY, COMMIT_KEY, SOURCE_STAGE_KEY,
    REPOSITORY, RULES_HEAD, WRAPPER_PATH, WRAPPER_SHA256,
    SOURCE_PATH, MODEL_NAME, MODEL_PROPOSE_KEY, MODEL_APPLY_KEY,
    PROPOSER_SHA256, APPLIER_SHA256, VALIDATOR_SHA256,
    VALIDATOR_KEY, RULES219_NATIVE_DESCRIPTOR, 
    make_stage_spec, stage_manifest_then_commit,
    fixed_rules219_native_specs, Rules219NativeBuilderAdapter,
    bind_rules219_native_executor,
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
        with self.assertRaisesRegex(NativeExecutorBlocked, "source_not_cleanly_staged"):
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




class Rules219NativeCatalogTests(unittest.TestCase):
    def test_exact_catalog_contains_only_fixed_reviewed_commands(self):
        specs = fixed_rules219_native_specs(
            proposer_sha256=PROPOSER_SHA256, applier_sha256=APPLIER_SHA256,
            validator_sha256=VALIDATOR_SHA256,
        )
        self.assertEqual(set(specs), set(RULES219_NATIVE_DESCRIPTOR.command_keys))
        for spec in specs.values():
            spec.validate(RULES219_NATIVE_DESCRIPTOR)
        self.assertEqual(specs[MODEL_PROPOSE_KEY].argv,
                         ("python", "scripts/rules219_model_propose.py"))
        self.assertEqual(specs[MODEL_APPLY_KEY].argv,
                         ("python", "scripts/rules219_model_apply.py"))
        self.assertEqual(specs[VALIDATOR_KEY].role, "validator")
        self.assertEqual(specs[COMMIT_KEY].argv[:3], ("git", "commit", "-m"))
        self.assertEqual(specs[SOURCE_STAGE_KEY].argv,
                         ("git", "add", "--", SOURCE_PATH))
        self.assertEqual(specs[COMMAND_KEY].script_sha256, WRAPPER_SHA256)

    def test_model_digest_must_be_from_trusted_installation(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "installed_digests_required"):
            fixed_rules219_native_specs(proposer_sha256="1" * 64,
                                        applier_sha256="invalid",
                                        validator_sha256="3" * 64)

    def test_well_formed_but_unreviewed_installed_script_hash_is_rejected(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "installed_digests_unreviewed"):
            fixed_rules219_native_specs(
                proposer_sha256="f" * 64,
                applier_sha256=APPLIER_SHA256,
                validator_sha256=VALIDATOR_SHA256,
            )

    def test_fixed_scripts_bytes_equal_pinned_sha_in_rules_checkout(self):
        path = os.environ.get("RULES219_FIXTURE_ROOT")
        if not path:
            self.skipTest("SHA-bound Rules fixture required")
        for filename, digest in (
            ("scripts/rules219_model_propose.py", PROPOSER_SHA256),
            ("scripts/rules219_model_apply.py", APPLIER_SHA256),
            ("scripts/rules219_validator.py", VALIDATOR_SHA256),
        ):
            self.assertEqual(
                hashlib.sha256((Path(path) / filename).read_bytes()).hexdigest(),
                digest,
            )

    def test_native_adapter_requires_valid_model_digest(self):
        with self.assertRaisesRegex(NativeExecutorBlocked, "model_digest_not_pinned"):
            Rules219NativeBuilderAdapter(
                expected_model_digest="not-attested",
                model_receipt_reader=lambda: {}, validator_receipt_reader=lambda: {},
            )

    def test_native_executor_rejects_fake_registry_before_any_host_call(self):
        adapter = Rules219NativeBuilderAdapter(
            expected_model_digest="a" * 64,
            model_receipt_reader=lambda: {}, validator_receipt_reader=lambda: {},
        )
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(
                NativeExecutorBlocked, "executor_dependencies_unattested"
            ):
                bind_rules219_native_executor(
                    registry=object(), adapter=adapter, api=object(),
                    workspace=LocalWorkspace(Path(d)), receipts=ReceiptStore(Path(d) / "receipts"),
                    artifact_transport=None, role="builder",
                )


@unittest.skipUnless(
    os.environ.get("RULES219_FIXTURE_ROOT"),
    "Pinned Rules #219 checkout is required for isolated real-Git integration",
)
class Rules219NativeBuilderIntegrationTests(unittest.TestCase):
    """Real Rules branch files + Git + manifesto; fake model/Gateway receipts.

    MUST NOT be counted as a real Qwen model or physical Gateway.
    """
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        trusted = Path(os.environ["RULES219_FIXTURE_ROOT"]).resolve()
        self.root = Path(self.temp.name) / "rules"
        shutil.copytree(trusted, self.root, symlinks=True)
        self.root = self.root.resolve()
        self.git("remote", "remove", "origin")
        self.base = self.git("rev-parse", "HEAD").decode().strip()
        self.assertEqual(self.base, RULES_HEAD)
        self.before = (self.root / SOURCE_PATH).read_bytes()
        self.after = self.before + b"\n# Synthetic integration-only regression fixture\n"
        self.expected_digest = "a" * 64
        self.task = "cwp-" + "a" * 24
        self.desc = RULES219_NATIVE_DESCRIPTOR
        self.events = []
        self.reader_override = {}
        self.invalid_apply = None
        self.invalid_propose = None
        self.repo_manifest = TaskManifest.parse({
            "schema_version": 1, "task_id": self.task,
            "repository": REPOSITORY, "base_sha": RULES_HEAD,
            "workspace_sha": RULES_HEAD, "adapter_id": self.desc.adapter_id,
            "adapter_contract_sha256": self.desc.contract_sha256,
            "before_sha256": {
                relative: hashlib.sha256((self.root / relative).read_bytes()).hexdigest()
                for relative in self.desc.allowed_paths
            },
        })
        self.adapter = Rules219NativeBuilderAdapter(
            expected_model_digest=self.expected_digest,
            model_receipt_reader=self.model_receipt,
            validator_receipt_reader=self.validator_receipt,
        )
        self.context = AdapterContext(
            self.root, self.repo_manifest, self.desc,
            self.callbacks(self.root), "builder",
        )

    def git(self, *args, root=None):
        return subprocess.run(
            ["git", *args], cwd=root or self.root,
            check=True, capture_output=True,
        ).stdout

    def callbacks(self, worktree):
        source = worktree / SOURCE_PATH
        def proposed():
            self.events.append("propose")
            if self.invalid_propose:
                return self.invalid_propose()
            return {"result": "GOVERNED_COMMAND_VERIFIED", "head": RULES_HEAD}
        def applied():
            self.events.append("apply")
            if self.invalid_apply:
                return self.invalid_apply()
            source.write_bytes(self.after)
            return {"result": "GOVERNED_COMMAND_VERIFIED", "head": RULES_HEAD}
        def staged():
            self.events.append("source-stage")
            self.git("add", "--", SOURCE_PATH, root=worktree)
            return {"result": "GOVERNED_COMMAND_VERIFIED", "head": RULES_HEAD}
        def manifest():
            self.events.append("manifest")
            proc = subprocess.run(
                [sys.executable, "-B", "scripts/builder_manifest_stage.py"],
                cwd=worktree, capture_output=True, text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            if proc.returncode:
                raise AssertionError("Fixture manifest generator failed: " + proc.stderr)
            return {"result": "GOVERNED_COMMAND_VERIFIED", "head": RULES_HEAD}
        def committed():
            self.events.append("commit")
            self.git("-c", "user.name=IsolatedTest",
                     "-c", "user.email=unit@example.invalid",
                     "commit", "-qm", "test(rules219): model-generated regression",
                     root=worktree)
            return {"result": "GOVERNED_COMMAND_VERIFIED",
                    "head": self.git("rev-parse", "HEAD", root=worktree).decode().strip()}
        def validated():
            self.events.append("validate")
            return {"result": "GOVERNED_COMMAND_VERIFIED",
                    "head": self.git("rev-parse", "HEAD", root=worktree).decode().strip()}
        return {
            MODEL_PROPOSE_KEY: proposed,
            MODEL_APPLY_KEY: applied,
            SOURCE_STAGE_KEY: staged,
            COMMAND_KEY: manifest,
            COMMIT_KEY: committed,
            VALIDATOR_KEY: validated,
        }

    def model_receipt(self):
        return {
            "task_id": self.task, "source_sha": RULES_HEAD,
            "source_path": SOURCE_PATH, "model": MODEL_NAME,
            "digest": self.expected_digest, "process_verified": True,
            "origin": "http://127.0.0.1:11434", "model_calls": 1,
            "before_sha256": hashlib.sha256(self.before).hexdigest(),
            "after_sha256": hashlib.sha256(self.after).hexdigest(),
            **self.reader_override,
        }

    def validator_receipt(self):
        return {
            "result": "INDEPENDENT_TESTS_PASSED",
            "produced_sha": self.produced,
            "source_sha": RULES_HEAD, "validator_role": "validator",
            "tests_passed": 1, "tests_failed": 0,
            "negative_control_passed": True,
            "idempotency_passed": True,
            **self.reader_override,
        }

    def test_native_builder_calls_real_manifest_wrapper_and_commits(self):
        result = self.adapter.build(self.context)
        self.produced = result.produced_sha
        self.assertEqual(
            self.events, ["propose", "apply", "source-stage", "manifest", "commit"]
        )
        self.assertEqual(result.changed_paths, ("MANIFEST.json", SOURCE_PATH))
        self.assertNotEqual(result.produced_sha, RULES_HEAD)
        self.assertEqual(self.git("status", "--porcelain"), b"")
        self.assertEqual(self.git("show", "HEAD:" + SOURCE_PATH), self.after)
        self.assertEqual(
            self.git("show", "HEAD:MANIFEST.json"),
            (self.root / "MANIFEST.json").read_bytes(),
        )
        self.assertEqual(len(result.evidence_sha256), 64)
        # Independent *checkout*, but validator receipt is synthetic here.
        validator_root = Path(self.temp.name) / "validator"
        shutil.copytree(self.root, validator_root, symlinks=True)
        validator_root = validator_root.resolve()
        validator_manifest = TaskManifest.parse({
            **self.repo_manifest.__dict__,
            "workspace_sha": self.produced,
        })
        validator_context = AdapterContext(
            validator_root, validator_manifest, self.desc,
            self.callbacks(validator_root), "validator",
        )
        verified = self.adapter.validate(validator_context, self.produced)
        self.assertEqual(verified.produced_sha, self.produced)
        self.assertEqual(verified.changed_paths, ("MANIFEST.json", SOURCE_PATH))
        self.assertEqual(verified.invoked_commands, (VALIDATOR_KEY,))

    def test_bad_model_receipt_blocks_before_apply(self):
        self.reader_override = {"model_calls": 0}
        with self.assertRaisesRegex(NativeExecutorBlocked, "provenance_unverified"):
            self.adapter.build(self.context)
        self.assertEqual(self.events, ["propose"])
        self.assertEqual(self.git("status", "--porcelain"), b"")

    def test_bad_digest_blocks_before_apply(self):
        self.reader_override = {"digest": "b" * 64}
        with self.assertRaisesRegex(NativeExecutorBlocked, "provenance_unverified"):
            self.adapter.build(self.context)
        self.assertEqual(self.events, ["propose"])

    def test_proposal_must_not_modify_source(self):
        def changed():
            (self.root / SOURCE_PATH).write_bytes(self.after)
            return {"result": "GOVERNED_COMMAND_VERIFIED", "head": RULES_HEAD}
        self.invalid_propose = changed
        with self.assertRaisesRegex(NativeExecutorBlocked, "proposal_unverified"):
            self.adapter.build(self.context)
        self.assertEqual(self.events, ["propose"])

    def test_apply_cannot_edit_second_file(self):
        def malicious():
            (self.root / SOURCE_PATH).write_bytes(self.after)
            (self.root / "README.md").write_text("changed", encoding="utf-8")
            return {"result": "GOVERNED_COMMAND_VERIFIED", "head": RULES_HEAD}
        self.invalid_apply = malicious
        with self.assertRaisesRegex(NativeExecutorBlocked, "patch_unverified"):
            self.adapter.build(self.context)
        self.assertEqual(self.events, ["propose", "apply"])

    def test_second_builder_call_never_creates_duplicate_commit(self):
        result = self.adapter.build(self.context)
        before = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(NativeExecutorBlocked, "native_builder_base_drift"):
            self.adapter.build(self.context)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.events.count("commit"), 1)

    def test_validator_fake_receipt_not_accepted(self):
        self.produced = self.adapter.build(self.context).produced_sha
        self.reader_override = {"negative_control_passed": False}
        vctx = AdapterContext(
            self.root, TaskManifest.parse({
                **self.repo_manifest.__dict__, "workspace_sha": self.produced
            }), self.desc, self.callbacks(self.root), "validator",
        )
        with self.assertRaisesRegex(NativeExecutorBlocked, "independent_validator_unverified"):
            self.adapter.validate(vctx, self.produced)



if __name__ == "__main__":
    unittest.main()
