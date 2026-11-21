"""Builder source proposal negative fixtures; no simulated model success claim."""
from __future__ import annotations

import json
import unittest

from scripts import noteri42_model_repair as builder


def old():
    return (b"name: Noteri Control Plane Fallback Probe\n"
            b"          if: inputs.mode == 'rules219_readonly'\n"
            + builder.OLD.encode() + b"\n"
            b"          if ($envelope.gateway_exit_code -ne 0) { throw 'BLOCK' }\n")


def fixed():
    return "\n".join([
        "          $gatewayExit = $LASTEXITCODE",
        "          if (-not $raw) {",
        "            throw 'NOTERI_219_GATEWAY_ENVELOPE_MISSING'",
        "          }",
        "          try {",
        "            $envelope = @($raw)[-1] | ConvertFrom-Json -ErrorAction Stop",
        "          }",
        "          catch {",
        "            throw 'NOTERI_219_GATEWAY_ENVELOPE_INVALID'",
        "          }",
        "          if ($gatewayExit -ne 0) {",
        "            if ($envelope.gateway_exit_code -in @(20,21,22,23,24,25,26)) {",
        '              throw "NOTERI_219_GATEWAY_POLICY_EXIT_$($envelope.gateway_exit_code)"',
        "            }",
        "            throw 'NOTERI_219_GATEWAY_FAILURE_UNCLASSIFIED'",
        "          }",
    ])


class ModelRepairContracts(unittest.TestCase):
    def test_single_target_only_and_repeat_is_denied(self):
        replacement = fixed()
        after = builder.candidate(old(), replacement)
        self.assertEqual(after.count(replacement.encode()), 1)
        self.assertIn(b"NOTERI_219_GATEWAY_POLICY_EXIT_", after)
        self.assertIn(b"if ($envelope.gateway_exit_code -ne 0)", after)
        with self.assertRaises(builder.Blocked):
            builder.candidate(after, replacement)

    def test_fails_when_original_block_missing_or_ambiguous(self):
        for before in (old().replace(builder.OLD.encode(), b""),
                       old() + builder.OLD.encode()):
            with self.subTest(size=len(before)), self.assertRaises(builder.Blocked):
                builder.candidate(before, fixed())

    def test_rejects_shell_and_io_in_model_response(self):
        for payload in ("          Write-Output $raw",
                        "          Invoke-Expression 'whoami'",
                        "          $gatewayExit = $LASTEXITCODE; Remove-Item $raw",
                        "          if ($env:TOKEN -ne 0) {"):
            with self.subTest(payload=payload), self.assertRaises(builder.Blocked):
                builder.candidate(old(), fixed().replace(
                    "          $gatewayExit = $LASTEXITCODE", payload))

    def test_rejects_balancing_and_nonreviewed_codes(self):
        for value in (fixed().replace("          }", "", 1),
                      fixed().replace("NOTERI_219_GATEWAY_FAILURE_UNCLASSIFIED",
                                      "NOTERI_219_GATEWAY_SUCCESS"),
                      fixed() + "\n          throw 'NOTERI_219_GATEWAY_FALSE_OK'"):
            with self.subTest(value=value[-45:]), self.assertRaises(builder.Blocked):
                builder.candidate(old(), value)

    def test_context_requires_task_model_evidence_and_gateway(self):
        env = {
            "NOTERI42_TASK_ID": "cwp-" + "a" * 24,
            "NOTERI42_MODEL_DIGEST": "b" * 64,
            "NOTERI42_BASE_SHA": builder.BASE_SHA,
            "NOTERI42_BUILDER_ROLE": "builder",
            "NOTERI42_GATEWAY_BOUND": "true",
            "NOTERI42_BASELINE_READBACK": "verified",
            "NOTERI42_BASELINE_EVIDENCE_SHA256": "c" * 64,
            "NOTERI42_COST_ZERO_ATTESTED": "true",
        }
        self.assertEqual(builder.context(env), (env["NOTERI42_TASK_ID"], "b"*64))
        for key in env:
            with self.subTest(key=key), self.assertRaises(builder.Blocked):
                builder.context({**env, key: ""})

    def test_local_ollama_only_and_request_is_bounded(self):
        class Response:
            status = 200
            def geturl(self):
                return builder.ENDPOINT + "/api/tags"
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def read(self, limit):
                assert limit == 65537
                return b'{"models":[]}'
        class Opener:
            def open(self, request, timeout):
                self.request, self.timeout = request, timeout
                return Response()
        client = Opener()
        self.assertEqual(builder.ollama(client, "/api/tags"),
                         {"models": []})
        self.assertEqual(client.request.full_url,
                         "http://127.0.0.1:11434/api/tags")
        self.assertEqual(client.request.get_method(), "GET")
        with self.assertRaises(builder.Blocked):
            builder.ollama(client, "/evil")

    def test_no_success_without_real_model_execution(self):
        text = __import__("pathlib").Path(builder.__file__).read_text()
        self.assertIn('"/api/chat"', text)
        self.assertIn('"/api/ps"', text)
        self.assertIn('"model_calls_observed": 1', text)
        self.assertIn('"gateway_attested": False', text)
        self.assertIn('"cas_verified": False', text)
        self.assertNotIn("subprocess.Popen", text)
        self.assertNotIn("shell=True", text)


if __name__ == "__main__":
    unittest.main()
