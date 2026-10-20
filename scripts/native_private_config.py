#!/usr/bin/env python3
"""Private, non-logging configuration intake for the native continuity runtime."""
from __future__ import annotations
import argparse
import getpass
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PRIVATE_ROOT = Path("/workspace/.reqsys-command-gateway-posix/private-continuity-v1")
BROKER_HOST = "br-royal-butterfly-b4as4z27-continuityidentity.compute.c-6.us-east-2.aws.neon.tech"
S3_ENDPOINT = "https://br-royal-butterfly-b4as4z27.storage.c-6.us-east-2.aws.neon.tech"
S3_BUCKET = "engineering-continuity-artifacts"
S3_PREFIX = "engineering-worker-pool/checkpoints/live-v1"
RUNTIME_KEYS = frozenset({
    "authority_id", "schema", "database_url", "aws_access_key_id",
    "aws_secret_access_key", "s3_endpoint", "s3_region", "s3_bucket", "s3_prefix",
})

class PrivateConfigurationError(RuntimeError):
    pass

def validate_url(value: str, *, role: str | None = None) -> str:
    try:
        url = urllib.parse.urlsplit(value)
        pairs = urllib.parse.parse_qsl(url.query, strict_parsing=True)
        query = dict(pairs)
        if (url.scheme != "postgresql" or not url.hostname
                or not re.fullmatch(r"[a-z0-9.-]+\.neon\.tech", url.hostname)
                or url.path != "/neondb" or not url.username or not url.password
                or url.fragment or url.port not in (None, 5432)
                or len(query) != len(pairs)
                or set(query) - {"sslmode", "sslrootcert", "channel_binding", "connect_timeout"}
                or query.get("sslmode") != "verify-full"
                or query.get("sslrootcert") != "system"
                or (role is not None and urllib.parse.unquote(url.username) != role)
                or any(c in value for c in "\r\n\0")):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PrivateConfigurationError("database_configuration_invalid") from None
    return value

def validate_runtime(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != RUNTIME_KEYS:
        raise PrivateConfigurationError("runtime_configuration_invalid")
    if not all(isinstance(v, str) and 0 < len(v) < 4096
               and not any(c in v for c in "\r\n\0") for v in value.values()):
        raise PrivateConfigurationError("runtime_configuration_invalid")
    validate_url(value["database_url"], role="ewp_continuity_runtime")
    if (not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value["schema"])
            or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value["authority_id"])
            or value["s3_endpoint"] != S3_ENDPOINT or value["s3_bucket"] != S3_BUCKET
            or value["s3_prefix"] != S3_PREFIX or value["s3_region"] != "us-east-2"):
        raise PrivateConfigurationError("runtime_configuration_invalid")
    return dict(value)

def _directory(path: Path) -> None:
    if os.name != "posix" or not path.is_absolute():
        raise PrivateConfigurationError("private_directory_invalid")
    for current in (*reversed(path.parents), path):
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise PrivateConfigurationError("private_directory_invalid")
        else:
            current.mkdir(mode=0o700)
    info = path.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PrivateConfigurationError("private_directory_permissions")

