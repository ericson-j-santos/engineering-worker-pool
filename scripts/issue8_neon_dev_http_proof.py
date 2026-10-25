"""Strict native HTTPS readback of an isolated Neon OIDC function in DEV.

No credentials, model calls, PostgreSQL, file writes or external mutation.
The host is a code-pinned DEV child branch, never the primary broker.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

HOST = "br-gentle-river-b4yz6b44-continuityidentity.compute.c-6.us-east-2.aws.neon.tech"
ORIGIN = "https://" + HOST
SOURCE_SHA = "ac877d3f9fa74b2b4308f2c2f0e4af72ed37de24"
PACKAGE_SHA256 = "cc75a58dd464f9af9f937a3346f532f4eb83e4a266257fcaf17fc5f801370ea0"
BRANCH = "proof/issue8-neon-dev-oidc-20261025"


class Blocked(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def get_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        NoRedirect(),
    )


def response(opener, path, *, method="GET", payload=None):
    if path not in ("/health", "/path-not-installed", "/bootstrap"):
        raise Blocked("unknown_target_blocked")
    req = urllib.request.Request(
        ORIGIN + path,
        data=payload,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with opener.open(req, timeout=12) as answer:
            code = answer.status
            kind = answer.headers.get("Content-Type", "")
            body = answer.read(1025)
    except urllib.error.HTTPError as err:
        with err:
            code = err.code
            kind = err.headers.get("Content-Type", "")
            body = err.read(1025)
    except (OSError, ValueError, TimeoutError):
        raise Blocked("https_transport_unverified") from None
    if len(body) > 1024:
        raise Blocked("response_boundedness_failed")
    return code, kind.split(";")[0].strip(), body


def require_healthy(status, kind, body):
    """Classify current HTTP failure without exposing any response payload."""
    if status != 200:
        code = str(status) if status in (301, 302, 307, 308, 401, 403, 404,
                                        429, 500, 502, 503, 504) else "other"
        raise Blocked("function_health_http_" + code)
    if kind != "application/json":
        raise Blocked("function_health_content_type_invalid")
    try:
        record = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise Blocked("function_health_json_invalid") from None
    if record != {"service": "continuity-identity", "version": 1}:
        raise Blocked("function_health_schema_mismatch")


def run():
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_REPOSITORY")
        != "ericson-j-santos/engineering-worker-pool"
        or os.environ.get("GITHUB_ACTOR") != "ericson-j-santos"
        or os.environ.get("GITHUB_REF") != "refs/heads/" + BRANCH
        or not re.fullmatch(r"[0-9a-f]{40}", os.environ.get("GITHUB_SHA", ""))
    ):
        raise Blocked("github_identity_unverified")
    opener = get_opener()
    for repeat in range(2):
        ok, kind, body = response(opener, "/health")
        require_healthy(ok, kind, body)
        unknown, _, _ = response(opener, "/path-not-installed")
        if unknown != 404:
            raise Blocked("unrecognized_route_not_rejected")
        unauthorized, _, _ = response(
            opener, "/bootstrap", method="POST",
            payload=b'{"purpose":"native-continuity-runtime-v1","run_id":"1"}',
        )
        if unauthorized != 401:
            raise Blocked("unauthenticated_request_not_rejected")
    return {
        "result": "OIDC_DEV_HTTP_E2E_VERIFIED",
        "source_sha": SOURCE_SHA,
        "bundle_sha256": PACKAGE_SHA256,
        "expected_function_branch": "br-gentle-river-b4yz6b44",
        "repeat_count": 2,
        "health_status": 200,
        "unknown_path_status": 404,
        "missing_credential_status": 401,
        "main_function_touched": False,
        "model_calls": 0,
        "writes": 0,
    }


def main():
    try:
        print(json.dumps(run(), sort_keys=True))
    except Exception as exc:
        label = str(exc) if isinstance(exc, Blocked) and re.fullmatch(
            r"[a-z][a-z0-9_]{2,64}", str(exc)
        ) else "probe_unverified"
        print(json.dumps({
            "result": "OIDC_DEV_HTTP_E2E_BLOCKED",
            "reason_code": label,
            "model_calls": 0,
            "writes": 0,
        }, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
