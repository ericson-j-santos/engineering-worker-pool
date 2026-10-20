#!/usr/bin/env python3
"""Probe a real S3 artifact store without putting credentials in argv or files.

The caller must provide exactly one newline-terminated JSON object on stdin:

    {"aws_access_key_id":"...","aws_secret_access_key":"..."}

Run this process with a pipe, never a TTY.  Route settings are non-secret CLI
arguments.  Output is deliberately bounded to a fixed, non-secret receipt.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, MutableMapping, Protocol, TextIO
from urllib.parse import urlsplit

try:
    import termios as _termios
except ImportError:  # pragma: no cover - Windows fails closed at runtime.
    _termios = None

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.native_one_shot import NativeExecutorBlocked
from app.s3_git_bundle_artifact import (
    S3ArtifactConfig,
    S3GitBundleArtifactTransport,
    _is_precondition_failed,
)


MAX_CREDENTIAL_LINE_BYTES = 16_384
TTY_PUBLIC_MARKER = b"S3_ARTIFACT_STORE_SMOKE_TTY_V1\n"
TTY_ECHO_DISABLED_ACK = b"S3_ARTIFACT_STORE_SMOKE_ECHO_DISABLED_V1\n"
ACCESS_KEY = re.compile(r"[A-Za-z0-9._+=/@:-]{3,512}\Z")
SAFE_TRANSPORT_REASONS = {
    "artifact_store_config_invalid",
    "artifact_store_dependency_missing",
    "artifact_store_unavailable",
}
SECRET_ENVIRONMENT_KEYS = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AWS_EC2_METADATA_DISABLED",
}


class ArtifactStoreSmokeBlocked(RuntimeError):
    """Fail-closed error whose message is always a fixed classification."""


class PreflightTransport(Protocol):
    conditional_outcomes: list[str]

    def preflight(self) -> None: ...


TransportFactory = Callable[[S3ArtifactConfig], PreflightTransport]
TtyAckWriter = Callable[[], None]


@dataclass(frozen=True)
class CredentialInput:
    access_key_id: str
    secret_access_key: str


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ArtifactStoreSmokeBlocked(reason)


def _write_tty_ack() -> None:
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open("/dev/tty", flags)
    try:
        remaining = memoryview(TTY_ECHO_DISABLED_ACK)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("tty_ack_write_failed")
            remaining = remaining[written:]
    finally:
        os.close(descriptor)


def _read_hidden_tty_line(
    stream: BinaryIO,
    *,
    termios_api: Any,
    ack_writer: TtyAckWriter,
) -> bytes:
    _require(termios_api is not None, "credential_tty_hidden_unavailable")
    marker = stream.readline(len(TTY_PUBLIC_MARKER) + 1)
    _require(
        hmac.compare_digest(marker, TTY_PUBLIC_MARKER),
        "credential_tty_marker_invalid",
    )
    try:
        descriptor = stream.fileno()
        original = termios_api.tcgetattr(descriptor)
        hidden = list(original)
        hidden[3] &= ~(
            termios_api.ECHO | getattr(termios_api, "ECHONL", 0)
        )
        termios_api.tcsetattr(descriptor, termios_api.TCSADRAIN, hidden)
    except (AttributeError, IndexError, OSError, TypeError, ValueError):
        raise ArtifactStoreSmokeBlocked("credential_tty_hidden_unavailable") from None
    try:
        try:
            ack_writer()
        except OSError:
            raise ArtifactStoreSmokeBlocked("credential_tty_ack_failed") from None
        return stream.readline(MAX_CREDENTIAL_LINE_BYTES + 1)
    finally:
        try:
            termios_api.tcsetattr(descriptor, termios_api.TCSADRAIN, original)
        except (AttributeError, OSError, TypeError, ValueError):
            raise ArtifactStoreSmokeBlocked("credential_tty_restore_failed") from None


def read_credentials(
    stream: BinaryIO,
    *,
    hidden_tty: bool = False,
    termios_api: Any = _termios,
    ack_writer: TtyAckWriter = _write_tty_ack,
) -> CredentialInput:
    isatty = getattr(stream, "isatty", None)
    tty = bool(callable(isatty) and isatty())
    if hidden_tty:
        _require(tty, "credential_tty_hidden_requires_tty")
        raw = _read_hidden_tty_line(
            stream,
            termios_api=termios_api,
            ack_writer=ack_writer,
        )
    else:
        _require(not tty, "credential_stdin_must_be_pipe")
        raw = stream.readline(MAX_CREDENTIAL_LINE_BYTES + 1)
    _require(
        0 < len(raw) <= MAX_CREDENTIAL_LINE_BYTES and raw.endswith(b"\n"),
        "credential_payload_invalid",
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ArtifactStoreSmokeBlocked("credential_payload_invalid") from None
    _require(
        type(payload) is dict
        and set(payload) == {"aws_access_key_id", "aws_secret_access_key"},
        "credential_payload_invalid",
    )
    access_key = payload["aws_access_key_id"]
    secret_key = payload["aws_secret_access_key"]
    _require(
        type(access_key) is str
        and bool(ACCESS_KEY.fullmatch(access_key))
        and type(secret_key) is str
        and 8 <= len(secret_key) <= 4_096
        and all(32 <= ord(character) < 127 for character in secret_key),
        "credential_payload_invalid",
    )
    return CredentialInput(access_key, secret_key)


@contextmanager
def credential_environment(
    credential: CredentialInput,
    environ: MutableMapping[str, str],
) -> Iterator[None]:
    previous = {
        key: (key in environ, environ.get(key, ""))
        for key in SECRET_ENVIRONMENT_KEYS
    }
    try:
        for key in SECRET_ENVIRONMENT_KEYS:
            environ.pop(key, None)
        environ.update({
            "AWS_ACCESS_KEY_ID": credential.access_key_id,
            "AWS_SECRET_ACCESS_KEY": credential.secret_access_key,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_EC2_METADATA_DISABLED": "true",
        })
        yield
    finally:
        for key in SECRET_ENVIRONMENT_KEYS:
            environ.pop(key, None)
        for key, (existed, value) in previous.items():
            if existed:
                environ[key] = value


def _default_factory(config: S3ArtifactConfig) -> PreflightTransport:
    transport = S3GitBundleArtifactTransport.from_boto3(config)
    outcomes: list[str] = []
    inner_client = transport.client

    class BoundRecordingClient:
        def put_object(self, **kwargs: object) -> object:
            try:
                result = inner_client.put_object(**kwargs)
            except Exception as error:
                outcomes.append(
                    "precondition_failed"
                    if _is_precondition_failed(error)
                    else "failed"
                )
                raise
            outcomes.append("created")
            return result

        def get_object(self, **kwargs: object) -> object:
            return inner_client.get_object(**kwargs)

    transport.client = BoundRecordingClient()

    class ObservedTransport:
        conditional_outcomes = outcomes

        def preflight(self) -> None:
            transport.preflight()

    return ObservedTransport()


def run_probe(
    *,
    bucket: str,
    region: str,
    endpoint: str,
    prefix: str,
    credential: CredentialInput,
    factory: TransportFactory = _default_factory,
    environ: MutableMapping[str, str] = os.environ,
) -> dict[str, object]:
    config = S3ArtifactConfig(
        bucket=bucket,
        region=region,
        endpoint_url=endpoint,
        key_prefix=prefix,
    )
    with credential_environment(credential, environ):
        transport = factory(config)
        transport.preflight()
        transport.preflight()
        _require(
            transport.conditional_outcomes == ["created", "precondition_failed"]
            or transport.conditional_outcomes
            == ["precondition_failed", "precondition_failed"],
            "artifact_store_conditional_write_unverified",
        )

    endpoint_host = urlsplit(endpoint).hostname
    _require(bool(endpoint_host), "artifact_store_config_invalid")
    return {
        "schema_version": "1.0.0",
        "result": "S3_ARTIFACT_STORE_SMOKE_PASSED",
        "bucket": bucket,
        "region": region,
        "endpoint_host": endpoint_host,
        "prefix": prefix,
        "conditional_create_and_readback": True,
        "precondition_failed_and_readback": True,
        "credential_source": "ephemeral_stdin",
        "credential_persisted": False,
        "secrets_exposed": False,
        "physical_cross_host_validated": False,
    }


def _reason(error: BaseException) -> str:
    if isinstance(error, ArtifactStoreSmokeBlocked):
        return str(error)
    if isinstance(error, NativeExecutorBlocked):
        candidate = error.args[0] if len(error.args) == 1 else None
        if candidate in SAFE_TRANSPORT_REASONS:
            return str(candidate)
    return "artifact_store_probe_failed"


def main(
    argv: list[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    factory: TransportFactory = _default_factory,
    environ: MutableMapping[str, str] = os.environ,
) -> int:
    parser = argparse.ArgumentParser(
        description="Probe S3 artifact CAS using credentials supplied only on stdin",
    )
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument(
        "--credential-tty-hidden",
        action="store_true",
        help="opt in to marker/ACK handshake with terminal echo disabled",
    )
    args = parser.parse_args(argv)
    input_stream = sys.stdin.buffer if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    credential: CredentialInput | None = None
    try:
        credential = read_credentials(
            input_stream,
            hidden_tty=args.credential_tty_hidden,
        )
        receipt = run_probe(
            bucket=args.bucket,
            region=args.region,
            endpoint=args.endpoint,
            prefix=args.prefix,
            credential=credential,
            factory=factory,
            environ=environ,
        )
        print(json.dumps(receipt, sort_keys=True), file=output_stream)
        return 0
    except Exception as error:
        blocked = {
            "schema_version": "1.0.0",
            "result": "S3_ARTIFACT_STORE_SMOKE_BLOCKED",
            "reason": _reason(error),
            "credential_persisted": False,
            "secrets_exposed": False,
            "physical_cross_host_validated": False,
        }
        print(json.dumps(blocked, sort_keys=True), file=error_stream)
        return 2
    finally:
        credential = None


if __name__ == "__main__":
    raise SystemExit(main())
