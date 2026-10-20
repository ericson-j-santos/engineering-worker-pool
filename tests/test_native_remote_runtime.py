"""Offline security and composition controls; mocks are not provider evidence."""
from __future__ import annotations
import json
import os
import ssl
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from app import native_remote_runtime as remote
from app.native_one_shot import NativeExecutorBlocked
from app.s3_git_bundle_artifact import S3ArtifactConfig

TASK = "cwp-" + "1" * 24
PATH = "/v1/tasks/" + TASK
ORIGIN = "https://coordinator.example.invalid"
TOKEN = "synthetic-unit-test-token-only"


def response(raw=b"{}", status=200, **headers):
    return httpx.Response(status, stream=httpx.ByteStream(raw),
                          headers={"content-type": "application/json", **headers})


class RemoteTransportTests(unittest.TestCase):
    def api(self, handler, provider=None):
        api = remote.HttpsWorkerApi(
            route=remote.HttpsWorkerRoute(ORIGIN),
            token_provider=provider or (lambda: TOKEN),
        )
        api.close()
        api._client = httpx.Client(transport=httpx.MockTransport(handler),
                                  trust_env=False, follow_redirects=False)
        self.addCleanup(api.close)
        return api

    def test_origin_positive(self):
        for value in (ORIGIN, "https://127.0.0.1:8443", "https://[::1]:8443"):
            with self.subTest(value=value):
                self.assertEqual(remote.HttpsWorkerRoute(value).origin, value)

    def test_origin_negative(self):
        values = [
            None, "", "http://pool.invalid", ORIGIN + "/", ORIGIN + "/v1",
            ORIGIN + "?x", ORIGIN + "#x", "https://user:password@pool.invalid",
            "https://POOL.invalid", "https://pool.invalid:443",
            "https://pool.invalid:0", "https://pool.invalid:65536",
            "https://pool.invalid:abc", "https://pool..invalid",
            "https://-pool.invalid", "https://pool.invalid.",
            "https://pool.invalid\n", " https://pool.invalid",
            "https://pool.invalid%2f.evil", "https://[fe80::1%eth0]",
            "https://pöol.invalid",
        ]
        for value in values:
            with self.subTest(value=repr(value)):
                with self.assertRaisesRegex(NativeExecutorBlocked, "^worker_origin_invalid$"):
                    remote.HttpsWorkerRoute(value)

    def test_tls_and_environment_closed(self):
        with patch.dict(os.environ, {
            "SSLKEYLOGFILE": "/never-created-by-this-test",
            "SSL_CERT_FILE": "/missing-ca", "SSL_CERT_DIR": "/missing-ca-dir",
            "HTTPS_PROXY": "http://127.0.0.1:9",
        }):
            context = remote.HttpsWorkerRoute(ORIGIN).ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertIsNone(context.keylog_filename)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_bad_ca_before_credentials(self):
        provider = Mock(return_value=TOKEN)
        with self.assertRaisesRegex(NativeExecutorBlocked, "^worker_ca_invalid$"):
            remote.HttpsWorkerApi(route=remote.HttpsWorkerRoute(ORIGIN, "invalid CA"),
                                  token_provider=provider)
        provider.assert_not_called()
        with self.assertRaises(NativeExecutorBlocked):
            remote.HttpsWorkerRoute(ORIGIN, "-----BEGIN PRIVATE KEY-----")

    def test_no_caller_supplied_client_or_unverified_context(self):
        with self.assertRaises(TypeError):
            remote.HttpsWorkerApi(route=remote.HttpsWorkerRoute(ORIGIN),
                                  token_provider=lambda: TOKEN, client=Mock())
        with self.assertRaises(TypeError):
            remote.HttpsWorkerApi(route=remote.HttpsWorkerRoute(ORIGIN),
                                  token_provider=lambda: TOKEN, verify=False)

    def test_exact_origin_no_mutable_base_redirect(self):
        seen = []
        def handler(request):
            seen.append((str(request.url), request.headers["authorization"]))
            return response(b'{"ok":true}')
        api = self.api(handler)
        api.base_url = "https://another.example.invalid"
        self.assertEqual(api._request("GET", PATH), {"ok": True})
        self.assertEqual(seen, [(ORIGIN + PATH, "Bearer " + TOKEN)])

    def test_admin_paths_before_provider(self):
        provider = Mock(return_value=TOKEN)
        api = self.api(lambda _: self.fail("network must not be called"), provider)
        for method, path in [
            ("POST", "/v1/workers"), ("POST", "/v1/repositories"),
            ("GET", PATH + "?x"), ("GET", "/v1/tasks/../workers"),
            ("GET", "//evil.invalid"), ("DELETE", PATH), ("GET", None),
        ]:
            with self.subTest(method=method, path=path):
                with self.assertRaises(NativeExecutorBlocked):
                    api._request(method, path, {} if method == "POST" else None)
        provider.assert_not_called()

    def test_tokens_closed_and_provider_errors_redacted(self):
        for value in ["", None, "short", TOKEN + "\r\nInjected: value", TOKEN + " "]:
            with self.subTest(token_type=type(value).__name__):
                api = self.api(lambda _: self.fail("no network"), lambda: value)
                with self.assertRaisesRegex(NativeExecutorBlocked, "^worker_token_invalid$"):
                    api._request("GET", PATH)
        api = self.api(lambda _: self.fail("no network"),
                       Mock(side_effect=RuntimeError("DO-NOT-EXPOSE")))
        with self.assertRaisesRegex(NativeExecutorBlocked,
                                    "^worker_token_provider_unavailable$") as failure:
            api._request("GET", PATH)
        self.assertNotIn("DO-NOT-EXPOSE", str(failure.exception))

    def test_redirection_never_followed(self):
        calls = []
        def handler(request):
            calls.append(request)
            return response(status=307, location="https://evil.invalid")
        api = self.api(handler)
        with self.assertRaisesRegex(NativeExecutorBlocked, "^worker_http_unverified$"):
            api._request("POST", PATH + "/start", {"worker_id": "builder"})
        self.assertEqual(len(calls), 1)

    def test_post_timeout_not_retried(self):
        handler = Mock(side_effect=httpx.ReadTimeout("private body not logged"))
        api = self.api(handler)
        with self.assertRaisesRegex(NativeExecutorBlocked, "^worker_transport_unverified$"):
            api._request("POST", PATH + "/start", {"worker_id": "builder"})
        self.assertEqual(handler.call_count, 1)

    def test_json_and_size_closed(self):
        for raw in (b"[]", b'{"a":1,"a":2}', b'{"a":NaN}', b"invalid",
                    b"\xff", b" " * (remote.MAX_RESPONSE_BYTES + 1)):
            with self.subTest(raw_size=len(raw)):
                api = self.api(lambda _, raw=raw: response(raw))
                with self.assertRaisesRegex(NativeExecutorBlocked, "^worker_response_invalid$"):
                    api._request("GET", PATH)

    def test_encoding_and_status_closed(self):
        for status, headers, code in [
            (401, {}, "worker_auth_rejected"), (500, {}, "worker_http_unverified"),
            (200, {"content-encoding": "gzip"}, "worker_response_invalid"),
            (200, {"content-type": "text/plain"}, "worker_response_invalid"),
        ]:
            api = self.api(lambda _, s=status, h=headers: response(status=s, **h))
            with self.assertRaisesRegex(NativeExecutorBlocked, "^" + code + "$"):
                api._request("GET", PATH)

    def test_provider_refreshed_no_cookie_reuse(self):
        provider = Mock(side_effect=[TOKEN, TOKEN + "-new"])
        seen = []
        def handler(request):
            seen.append(dict(request.headers))
            return response(**{"set-cookie": "test-cookie=ignored"})
        api = self.api(handler, provider)
        api._request("GET", PATH)
        api._request("GET", PATH)
        self.assertEqual(provider.call_count, 2)
        self.assertNotIn("cookie", seen[1])
        self.assertEqual(seen[1]["authorization"], "Bearer " + TOKEN + "-new")

    def test_request_payload_bound_before_credentials(self):
        provider = Mock(return_value=TOKEN)
        api = self.api(lambda _: self.fail("no network"), provider)
        for payload in [None, [], {"bad": float("nan")},
                        {"oversized": "x" * remote.MAX_RESPONSE_BYTES}]:
            with self.assertRaisesRegex(NativeExecutorBlocked, "^worker_payload_invalid$"):
                api._request("POST", PATH + "/start", payload)
        provider.assert_not_called()


