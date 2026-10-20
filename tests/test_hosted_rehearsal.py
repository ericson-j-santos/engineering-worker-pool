"""Offline controls. These tests never claim real host or provider readiness."""
from __future__ import annotations
import base64
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hosted_rehearsal", ROOT / "scripts/hosted_rehearsal.py")
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class CapsuleTests(unittest.TestCase):
    def setUp(self):
        self.context = dict(source_sha="a"*40, rules_sha=module.RULES_SHA,
                            run_id="12345", run_attempt="1")
        db = b"SQLite format 3\x00" + b"fixture-only"
        artifact = {"stage":"one","result":module.digest(module.PUBLIC_INPUT)}
        self.value = dict(self.context, schema=module.MAGIC, producer_host="producer-test",
                          producer_runner="GitHub Actions producer fixture",
                          task_id="cwp-"+"a"*24, old_generation=1, old_attempt="attempt-one",
                          artifact=artifact, artifact_sha256=module.digest(module.canonical(artifact)),
                          database=base64.b64encode(db).decode(), database_sha256=module.digest(db))

    def decode(self, value=None, context=None):
        raw, sha = module.encode_capsule(value or self.value)
        return module.decode_capsule(raw, sha, context or self.context)

    def test_roundtrip_is_deterministic(self):
        self.assertEqual(self.decode(), self.value)
        self.assertEqual(module.encode_capsule(self.value), module.encode_capsule(self.value))

    def test_corrupt_bytes_rejected(self):
        encoded, sha = module.encode_capsule(self.value)
        with self.assertRaises(module.RehearsalError):
            module.decode_capsule(encoded, "b"*64, self.context)

    def test_all_context_fields_bound(self):
        for key in self.context:
            wrong = dict(self.context, **{key:"2" if key != "source_sha" else "b"*40})
            with self.subTest(key=key), self.assertRaises(module.RehearsalError):
                self.decode(context=wrong)

    def test_unknown_missing_keys_and_schema(self):
        variants = [dict(self.value, arbitrary=True), dict(self.value, schema="wrong")]
        missing = dict(self.value)
        missing.pop("artifact")
        variants.append(missing)
        for value in variants:
            with self.assertRaises(module.RehearsalError):
                self.decode(value)

    def test_duplicate_json_rejected(self):
        with self.assertRaises(module.RehearsalError):
            module.strict_json(b'{"a":1,"a":2}\n')

    def test_noncanonical_json_rejected(self):
        with self.assertRaises(module.RehearsalError):
            module.strict_json(b'{ "a": 1 }\n')

    def test_decompression_bomb_rejected(self):
        encoded = base64.b64encode(zlib.compress(b"a"*(module.MAX_RAW+1))).decode()
        with self.assertRaises(module.RehearsalError):
            module.decode_capsule(encoded, "a"*64, self.context)

    def test_trailing_compressed_data_rejected(self):
        encoded, sha = module.encode_capsule(self.value)
        invalid = base64.b64encode(base64.b64decode(encoded)+b"trailing").decode()
        with self.assertRaises(module.RehearsalError):
            module.decode_capsule(invalid, sha, self.context)

    def test_generation_type_and_identity_are_strict(self):
        for generation in (True, "1", 0, 2, None):
            with self.assertRaises(module.RehearsalError):
                self.decode(dict(self.value, old_generation=generation))
        for key in ("task_id","old_attempt","producer_host","producer_runner"):
            with self.assertRaises(module.RehearsalError):
                self.decode(dict(self.value, **{key:"unsafe\nidentity"}))

    def test_artifact_divergence_rejected_even_with_outer_digest(self):
        for update in ({"artifact":{"stage":"one","result":"b"*64}},
                       {"artifact_sha256":"b"*64}):
            with self.assertRaises(module.RehearsalError):
                self.decode(dict(self.value, **update))

    def test_database_format_hash_and_encoding(self):
        for update in ({"database":"%%%"},
                       {"database":base64.b64encode(b"not-sqlite").decode()},
                       {"database_sha256":"b"*64}):
            with self.assertRaises(module.RehearsalError):
                self.decode(dict(self.value, **update))

    def test_encoded_size_and_digest_validation(self):
        for encoded, sha in (("", "a"*64), ("a"*(module.MAX_ENCODED+1),"a"*64),
                             ("invalid", "invalid")):
            with self.assertRaises(module.RehearsalError):
                module.decode_capsule(encoded, sha, self.context)

    def test_launch_receipt_must_bind_exact_sha_and_boolean(self):
        receipt = dict(result="SESSION_LAUNCH_OK", state_validated=True,
                       base_sync="isolated_session_source", source_repo_root=str(module.ROOT),
                       head=self.context["source_sha"], snapshot_sha256="a"*64,
                       session_id="rehearsal-test",
                       target_path=str(module.WORK_ROOT/"wt-chat-rehearsal-test"))
        self.assertEqual(module.validate_launch(receipt, self.context).name,
                         "wt-chat-rehearsal-test")
        for update in ({"state_validated":1},{"head":"b"*40},{"result":"ok"},
                       {"snapshot_sha256":""},{"session_id":"../escape"},
                       {"base_sync":"not_needed"},{"source_repo_root":"/tmp/another-source"},
                       {"target_path":"/tmp/wt-chat-escape"}):
            with self.assertRaises(module.RehearsalError):
                module.validate_launch(dict(receipt, **update), self.context)

    def test_physical_host_cannot_run_entrypoint(self):
        with patch.dict(module.os.environ, {}, clear=True):
            with self.assertRaises(module.RehearsalError):
                module.context()

    def test_fixtures_cannot_be_claimed_as_live_failover(self):
        text = (ROOT/"scripts/hosted_rehearsal.py").read_text()
        self.assertIn('live_failover_verified=False', text)
        self.assertIn('s3_backend_verified=False', text)
        self.assertNotIn("boto3", text)
        self.assertNotIn("requests.", text)
        self.assertIn('TEST_REPOSITORY = "example/hosted-continuity-rehearsal"', text)

    def test_same_runner_rejected_even_with_distinct_container(self):
        encoded, sha = module.encode_capsule(self.value)
        with patch.dict(module.os.environ, {
                "REHEARSAL_CAPSULE": encoded, "REHEARSAL_CAPSULE_SHA256": sha,
                "RUNNER_NAME": self.value["producer_runner"]}, clear=True), \
                patch.object(module.socket, "gethostname", return_value="another-container"), \
                patch.object(module, "work_path") as work:
            with self.assertRaisesRegex(module.RehearsalError, "distinct_executor_required"):
                module.resume(self.context)
            work.assert_not_called()


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name).resolve()
        (self.source / ".git").mkdir()
        self.rules = self.source / ".rehearsal-rules"
        (self.rules / ".git").mkdir(parents=True)
        (self.rules / ".git" / "HEAD").write_text(module.RULES_SHA + "\n")
        self.pins = {}
        for relative, payload in (
                (module.POLICY_RELATIVE, b'{"fixture":"policy"}\n'),
                ("scripts/session_launcher.py", b"# fixture launcher\n")):
            target = self.rules / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            self.pins[relative] = (len(payload), hashlib.sha256(payload).hexdigest())
        self.manifest = {"version":"1.6.12", "files":[
            {"path":relative, "size":size, "sha256":sha}
            for relative, (size, sha) in self.pins.items()]}
        (self.rules / "MANIFEST.json").write_text(json.dumps(self.manifest))
        self.sha = "a"*40
        self.event = self.source / "event.json"
        self.event.write_text(json.dumps({
            "repository":{"private":False},
            "pull_request":{"user":{"login":"ericson-j-santos"},
                "head":{"repo":{"full_name":module.REPOSITORY},
                        "sha":self.sha, "ref":"test-hosted-bootstrap"}}}))
        self.env = {
            "GITHUB_ACTIONS":"true", "GITHUB_REPOSITORY":module.REPOSITORY,
            "RUNNER_ENVIRONMENT":"github-hosted", "GITHUB_ACTOR":"ericson-j-santos",
            "GITHUB_EVENT_NAME":"pull_request", "GITHUB_JOB":"produce",
            "GITHUB_RUN_ID":"12345", "GITHUB_RUN_ATTEMPT":"1",
            "REHEARSAL_EXPECTED_SHA":self.sha, "GITHUB_WORKSPACE":str(self.source),
            "GITHUB_EVENT_PATH":str(self.event), "REQSYS_WORKSPACE_ROOT":"/workspace",
            "REQSYS_GATEWAY_STATE_DIR":"/workspace/.reqsys-command-gateway-posix",
            "GIT_CONFIG_COUNT":"1", "GIT_CONFIG_KEY_0":"safe.directory",
            "GIT_CONFIG_VALUE_0":str(self.source)}

    @contextmanager
    def configured(self, changes=None):
        env = dict(self.env, **(changes or {}))
        with patch.object(module, "ROOT", self.source), \
                patch.object(module, "PINNED_RULES_FILES", self.pins), \
                patch.object(module.sys, "dont_write_bytecode", True), \
                patch.dict(module.os.environ, env, clear=True):
            yield

    def test_policy_binding_is_canonical_and_not_parallel(self):
        self.assertEqual(module.WORK_ROOT, Path("/workspace/.chatgpt-workers"))
        self.assertEqual(module.POLICY_RELATIVE, "config/command-gateway.policy.posix.json")
        self.assertEqual(module.POLICY_SHA256,
                         "50e2da27bcca6c82ae57cabbb5d8f490108fb363265c5f30fd0dcf78d6828bc5")
        self.assertEqual(module.PINNED_RULES_FILES[module.POLICY_RELATIVE],
                         (2058, module.POLICY_SHA256))
        text = (ROOT/"scripts/hosted_rehearsal.py").read_text()
        self.assertNotIn('"config" / "hosted-rehearsal-gateway.json"', text)

    def test_canonical_bytes_are_verified_without_executing_commands(self):
        with self.configured(), patch.object(module.subprocess, "run") as command:
            self.assertEqual(module.canonical_governance(self.source),
                             (self.rules/"scripts", self.rules/module.POLICY_RELATIVE))
            command.assert_not_called()

    def test_changed_policy_or_module_bytes_are_rejected(self):
        with self.configured():
            for relative in self.pins:
                path = self.rules / relative
                original = path.read_bytes()
                try:
                    path.write_bytes(b"!" + original[1:])
                    with self.subTest(path=relative), self.assertRaisesRegex(
                            module.RehearsalError, "canonical_governance_bytes_mismatch"):
                        module.canonical_governance(self.source)
                finally:
                    path.write_bytes(original)

    def test_detached_rules_sha_and_manifest_pin_are_required(self):
        with self.configured():
            (self.rules/".git"/"HEAD").write_text("b"*40 + "\n")
            with self.assertRaisesRegex(module.RehearsalError, "canonical_rules_sha_mismatch"):
                module.canonical_governance(self.source)
            (self.rules/".git"/"HEAD").write_text(module.RULES_SHA + "\n")
            self.manifest["files"][0]["sha256"] = "b"*64
            (self.rules/"MANIFEST.json").write_text(json.dumps(self.manifest))
            with self.assertRaisesRegex(module.RehearsalError, "canonical_manifest_mismatch"):
                module.canonical_governance(self.source)

    def test_source_alias_and_noncanonical_environment_are_rejected(self):
        alias = self.source/"source-alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(module.RehearsalError, "physical_directory_required"):
            module.physical_directory(alias)
        for changes in ({"REQSYS_WORKSPACE_ROOT":"/tmp"},
                        {"REQSYS_GATEWAY_STATE_DIR":"/tmp/state"},
                        {"GITHUB_WORKSPACE":str(alias)},
                        {"GIT_CONFIG_COUNT":""}, {"GIT_CONFIG_COUNT":"0"},
                        {"GIT_CONFIG_COUNT":"2"}, {"GIT_CONFIG_KEY_0":"core.bare"},
                        {"GIT_CONFIG_VALUE_0":"*"},
                        {"GIT_CONFIG_VALUE_0":str(self.source/"different-source")}):
            with self.subTest(changes=changes), self.configured(changes), \
                    patch.object(module, "execute_json") as command:
                with self.assertRaises(module.RehearsalError):
                    module.run_governed("produce")
                command.assert_not_called()

    def test_launcher_is_first_command_and_failure_stops_gateway(self):
        with self.configured(), patch.object(module.subprocess, "run") as direct, \
                patch.object(module, "execute_json",
                    side_effect=module.RehearsalError("intentional_launcher_stop")) as command:
            with self.assertRaisesRegex(module.RehearsalError, "intentional_launcher_stop"):
                module.run_governed("produce")
            direct.assert_not_called()
            command.assert_called_once()
            argv = command.call_args.args[0]
            self.assertEqual(argv[:3],
                             [module.sys.executable, "-B", str(self.rules/"scripts/session_launcher.py")])
            self.assertEqual(argv[argv.index("--policy")+1], str(self.rules/module.POLICY_RELATIVE))
            self.assertEqual(argv[argv.index("--repo")+1], str(self.source))
            self.assertEqual(argv[argv.index("--expected-head")+1], self.sha)
            self.assertEqual(argv[argv.index("--sync-ref")+1], "origin/test-hosted-bootstrap")

    def test_real_unittest_summary_supplies_observed_count(self):
        for count in (1, 23, 102):
            payload = dict(result="ok", command_exit_code=0, stdout="",
                           stderr=f"Ran {count} tests in 0.123s\n\nOK\n")
            self.assertEqual(module.validated_test_count(payload), count)

    def test_zero_missing_skipped_or_failed_test_report_is_rejected(self):
        reports = ("", "OK\n", "Ran 0 tests in 0.000s\n\nOK\n",
                   "Ran 7 tests in 0.001s\n", "Ran 7 tests in 0.001s\n\nOK (skipped=1)\n",
                   "Ran 7 tests in 0.001s\n\nOK (expected failures=1)\n",
                   "Ran 7 tests in 0.001s\n\nFAILED (failures=1)\n",
                   "Ran 7 tests in 0.001s\nRan 7 tests in 0.001s\n\nOK\n")
        for report in reports:
            with self.subTest(report=report), self.assertRaises(module.RehearsalError):
                module.validated_test_count(
                    dict(result="ok", command_exit_code=0, stdout="", stderr=report))
        for update in ({"result":"blocked"}, {"command_exit_code":1}):
            payload = dict(result="ok", command_exit_code=0,
                           stdout="", stderr="Ran 7 tests in 0.001s\n\nOK\n")
            with self.assertRaises(module.RehearsalError):
                module.validated_test_count(dict(payload, **update))

    def test_launcher_failure_preserves_bounded_stderr_diagnostic(self):
        receipt = {"action":"session_launcher", "result":"SESSION_LAUNCH_BLOCKED",
                   "gateway_exit_code":28, "phase":"validate_session_source",
                   "error":"fixture source unavailable\nread-only diagnostic",
                   "snapshot":"fixture-not-for-logs"}
        result = module.subprocess.CompletedProcess(
            ["python", "launcher"], 28, stdout="not a receipt\n",
            stderr=json.dumps(receipt) + "\n")
        with patch.object(module.subprocess, "run", return_value=result), \
                patch("builtins.print") as emitted:
            with self.assertRaisesRegex(module.RehearsalError, "governed_command_failed"):
                module.execute_json(["python", "launcher"], timeout=1, cwd=ROOT)
        diagnostic = json.loads(emitted.call_args.args[0])
        self.assertEqual(diagnostic["gateway_result"], "SESSION_LAUNCH_BLOCKED")
        self.assertEqual(diagnostic["gateway_phase"], "validate_session_source")
        self.assertEqual(diagnostic["launcher_error"],
                         "fixture source unavailable read-only diagnostic")
        self.assertNotIn("fixture-not-for-logs", emitted.call_args.args[0])

if __name__ == "__main__":
    unittest.main()