def read_private(path: Path, *, max_bytes: int = 16384) -> bytes:
    if os.name != "posix":
        raise PrivateConfigurationError("private_file_platform")
    for parent in path.parents:
        if parent.is_symlink():
            raise PrivateConfigurationError("private_file_parent")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o077
                or before.st_size > max_bytes):
            raise PrivateConfigurationError("private_file_permissions")
        payload = b""
        while len(payload) <= max_bytes:
            chunk = os.read(descriptor, min(4096, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(descriptor)
        current = path.lstat()
        fingerprint = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)
        if len(payload) > max_bytes or fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(current):
            raise PrivateConfigurationError("private_file_changed")
        return payload
    finally:
        os.close(descriptor)

def _write(path: Path, payload: bytes) -> str:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if read_private(path) != payload:
            raise PrivateConfigurationError("private_configuration_conflict") from None
        return "replayed"
    try:
        view = memoryview(payload)
        while view:
            size = os.write(descriptor, view)
            if size <= 0:
                raise PrivateConfigurationError("private_write_failed")
            view = view[size:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return "created"

def store_configuration(kind: str, value: object, directory: Path = PRIVATE_ROOT) -> dict[str, object]:
    if kind == "admin":
        if not isinstance(value, dict) or set(value) != {"database_url"}:
            raise PrivateConfigurationError("admin_configuration_invalid")
        url = validate_url(value["database_url"])
        outputs = {"admin.url": (url + "\n").encode("utf-8")}
    elif kind == "runtime":
        runtime = validate_runtime(value)
        outputs = {
            "runtime.json": (json.dumps(runtime, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
            "runtime.url": (runtime["database_url"] + "\n").encode("utf-8"),
        }
    else:
        raise PrivateConfigurationError("configuration_kind_invalid")
    _directory(directory)
    # Check conflicts before writing any member. An interrupted first write can be
    # completed idempotently; an existing, different configuration is never rotated.
    for name, payload in outputs.items():
        destination = directory / name
        if destination.exists() or destination.is_symlink():
            if read_private(destination) != payload:
                raise PrivateConfigurationError("private_configuration_conflict")
    states = [_write(directory / name, payload) for name, payload in outputs.items()]
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    for name, payload in outputs.items():
        if read_private(directory / name) != payload:
            raise PrivateConfigurationError("private_readback_failed")
    return {"result": "PRIVATE_CONFIGURATION_STORED", "kind": kind,
            "directory": str(directory), "replayed": all(s == "replayed" for s in states)}

def runtime_environment(directory: Path = PRIVATE_ROOT) -> dict[str, str]:
    runtime = validate_runtime(json.loads(read_private(directory / "runtime.json")))
    if read_private(directory / "runtime.url").decode("utf-8").strip() != runtime["database_url"]:
        raise PrivateConfigurationError("private_configuration_mismatch")
    return {
        "CODEX_WORKER_POOL_BACKEND": "postgres",
        "CODEX_WORKER_POOL_DATABASE_URL_FILE": str(directory / "runtime.url"),
        "CODEX_WORKER_POOL_DB_SCHEMA": runtime["schema"],
        "CODEX_WORKER_POOL_AUTHORITY_ID": runtime["authority_id"],
        "EWP_TEST_POSTGRES_URL_FILE": str(directory / "runtime.url"),
        "EWP_TEST_POSTGRES_SCHEMA": runtime["schema"],
        "EWP_TEST_POSTGRES_AUTHORITY_ID": runtime["authority_id"],
        "AWS_ACCESS_KEY_ID": runtime["aws_access_key_id"],
        "AWS_SECRET_ACCESS_KEY": runtime["aws_secret_access_key"],
        "AWS_DEFAULT_REGION": runtime["s3_region"],
        "CODEX_ARTIFACT_S3_BUCKET": runtime["s3_bucket"],
        "CODEX_ARTIFACT_S3_PREFIX": runtime["s3_prefix"],
        "CODEX_ARTIFACT_S3_REGION": runtime["s3_region"],
        "CODEX_ARTIFACT_S3_ENDPOINT": runtime["s3_endpoint"],
    }

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PrivateConfigurationError("identity_redirect_rejected")

def _json_request(request: urllib.request.Request, *, maximum: int) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            body = response.read(maximum + 1)
            if response.status != 200 or len(body) > maximum:
                raise PrivateConfigurationError("identity_response_invalid")
            payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise PrivateConfigurationError("identity_response_invalid")
        return payload
    except (urllib.error.URLError, ValueError, UnicodeError, OSError):
        raise PrivateConfigurationError("identity_exchange_failed") from None

def hydrate_github(directory: Path = PRIVATE_ROOT) -> dict:
    try:
        base = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
        bearer = os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
        parsed = urllib.parse.urlsplit(base)
        if (parsed.scheme != "https" or not parsed.hostname
                or not parsed.hostname.endswith(".actions.githubusercontent.com")
                or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment
                or "\r" in bearer or "\n" in bearer):
            raise PrivateConfigurationError("github_identity_source_invalid")
        query = urllib.parse.parse_qsl(parsed.query)
        query = [(k, v) for k, v in query if k != "audience"]
        query.append(("audience", "urn:engineering-worker-pool:continuity:v1"))
        endpoint = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
        token = _json_request(urllib.request.Request(endpoint, headers={"Authorization": "Bearer " + bearer}), maximum=20000).get("value")
        if not isinstance(token, str) or not 20 < len(token) < 16384:
            raise PrivateConfigurationError("github_identity_invalid")
        run_id = os.environ["GITHUB_RUN_ID"]
        run_attempt = os.environ["GITHUB_RUN_ATTEMPT"]
        head = os.environ["GITHUB_SHA"]
        if not re.fullmatch(r"[1-9][0-9]{0,19}", run_id) or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise PrivateConfigurationError("github_context_invalid")
        body = json.dumps({"purpose": "native-continuity-runtime-v1", "run_id": run_id}).encode("utf-8")
        result = _json_request(urllib.request.Request("https://" + BROKER_HOST + "/bootstrap", data=body,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST"), maximum=16384)
        expected = {"repository_id": "1384155635", "sha": head, "run_id": run_id, "run_attempt": run_attempt}
        if result.get("version") != 1 or result.get("identity") != expected:
            raise PrivateConfigurationError("github_identity_readback_mismatch")
        receipt = store_configuration("runtime", result.get("runtime"), directory)
        receipt["identity"] = expected
        return receipt
    except KeyError:
        raise PrivateConfigurationError("github_identity_unavailable") from None

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("store-admin", "store-runtime", "hydrate-github"))
    args = parser.parse_args()
    try:
        if args.action == "hydrate-github":
            receipt = hydrate_github()
        else:
            if os.name != "posix" or not sys.stdin.isatty():
                raise PrivateConfigurationError("private_tty_required")
            value = json.loads(getpass.getpass("Private continuity configuration (hidden): "))
            receipt = store_configuration(args.action.removeprefix("store-"), value)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except (PrivateConfigurationError, ValueError, TypeError, OSError):
        # Never print exception text, submitted input, DSNs, bearer tokens, or SDK
        # response bodies. Detailed non-secret decisions are covered by tests.
        print(json.dumps({"result": "PRIVATE_CONFIGURATION_BLOCKED"}))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
