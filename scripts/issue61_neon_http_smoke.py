"""Read-only EWP #61 Neon isolated Function smoke; no credentials or mutations.

Only the fixed isolated child endpoint may be addressed. GitHub Actions job pins
and validates the repository, branch and exact checkout SHA before invocation.
"""
import json
import ssl
import sys
import urllib.error
import urllib.request

HOST = (
    "https://br-dry-thunder-b4e2yem1-continuityidentity"
    ".compute.c-6.us-east-2.aws.neon.tech"
)
MAX_BODY = 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def make_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        NoRedirect(),
    )


def call(opener, path, method="GET", body=None):
    if path not in ("/health", "/path-not-installed", "/bootstrap"):
        raise ValueError("unexpected_path")
    if path == "/bootstrap":
        if method != "POST" or body is None:
            raise ValueError("unexpected_method_or_body")
    elif method != "GET" or body is not None:
        raise ValueError("unexpected_method_or_body")
    request = urllib.request.Request(
        HOST + path,
        method=method,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        response = opener.open(request, timeout=12)
    except urllib.error.HTTPError as ex:
        response = ex
    with response:
        code = response.status if hasattr(response, "status") else response.code
        kind = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
        raw = response.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY or kind != "application/json":
        raise ValueError("non_json_or_large_response")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("invalid_json_response") from None
    if not isinstance(result, dict):
        raise ValueError("response_not_object")
    return code, result


def check(opener):
    checks = (
        ("GET", "/health", None, 200, {"service": "continuity-identity", "version": 1}),
        ("GET", "/path-not-installed", None, 404, {"error": "not_found"}),
        ("POST", "/bootstrap", b'{"purpose":"native-continuity-runtime-v1","run_id":"1"}', 401, {"error": "unauthorized"}),
    )
    for method, path, body, expected_status, expected_json in checks:
        status, payload = call(opener, path, method, body)
        if status != expected_status or payload != expected_json:
            raise AssertionError("neon_http_contract_failure:" + path + ":" + str(status))
    return True


def main():
    try:
        check(make_opener())
    except (AssertionError, ValueError, urllib.error.URLError, TimeoutError, OSError) as ex:
        # Never serialize response bodies, credentials or exception text to CI logs.
        print("EWP61_ISOLATED_HTTP_SMOKE=FAIL category=" + type(ex).__name__)
        return 1
    print("EWP61_ISOLATED_HTTP_SMOKE=PASS health=200 unknown=404 unsigned=401 no_write=true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
