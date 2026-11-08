from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.pre_pr_gate_cli import main as cli_main

from app.pre_pr_gate import PrePRBlocked, evaluate_pre_pr, replay_pre_pr


BASE = "a" * 40
HEAD = "b" * 40
PRODUCED = "c" * 40
DIGEST = "d" * 64
ARTIFACT = "e" * 64
PATCH = "f" * 64


def records():
    request = {
        "schema_version": "pre-pr.request.v1",
        "repository": "ericson-j-santos/noteri-runtime",
        "issue_number": 42,
        "todo_id": "TODO-54",
        "base_sha": BASE,
        "head_sha": HEAD,
        "source_branch": "fix/noteri-probe-rules-sha-20261106",
        "correlation_id": "noteri-issue42-prepr-20261108",
    }
    builder = {
        "schema_version": "pre-pr.builder.v1",
        "repository": request["repository"],
        "issue_number": 42,
        "todo_id": "TODO-54",
        "base_sha": BASE,
        "source_sha": HEAD,
        "produced_sha": PRODUCED,
        "source_branch": request["source_branch"],
        "correlation_id": request["correlation_id"],
        "model_id": "qwen2.5-coder:7b",
        "model_digest": DIGEST,
        "model_calls": 1,
        "baseline_failed": True,
        "changed_paths": ["scripts/noteri_probe.py"],
        "before_sha256": "1" * 64,
        "after_sha256": "2" * 64,
        "patch_sha256": PATCH,
        "artifact_ref": "cas+git-bundle://sha256/" + ARTIFACT,
        "artifact_sha256": ARTIFACT,
        "lease_generation": 2,
        "attempt_id": "attempt-noteri-20261108",
        "builder_worker_id": "builder-noteri-20261108",
    }
    validator = {
        "schema_version": "pre-pr.validator.v1",
        "repository": request["repository"],
        "issue_number": 42,
        "todo_id": "TODO-54",
        "base_sha": BASE,
        "source_sha": HEAD,
        "produced_sha": PRODUCED,
        "correlation_id": request["correlation_id"],
        "artifact_ref": builder["artifact_ref"],
        "artifact_sha256": ARTIFACT,
        "cas_readback_sha256": ARTIFACT,
        "patch_sha256": PATCH,
        "before_sha256": builder["before_sha256"],
        "after_sha256": builder["after_sha256"],
        "model_id": builder["model_id"],
        "model_digest": builder["model_digest"],
        "model_calls": builder["model_calls"],
        "changed_paths": builder["changed_paths"],
        "independent_checkout": True,
        "validator_worker_id": "validator-noteri-20261108",
        "validator_model_calls": 0,
        "positive_passed": True,
        "negative_passed": True,
        "regression_passed": True,
        "replay_passed": True,
        "lease_generation": 2,
        "attempt_id": builder["attempt_id"],
    }
    return request, builder, validator


class PrePRGateTests(unittest.TestCase):
    def test_self_consistent_untrusted_json_never_grants_ready(self):
        request, builder, validator = records()
        # All hashes, SHA IDs, model calls and CAS flags below are synthetic.
        result = evaluate_pre_pr(request, builder, validator)
        self.assertEqual(result["status"], "SCHEMA_ONLY_UNVERIFIED")
        self.assertFalse(result["draft_pr_authorized"])
        self.assertFalse(result["independent_evidence_verified"])
        self.assertFalse(result["merge_authorized"])
        self.assertFalse(result["physical_execution_authorized"])
        self.assertEqual(result["source_sha"], HEAD)
        self.assertEqual(len(result["receipt_fingerprint_sha256"]), 64)

    def test_cli_self_consistent_forged_receipts_is_blocked(self):
        request, builder, validator = records()
        with TemporaryDirectory() as temp:
            paths = [Path(temp) / f"{name}.json"
                     for name in ("request", "builder", "validator")]
            for path, value in zip(paths, (request, builder, validator)):
                path.write_text(json.dumps(value), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli_main([
                    "--request", str(paths[0]),
                    "--builder", str(paths[1]),
                    "--validator", str(paths[2]),
                ])
        result = json.loads(out.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "SCHEMA_ONLY_UNVERIFIED")
        self.assertFalse(result["draft_pr_authorized"])

    def test_bare_noncanonical_cas_ref_is_blocked(self):
        request, builder, validator = records()
        builder["artifact_ref"] = "cas+git://sha256/" + ARTIFACT
        validator["artifact_ref"] = builder["artifact_ref"]
        with self.assertRaisesRegex(PrePRBlocked, "artifact_ref_invalid"):
            evaluate_pre_pr(request, builder, validator)

    def test_exact_replay_is_idempotent(self):
        request, builder, validator = records()
        decision = evaluate_pre_pr(request, builder, validator)
        replay = replay_pre_pr(decision, request, builder, validator)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["receipt_fingerprint_sha256"], decision["receipt_fingerprint_sha256"])

    def test_replay_with_drift_fails_closed(self):
        request, builder, validator = records()
        decision = evaluate_pre_pr(request, builder, validator)
        changed = deepcopy(validator)
        changed["positive_passed"] = False
        with self.assertRaises(PrePRBlocked):
            replay_pre_pr(decision, request, builder, changed)

    def test_builder_must_bind_exact_source_head(self):
        request, builder, validator = records()
        builder["source_sha"] = "9" * 40
        with self.assertRaisesRegex(PrePRBlocked, "builder_source_sha_mismatch"):
            evaluate_pre_pr(request, builder, validator)

    def test_real_model_and_failed_baseline_are_required(self):
        request, builder, validator = records()
        builder["model_calls"] = 0
        with self.assertRaisesRegex(PrePRBlocked, "model_calls_invalid"):
            evaluate_pre_pr(request, builder, validator)
        request, builder, validator = records()
        builder["baseline_failed"] = False
        with self.assertRaisesRegex(PrePRBlocked, "baseline_failure_unproved"):
            evaluate_pre_pr(request, builder, validator)

    def test_validator_must_bind_exact_model_evidence(self):
        request, builder, validator = records()
        validator["model_digest"] = "0" * 64
        with self.assertRaisesRegex(PrePRBlocked, "validator_builder_binding_invalid"):
            evaluate_pre_pr(request, builder, validator)

    def test_cas_bytes_must_be_independently_read_back(self):
        request, builder, validator = records()
        validator["cas_readback_sha256"] = "0" * 64
        with self.assertRaisesRegex(PrePRBlocked, "cas_readback_mismatch"):
            evaluate_pre_pr(request, builder, validator)

    def test_validator_must_be_distinct_and_model_free(self):
        request, builder, validator = records()
        validator["validator_worker_id"] = builder["builder_worker_id"]
        with self.assertRaisesRegex(PrePRBlocked, "validator_independence_unproved"):
            evaluate_pre_pr(request, builder, validator)
        request, builder, validator = records()
        validator["validator_model_calls"] = 1
        with self.assertRaisesRegex(PrePRBlocked, "validator_model_call_forbidden"):
            evaluate_pre_pr(request, builder, validator)

    def test_negative_regression_and_replay_are_mandatory(self):
        for field in ("negative_passed", "regression_passed", "replay_passed"):
            request, builder, validator = records()
            validator[field] = False
            with self.assertRaises(PrePRBlocked):
                evaluate_pre_pr(request, builder, validator)

    def test_unknown_fields_are_rejected(self):
        request, builder, validator = records()
        request["command"] = "trust-me"
        with self.assertRaisesRegex(PrePRBlocked, "request_fields_invalid"):
            evaluate_pre_pr(request, builder, validator)


if __name__ == "__main__":
    unittest.main()