class RuntimeCompositionTests(unittest.TestCase):
    def runtime(self):
        authority = remote.GatewayAuthority(
            types.ModuleType("not_a_gateway"), Path("/not-read-by-this-test"),
            "session-test", "remote-composition-test", "a" * 40, "b" * 64, "c" * 64,
        )
        return remote.RemoteNativeExecutor(
            route=remote.HttpsWorkerRoute(ORIGIN), token_provider=Mock(return_value=TOKEN),
            s3_config=S3ArtifactConfig("fixture-bucket", "us-east-1"),
            s3_client=Mock(put_object=Mock(), get_object=Mock()),
            workspace=types.SimpleNamespace(root=Path("/fixture")),
            receipts=Mock(), adapters={}, command_registries={},
            authority=authority, fenced_adapter_ids=frozenset(),
        )

    def manifest(self):
        return {
            "schema_version": 1, "task_id": TASK, "repository": "fixture/continuity",
            "base_sha": "a" * 40, "workspace_sha": "a" * 40,
            "adapter_id": "fixture-adapter", "adapter_contract_sha256": "b" * 64,
            "before_sha256": {"src/value.py": "c" * 64},
        }

    def test_real_attestor_blocks_before_http_and_s3(self):
        runtime = self.runtime()
        with patch.object(remote, "HttpsWorkerApi") as api:
            with self.assertRaisesRegex(NativeExecutorBlocked, "^native_authority_invalid$"):
                runtime.run(self.manifest(), role="builder", worker_id="worker-test")
        api.assert_not_called()
        runtime.token_provider.assert_not_called()
        runtime.artifacts.client.put_object.assert_not_called()
        runtime.artifacts.client.get_object.assert_not_called()

    def test_composition_reuses_executor_and_transports(self):
        runtime = self.runtime()
        boundary = object()
        with patch.object(remote, "attest_execution_boundary", return_value=boundary) as attest:
            with patch.object(remote, "HttpsWorkerApi") as http:
                with patch.object(remote, "NativeOneShotExecutor") as executor:
                    executor.return_value.run.return_value = {"fixture_result": "done"}
                    got = runtime.run(self.manifest(), role="builder", worker_id="worker-test")
        self.assertEqual(got, {"fixture_result": "done"})
        self.assertIs(executor.call_args.kwargs["boundary"], boundary)
        self.assertIs(executor.call_args.kwargs["artifact_transport"], runtime.artifacts)
        self.assertIs(executor.call_args.kwargs["api"], http.return_value.__enter__.return_value)
        self.assertEqual(attest.call_args.kwargs["expected_head"], "a" * 40)
        http.return_value.__exit__.assert_called_once()

    def test_manifest_cannot_override_route(self):
        runtime = self.runtime()
        manifest = self.manifest()
        manifest["endpoint_url"] = "https://untrusted.invalid"
        with patch.object(remote, "HttpsWorkerApi") as api:
            with self.assertRaises(NativeExecutorBlocked):
                runtime.run(manifest, role="builder", worker_id="worker-test")
        api.assert_not_called()

    def test_head_drift_before_attestation(self):
        runtime = self.runtime()
        manifest = self.manifest()
        manifest["workspace_sha"] = "d" * 40
        with patch.object(remote, "attest_execution_boundary") as attest:
            with self.assertRaisesRegex(NativeExecutorBlocked, "^boundary_manifest_drift$"):
                runtime.run(manifest, role="builder", worker_id="worker-test")
        attest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
