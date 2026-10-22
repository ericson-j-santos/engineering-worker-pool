"""Contract tests of read-only canary preflight; NOT a physical E2E."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "candidate_readonly_canary.py"
SPEC = importlib.util.spec_from_file_location("canary_preflight", SCRIPT)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)
SHA = "a" * 40
URL = "http://127.0.0.1:18097"
TOKEN = "canary-test-only-12345678"


def token_file(tmp_path):
    target = tmp_path / "private.token"
    target.write_text(TOKEN, encoding="utf-8")
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return target


def responder(*, bad_task: bool = False, bad_version: bool = False, changed: bool = False,
              unauth_success: bool = False):
    calls = []
    def fn(url: str, token: str | None) -> tuple[int, dict[str, Any]]:
        calls.append((url, token is not None))
        if url.endswith("/health"):
            return 200, {"status": "healthy", "contract_version": "v1", "auth_configured": True}
        if url.endswith("/openapi.json"):
            return 200, {"info": {"version": "1.2.0" if bad_version else "1.4.0"},
                         "paths": {"/v1/tasks/{task_id}/diagnostics": {}}}
        if url.endswith("/v1/contract"):
            if not token:
                return (200 if unauth_success else 401), {}
            return 200, {"contract_name": "engineering-worker-pool", "contract_version": "v1"}
        if url.endswith("cwp-000000000000000000000000/diagnostics"):
            return 404, {}
        if "/v1/tasks/" in url:
            task_id = url.split("/v1/tasks/", 1)[1].split("/", 1)[0]
            issue = mod.TASKS[task_id]
            if url.endswith("/diagnostics"):
                return 200, {"read_mode": "sqlite_mode_ro", "claim_performed": False,
                             "reconciliation_requested": False, "workers": [],
                             "precheck_eligible_workers": 0,
                             "task": {"task_id": task_id, "issue_number": (3 if bad_task else issue),
                                      "repository": mod.REPOSITORY, "state": "queued"},
                             "lane": {"configured": True, "enabled": True}}
            return 200, {"task_id": task_id, "state": ("leased" if changed else "queued"),
                         "leased_by": None, "attempt_count": 0}
        raise AssertionError(f"unexpected path: {url}")
    return fn, calls


def run(tmp_path, fn, head_fn=lambda: SHA):
    return mod.run_probe(pool_url=URL, token_file=token_file(tmp_path), expected_sha=SHA,
                         correlation_id="canary-test-20261013", request_fn=fn, head_fn=head_fn)


def test_positive_all_gets_and_no_mutation_or_false_cutover(tmp_path):
    fn, calls = responder()
    result = run(tmp_path, fn)
    assert result["result"] == "CANDIDATE_READONLY_PREFLIGHT_OK"
    assert result["cutover_ready"] is False
    assert result["isolated_candidate_canary_passed"] is False
    assert result["candidate_image_digest_attested"] is False
    assert result["runtime_mutated_by_probe"] is False
    assert result["production_touched"] is False
    assert result["port"] == 18097
    assert len(result["tasks"]) == 2
    assert not any("/v1/snapshot" in url or "/v1/workers" in url for url, _ in calls)
    assert calls.count((URL + "/v1/contract", False)) == 1
    assert calls.count((URL + "/v1/contract", True)) == 1
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8097", "https://127.0.0.1:18097", "http://localhost:18097",
    "http://127.0.0.1:18097@evil.example", "http://127.0.0.1:18097/other",
    "http://127.0.0.1:18097/?mode=unsafe", "http://127.0.0.1:18097#foo",
    "http://127.0.0.1:80", "http://127.0.0.1:0", "http://127.0.0.1:65536",
])
def test_bad_or_live_url_blocked(url):
    with pytest.raises(mod.CanaryBlocked):
        mod.validate_url(url)


@pytest.mark.parametrize("case,reason", [
    ("bad_version", "candidate_api_version_or_route_invalid"),
    ("bad_task", "candidate_task_or_isolation_precheck_invalid"),
    ("changed", "candidate_task_or_isolation_precheck_invalid"),
    ("unauth_success", "candidate_http_status_mismatch"),
])
def test_fail_closed_on_wrong_contract_or_identity(tmp_path, case, reason):
    fn, _ = responder(**{case: True})
    with pytest.raises(mod.CanaryBlocked, match=reason):
        run(tmp_path, fn)


def test_replay_read_only_and_deterministic(tmp_path):
    fn, calls = responder()
    first = run(tmp_path, fn)
    second = run(tmp_path, fn)
    assert first == second
    assert len(calls) == 22  # exactly 11 GETs per invocation


def test_checkout_sha_mismatch_prevents_network_call(tmp_path):
    fn, calls = responder()
    with pytest.raises(mod.CanaryBlocked, match="checkout_sha_mismatch"):
        run(tmp_path, fn, head_fn=lambda: "b" * 40)
    assert not calls


def test_unprotected_token_file_rejected_without_network(tmp_path):
    file = token_file(tmp_path)
    if os.name != "nt":
        file.chmod(0o644)
        with pytest.raises(mod.CanaryBlocked, match="token_file_permissions"):
            mod.run_probe(pool_url=URL, token_file=file, expected_sha=SHA,
                          correlation_id="test-1", request_fn=responder()[0], head_fn=lambda: SHA)


def test_symlink_token_blocked(tmp_path):
    source = token_file(tmp_path)
    link = tmp_path / "alias.token"
    try:
        link.symlink_to(source)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
    with pytest.raises(mod.CanaryBlocked, match="token_file_not_regular"):
        mod.read_token(link)


def test_artifact_is_sanitized(tmp_path):
    fn, _ = responder()
    evidence = run(tmp_path, fn)
    path = tmp_path / "evidence.json"
    mod.write_evidence(path, evidence)
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert restored == evidence
    assert TOKEN not in path.read_text(encoding="utf-8")
    assert "lease_token" not in path.read_text(encoding="utf-8")


def test_actual_http_transport_blocks_redirects_and_no_proxy():
    """Exercise real stdlib HTTP transport locally without an API mock argument."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:8097/health")
                self.end_headers()
            elif self.path == "/unauth":
                self.send_response(401)
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    addr = f"http://127.0.0.1:{server.server_port}"
    try:
        assert mod.http_request(addr + "/unauth", None) == (401, {})
        assert mod.http_request(addr + "/ok", TOKEN) == (200, {"ok": True})
        with pytest.raises(mod.CanaryBlocked, match="redirect_blocked"):
            mod.http_request(addr + "/redirect", TOKEN)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
