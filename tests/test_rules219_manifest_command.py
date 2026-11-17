"""Local command contracts: not a physical Builder/Gateway attestation."""
from __future__ import annotations

from dataclasses import replace
import unittest

from app.native_one_shot import AdapterDescriptor, NativeExecutorBlocked
from app.rules219_manifest_command import (
    ADAPTER_ID, COMMAND_KEY, REPOSITORY, RULES_HEAD,
    WRAPPER_PATH, WRAPPER_SHA256, make_stage_spec,
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


if __name__ == "__main__":
    unittest.main()
