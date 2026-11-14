"""Deterministic adapter tests; fake Ollama/Git is never physical proof."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import urllib.error

from app import ollama_transport_native_adapter as adapter
from app import ollama_transport_repair_profile as profile
from app.native_one_shot import AdapterContext, TaskManifest

PIN = adapter.MODEL_PIN
DIGEST = PIN + "a" * (64 - len(PIN))
TASK = "cwp-" + "a" * 24
BASE = '''import json
import urllib.error
import urllib.request

class PilotBlocked(RuntimeError):
    pass

def http_json(path: str, *, payload=None, timeout=10):
    if path not in {"/api/tags", "/api/generate", "/api/ps"}:
        raise PilotBlocked("endpoint_not_allowed")
    req = urllib.request.Request("http://127.0.0.1:11434" + path)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(131073)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        raise PilotBlocked("ollama_unreachable") from None
    return json.loads(raw)
'''
REPLACEMENT = '''    except urllib.error.HTTPError as exc:
        if type(exc.code) is int and 400 <= exc.code <= 599:
            raise PilotBlocked(f"ollama_http_{exc.code}") from None
        raise PilotBlocked("ollama_http_error") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise PilotBlocked("ollama_timeout") from None
        raise PilotBlocked("ollama_connection_error") from None
    except TimeoutError:
        raise PilotBlocked("ollama_timeout") from None
    except ConnectionError:
        raise PilotBlocked("ollama_connection_error") from None
    except OSError:
        raise PilotBlocked("ollama_os_error") from None'''
CANDIDATE = BASE.replace(adapter.OLD_HANDLER, REPLACEMENT)


def manifest(*, repo=profile.REPOSITORY, base=profile.BASE_SHA, sha=None,
             path=profile.PATH, checksum=None):
    return TaskManifest.parse({
        "schema_version": 1, "task_id": TASK,
        "repository": repo, "base_sha": base, "workspace_sha": sha or base,
        "adapter_id": profile.PROFILE,
        "adapter_contract_sha256": adapter.DESCRIPTOR.contract_sha256,
        "before_sha256": {path: checksum or profile.digest(BASE.encode())},
    })


class LocalModelContract(unittest.TestCase):
    def setUp(self):
        self.provider = adapter.LocalQwenHttpProposer()
        self.inv = {"models": [{"name": adapter.MODEL, "digest": DIGEST,
                                "size": 4683087561, "details": {"format": "gguf"}}]}
        self.gen = {"model": adapter.MODEL, "done": True,
                    "eval_count": 12, "eval_duration": 400_000_000,
                    "response": json.dumps({"replacement": REPLACEMENT})}

    def fake(self, path, **kwargs):
        return self.inv if path == "/api/tags" else self.gen

    def test_no_cloud_opt_in_is_rejected_before_any_network(self):
        with patch.object(self.provider, "_request", side_effect=AssertionError("no_network")):
            with patch.dict("os.environ", {"NATIVE_OLLAMA_DEV": "0", "OLLAMA_NO_CLOUD": "0"}):
                with self.assertRaisesRegex(adapter.TransportBlocked, "local_only_not_opted_in"):
                    self.provider.propose(BASE.encode())

    def test_model_name_and_digest_are_pinned(self):
        with patch.dict("os.environ", {"NATIVE_OLLAMA_DEV": "1", "OLLAMA_NO_CLOUD": "1"}):
            for key, val in [("digest", "f" * 64), ("name", "external-cloud:latest"),
                             ("remote_host", "example.invalid"), ("size", 0)]:
                with self.subTest(key=key):
                    changed = json.loads(json.dumps(self.inv))
                    changed["models"][0][key] = val
                    with patch.object(self.provider, "_request", return_value=changed) as mocked:
                        with self.assertRaises(adapter.TransportBlocked):
                            self.provider.propose(BASE.encode())
                        self.assertEqual(mocked.call_count, 1)  # never /api/generate

    def test_real_local_request_contract_can_return_model_generated_patch(self):
        with patch.dict("os.environ", {"NATIVE_OLLAMA_DEV": "1", "OLLAMA_NO_CLOUD": "1"}):
            with patch.object(self.provider, "_request", side_effect=self.fake) as mocked:
                result = self.provider.propose(BASE.encode())
        self.assertEqual(result["model"], adapter.MODEL)
        self.assertEqual(result["model_digest"], DIGEST)
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["replacement"], REPLACEMENT)
        self.assertEqual(mocked.call_count, 2)
        args, kwargs = mocked.call_args
        self.assertEqual(args, ("/api/generate",))
        self.assertEqual(kwargs["timeout"], 120)
        raw = json.loads(kwargs["data"])
        self.assertEqual(raw["model"], adapter.MODEL)
        self.assertFalse(raw["stream"])
        self.assertEqual(raw["options"]["num_predict"], 512)

    def test_unproved_inference_rejected(self):
        with patch.dict("os.environ", {"NATIVE_OLLAMA_DEV": "1", "OLLAMA_NO_CLOUD": "1"}):
            for bad in (dict(done=False), dict(eval_count=0), dict(response="not json"),
                        dict(response=json.dumps({"replacement": "bad"})),
                        dict(model="wrong-model")):
                with self.subTest(bad=bad):
                    def observed(path, **_):
                        return self.inv if path == "/api/tags" else {**self.gen, **bad}
                    with patch.object(self.provider, "_request", side_effect=observed):
                        with self.assertRaises(adapter.TransportBlocked):
                            self.provider.propose(BASE.encode())

    def test_fails_closed_on_remote_error_without_leaking_message(self):
        with patch.object(adapter.urllib.request, "build_opener") as opened:
            opened.return_value.open.side_effect = urllib.error.URLError("private-sensitive-token")
            with self.assertRaises(adapter.TransportBlocked) as cm:
                self.provider._request("/api/tags")
        self.assertEqual(str(cm.exception), "local_model_transport_failed")
        self.assertNotIn("private-sensitive-token", str(cm.exception))

    def test_disallowed_endpoint_fails_before_network(self):
        with patch.object(adapter.urllib.request, "build_opener", side_effect=AssertionError):
            with self.assertRaisesRegex(adapter.TransportBlocked, "endpoint_denied"):
                self.provider._request("/api/pull")


class AdapterContract(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "scripts").mkdir()
        (self.root / profile.PATH).write_bytes(BASE.encode())
        self.provider = adapter.LocalQwenHttpProposer()
        self.run_regressions = lambda root: {"exit_code": 0, "category": "passed",
            "output_within_limit": True, "passed_count": 24, "failed_count": 0,
            "error_count": 0, "skipped_count": 0,
            "source_sha256": profile.digest((root / profile.PATH).read_bytes())}
        self.instance = adapter.OllamaTransportNativeAdapter(
            original=BASE.encode(), task_id=TASK, proposer=self.provider,
            regression_runner=self.run_regressions)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root)

    def ctx(self, *, manifest_arg=None, role="builder"):
        return AdapterContext(self.root, manifest_arg or manifest(),
                              adapter.DESCRIPTOR, self.instance.commands(self.root), role)

    def test_descriptor_matches_existing_native_executor_contract(self):
        self.assertEqual(adapter.DESCRIPTOR.adapter_id, profile.PROFILE)
        self.assertEqual(adapter.DESCRIPTOR.allowed_paths, (profile.PATH,))
        self.assertEqual(adapter.DESCRIPTOR.commands_for("builder"),
                         ("ollama.transport.repair",))
        self.assertEqual(adapter.DESCRIPTOR.commands_for("validator"),
                         ("ollama.transport.validate",))
        self.assertEqual(len(adapter.DESCRIPTOR.contract_sha256), 64)

    def test_untrusted_builder_constructor_is_blocked(self):
        with self.assertRaisesRegex(adapter.TransportBlocked, "adapter_construction_denied"):
            adapter.OllamaTransportNativeAdapter(
                original=BASE.encode(), task_id=TASK,
                proposer=lambda _: REPLACEMENT, regression_runner=self.run_regressions)

    def test_wrong_manifest_stops_before_model(self):
        for modification in ({"repo":"attacker/other"}, {"base":"f" * 40},
                             {"path":"src/other.py"}):
            with self.subTest(modification=modification):
                with patch.object(self.provider, "propose", side_effect=AssertionError("model_called")) as model:
                    with self.assertRaises(adapter.TransportBlocked):
                        self.instance.build(self.ctx(manifest_arg=manifest(**modification)))
                    model.assert_not_called()

    def test_non_worker_branch_cannot_call_model(self):
        with patch.object(self.provider, "propose", side_effect=AssertionError("model_called")) as model:
            with patch.object(adapter, "clean_git_configuration"):
                with patch.object(adapter, "git", side_effect=lambda root,*args: (
                    "" if args == ("remote",) else "main")):
                    with self.assertRaisesRegex(adapter.TransportBlocked, "builder_checkout_denied"):
                        self.instance.build(self.ctx())
            model.assert_not_called()

    def test_disallowed_model_patch_does_not_mutate_files(self):
        with patch.object(self.provider, "propose", return_value={
                "replacement": "    except HTTPError:\n        pass",
                "model": adapter.MODEL, "model_digest": DIGEST,
                "model_calls": 1, "eval_count": 7}):
            with patch.object(adapter, "clean_git_configuration"):
                with patch.object(adapter, "git", return_value=profile.BASE_SHA):
                    with self.assertRaises(Exception):
                        self.instance.build(self.ctx())
        self.assertEqual((self.root/profile.PATH).read_bytes(), BASE.encode())

    def test_builder_valid_candidate_produces_one_path_and_hash(self):
        head = "e" * 40
        commands = []
        def fake_git(root, *args):
            commands.append(args)
            if args == ("remote",): return ""
            if args == ("branch", "--show-current"): return "worker/ollama-http-repair"
            if args == ("status", "--porcelain", "--untracked-files=all"): return ""
            if args == ("diff", "--cached", "--name-only"): return profile.PATH
            if args == ("diff", "--name-only", profile.BASE_SHA, head): return profile.PATH
            if args == ("rev-parse", "HEAD"):
                return head if any("commit" in cmd for cmd in commands) else profile.BASE_SHA
            return ""
        with patch.object(self.provider, "propose", return_value={
                "replacement": REPLACEMENT, "model": adapter.MODEL,
                "model_digest": DIGEST, "model_calls": 1, "eval_count": 12}):
            with patch.object(adapter, "clean_git_configuration"):
                with patch.object(adapter, "git", side_effect=fake_git):
                    result = self.instance.build(self.ctx())
        self.assertEqual(result.produced_sha, head)
        self.assertEqual(result.changed_paths, (profile.PATH,))
        self.assertEqual(result.invoked_commands, ("ollama.transport.repair",))
        self.assertEqual((self.root/profile.PATH).read_text(), CANDIDATE)
        self.assertTrue(any("commit" in cmd for cmd in commands))

    def test_validator_rejects_skipped_and_wrong_sha(self):
        (self.root/profile.PATH).write_text(CANDIDATE)
        good = manifest(sha="e"*40,checksum=profile.digest(CANDIDATE.encode()))
        for changed in (dict(skipped_count=1), dict(source_sha256="f"*64),
                        dict(passed_count=23)):
            def negative(_root):
                return {**self.run_regressions(self.root), **changed}
            self.instance.regression_runner = negative
            # test the internal fixed command; native coordinator separately attests checkout
            with self.assertRaisesRegex(adapter.TransportBlocked, "validator_regressions_denied"):
                self.instance.commands(self.root)["ollama.transport.validate"]()
        self.instance.regression_runner = self.run_regressions
        with patch.object(adapter,"git",return_value="e"*40):
            with self.assertRaisesRegex(adapter.TransportBlocked, "validator_manifest_drift"):
                self.instance.validate(self.ctx(manifest_arg=manifest(sha="f"*40,
                    checksum=profile.digest(CANDIDATE.encode())), role="validator"), "e"*40)

    def test_validator_independent_payload_no_model_call(self):
        (self.root/profile.PATH).write_text(CANDIDATE)
        m = manifest(sha="e"*40,checksum=profile.digest(CANDIDATE.encode()))
        with patch.object(self.provider,"propose",side_effect=AssertionError("model_called")) as model:
            with patch.object(adapter,"git",return_value="e"*40):
                result=self.instance.validate(self.ctx(manifest_arg=m,role="validator"),"e"*40)
        model.assert_not_called()
        self.assertEqual(result.changed_paths,(profile.PATH,))
        self.assertEqual(result.invoked_commands,("ollama.transport.validate",))
        self.assertEqual(len(result.evidence_sha256),64)


if __name__ == "__main__":
    unittest.main()
