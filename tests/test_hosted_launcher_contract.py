"""Transient checkouts require explicit remote synchronization, not relaxed gates."""
import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "hosted_launcher_contract",
    Path(__file__).resolve().parents[1] / "scripts/hosted_checkpoint_continuity.py",
)
assert SPEC and SPEC.loader
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class LauncherControls(unittest.TestCase):
    def test_launcher_pins_remote_branch_and_exact_source(self):
        argv = m.launcher_arguments(Path("/repo"), Path("/policy.json"),
                                   Path("/rules/scripts"), "fixture-session",
                                   "fixture-correlation", "a" * 40)
        self.assertEqual(argv[argv.index("--expected-head") + 1], "a" * 40)
        self.assertEqual(argv[argv.index("--sync-ref") + 1], m.SYNC_REF)
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
