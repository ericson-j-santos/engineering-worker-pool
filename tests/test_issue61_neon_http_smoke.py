"""Offline security tests for the fixed-host, fail-closed Neon HTTP probe."""
from pathlib import Path
import importlib.util
import json
import urllib.error
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "issue61_neon_http_smoke.py"
spec = importlib.util.spec_from_file_location("issue61_neon_http_smoke", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class Response:
    def __init__(self, status, body):
        self.status = status
        self.code = status
        self.headers = {"Content-Type": "application/json; charset=utf-8"}
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size):
        return self.body[:size]


class Opener:
    def __init__(self, response_for):
        self.response_for = response_for
        self.calls = []

    def open(self, req, timeout):
        assert timeout <= 12
        assert req.full_url.startswith(probe.HOST + "/")
        assert req.headers.get("Authorization") is None
        self.calls.append((req.get_method(), req.full_url))
        return self.response_for(req)


def ok(req):
    return {
        "/health": Response(200, {"service": "continuity-identity", "version": 1}),
        "/path-not-installed": Response(404, {"error": "not_found"}),
        "/bootstrap": Response(401, {"error": "unauthorized"}),
    }[req.full_url[len(probe.HOST):]]


def test_positive_and_all_three_distinct_routes():
    opener = Opener(ok)
    assert probe.check(opener) is True
    assert [m for m, _ in opener.calls] == ["GET", "GET", "POST"]
    assert len(set(opener.calls)) == 3


def test_http200_only_is_not_enough():
    opener = Opener(lambda req: Response(200, {"error": "not_found"}))
    try:
        probe.check(opener)
        raise AssertionError("must_fail")
    except AssertionError as ex:
        assert str(ex).startswith("neon_http_contract_failure:")


def test_wrong_negative_status_fails():
    def responses(req):
        if req.full_url.endswith("/bootstrap"):
            return Response(200, {"error": "unauthorized"})
        return ok(req)
    try:
        probe.check(Opener(responses))
        raise AssertionError("must_fail")
    except AssertionError as ex:
        assert "/bootstrap" in str(ex)


def test_wrong_body_fails_even_on_expected_status():
    def responses(req):
        if req.full_url.endswith("/health"):
            return Response(200, {"service": "wrong", "version": 1})
        return ok(req)
    try:
        probe.check(Opener(responses))
        raise AssertionError("must_fail")
    except AssertionError as ex:
        assert "/health" in str(ex)


def test_forbid_arbitrary_target_and_method():
    opener = Opener(ok)
    for args in (("/admin",), ("/health", "POST", b"{}"), ("/bootstrap", "POST", None)):
        try:
            probe.call(opener, *args)
            raise AssertionError("must_fail")
        except ValueError:
            pass
    assert not opener.calls


def test_fail_closed_on_network_errors_without_leaking_exception_text():
    opener = Opener(lambda req: (_ for _ in ()).throw(urllib.error.URLError("secret-should-not-log")))
    with patch.object(probe, "make_opener", return_value=opener):
        with patch("builtins.print") as p:
            assert probe.main() == 1
            assert "secret-should-not-log" not in p.call_args.args[0]
            assert "FAIL" in p.call_args.args[0]


def test_redirect_is_explicitly_disabled():
    assert probe.NoRedirect().redirect_request(None, None, None, None, None, None) is None
