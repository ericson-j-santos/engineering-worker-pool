from __future__ import annotations

import io
import json

import pytest

from app.native_one_shot import NativeExecutorBlocked
from scripts.s3_artifact_store_smoke import (
    ArtifactStoreSmokeBlocked,
    CredentialInput,
    TTY_ECHO_DISABLED_ACK,
    TTY_PUBLIC_MARKER,
    main,
    read_credentials,
    run_probe,
)


ACCESS_KEY = "nsk_live_unit_test_access"
SECRET_KEY = "unit-test-secret-material"
ENDPOINT = "https://branch.storage.c-6.us-east-2.aws.neon.tech"


def credential_stream(
    access_key: str = ACCESS_KEY,
    secret_key: str = SECRET_KEY,
) -> io.BytesIO:
    return io.BytesIO(json.dumps({
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
    }).encode("utf-8") + b"\n")


def test_read_credentials_accepts_one_bounded_stdin_line() -> None:
    credential = read_credentials(credential_stream())
    assert credential == CredentialInput(ACCESS_KEY, SECRET_KEY)


class FakeTermios:
    ECHO = 0b0001
    ECHONL = 0b0010
    TCSADRAIN = 1

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.attributes = [0, 0, 0, self.ECHO | self.ECHONL, 0, 0, []]

    def tcgetattr(self, descriptor: int) -> list[object]:
        assert descriptor == 37
        self.events.append("get")
        return list(self.attributes)

    def tcsetattr(
        self, descriptor: int, when: int, attributes: list[object]
    ) -> None:
        assert descriptor == 37
        assert when == self.TCSADRAIN
        self.attributes = list(attributes)
        self.events.append(
            "echo_on" if int(self.attributes[3]) & self.ECHO else "echo_off"
        )


class FakeTty:
    def __init__(
        self,
        lines: list[bytes],
        termios_api: FakeTermios,
        events: list[str],
    ) -> None:
        self.lines = list(lines)
        self.termios_api = termios_api
        self.events = events

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 37

    def readline(self, _limit: int = -1) -> bytes:
        line = self.lines.pop(0) if self.lines else b""
        if line == TTY_PUBLIC_MARKER:
            self.events.append("read_marker_echo_on")
            assert int(self.termios_api.attributes[3]) & self.termios_api.ECHO
        else:
            self.events.append("read_secret_echo_off")
            assert not int(self.termios_api.attributes[3]) & self.termios_api.ECHO
        return line


def test_hidden_tty_handshake_disables_echo_before_secret_and_restores() -> None:
    events: list[str] = []
    termios_api = FakeTermios(events)
    original = list(termios_api.attributes)
    stream = FakeTty(
        [TTY_PUBLIC_MARKER, credential_stream().read()],
        termios_api,
        events,
    )
    ack_values: list[bytes] = []

    def ack_writer() -> None:
        events.append("ack")
        assert not int(termios_api.attributes[3]) & termios_api.ECHO
        ack_values.append(TTY_ECHO_DISABLED_ACK)

    credential = read_credentials(
        stream,
        hidden_tty=True,
        termios_api=termios_api,
        ack_writer=ack_writer,
    )

    assert credential == CredentialInput(ACCESS_KEY, SECRET_KEY)
    assert events == [
        "read_marker_echo_on",
        "get",
        "echo_off",
        "ack",
        "read_secret_echo_off",
        "echo_on",
    ]
    assert termios_api.attributes == original
    assert ack_values == [TTY_ECHO_DISABLED_ACK]
    assert ACCESS_KEY.encode() not in ack_values[0]
    assert SECRET_KEY.encode() not in ack_values[0]


def test_hidden_tty_restores_echo_before_reporting_invalid_secret() -> None:
    events: list[str] = []
    termios_api = FakeTermios(events)
    original = list(termios_api.attributes)
    stream = FakeTty(
        [TTY_PUBLIC_MARKER, b"not-json\n"],
        termios_api,
        events,
    )

    with pytest.raises(ArtifactStoreSmokeBlocked, match="credential_payload_invalid"):
        read_credentials(
            stream,
            hidden_tty=True,
            termios_api=termios_api,
            ack_writer=lambda: None,
        )

    assert termios_api.attributes == original
    assert events[-1] == "echo_on"


def test_tty_requires_explicit_hidden_mode_and_hidden_mode_requires_tty() -> None:
    events: list[str] = []
    termios_api = FakeTermios(events)
    tty = FakeTty([TTY_PUBLIC_MARKER], termios_api, events)
    with pytest.raises(ArtifactStoreSmokeBlocked, match="credential_stdin_must_be_pipe"):
        read_credentials(tty)
    with pytest.raises(
        ArtifactStoreSmokeBlocked,
        match="credential_tty_hidden_requires_tty",
    ):
        read_credentials(credential_stream(), hidden_tty=True)


@pytest.mark.parametrize("payload", [
    b"",
    b"{}\n",
    b'{"aws_access_key_id":"x","aws_secret_access_key":"short"}\n',
    b'{"aws_access_key_id":"valid_key","aws_secret_access_key":"valid-secret","extra":1}\n',
    b'{"aws_access_key_id":"valid_key","aws_secret_access_key":"line\\nfeed"}\n',
    b"not-json\n",
    b'{"aws_access_key_id":"valid_key","aws_secret_access_key":"valid-secret"}',
])
def test_read_credentials_rejects_malformed_or_unbounded_input(payload: bytes) -> None:
    with pytest.raises(ArtifactStoreSmokeBlocked, match="credential_payload_invalid"):
        read_credentials(io.BytesIO(payload))


def test_probe_uses_ephemeral_environment_and_runs_both_preflights() -> None:
    original = {
        "AWS_ACCESS_KEY_ID": "previous-access",
        "AWS_SESSION_TOKEN": "previous-session",
        "UNRELATED": "preserved",
    }
    observed: list[tuple[str, str, str | None, str | None]] = []

    class Transport:
        conditional_outcomes: list[str] = []

        def preflight(self) -> None:
            observed.append((
                original["AWS_ACCESS_KEY_ID"],
                original["AWS_SECRET_ACCESS_KEY"],
                original.get("AWS_SESSION_TOKEN"),
                original.get("AWS_EC2_METADATA_DISABLED"),
            ))
            self.conditional_outcomes.append(
                "created" if len(self.conditional_outcomes) == 0
                else "precondition_failed"
            )

    def factory(config):
        assert config.bucket == "engineering-continuity-artifacts"
        assert config.region == "us-east-2"
        assert config.endpoint_url == ENDPOINT
        assert config.key_prefix == "engineering-worker-pool/checkpoints"
        return Transport()

    receipt = run_probe(
        bucket="engineering-continuity-artifacts",
        region="us-east-2",
        endpoint=ENDPOINT,
        prefix="engineering-worker-pool/checkpoints",
        credential=CredentialInput(ACCESS_KEY, SECRET_KEY),
        factory=factory,
        environ=original,
    )

    assert observed == [
        (ACCESS_KEY, SECRET_KEY, None, "true"),
        (ACCESS_KEY, SECRET_KEY, None, "true"),
    ]
    assert original == {
        "AWS_ACCESS_KEY_ID": "previous-access",
        "AWS_SESSION_TOKEN": "previous-session",
        "UNRELATED": "preserved",
    }
    assert receipt["conditional_create_and_readback"] is True
    assert receipt["precondition_failed_and_readback"] is True
    assert receipt["credential_persisted"] is False
    assert ACCESS_KEY not in json.dumps(receipt)
    assert SECRET_KEY not in json.dumps(receipt)


def test_main_never_prints_secret_when_transport_failure_contains_it() -> None:
    def failing_factory(_config):
        raise RuntimeError(f"provider leaked {ACCESS_KEY} {SECRET_KEY}")

    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        [
            "--bucket", "engineering-continuity-artifacts",
            "--region", "us-east-2",
            "--endpoint", ENDPOINT,
            "--prefix", "engineering-worker-pool/checkpoints",
        ],
        stdin=credential_stream(),
        stdout=stdout,
        stderr=stderr,
        factory=failing_factory,
        environ={},
    )

    assert code == 2
    assert stdout.getvalue() == ""
    failure = stderr.getvalue()
    assert ACCESS_KEY not in failure
    assert SECRET_KEY not in failure
    assert json.loads(failure)["reason"] == "artifact_store_probe_failed"


def test_main_emits_only_allowlisted_transport_classification() -> None:
    def unavailable_factory(_config):
        raise NativeExecutorBlocked("artifact_store_unavailable")

    stderr = io.StringIO()
    code = main(
        [
            "--bucket", "engineering-continuity-artifacts",
            "--region", "us-east-2",
            "--endpoint", ENDPOINT,
            "--prefix", "engineering-worker-pool/checkpoints",
        ],
        stdin=credential_stream(),
        stdout=io.StringIO(),
        stderr=stderr,
        factory=unavailable_factory,
        environ={},
    )

    assert code == 2
    assert json.loads(stderr.getvalue())["reason"] == "artifact_store_unavailable"


def test_probe_rejects_backend_that_ignores_conditional_write() -> None:
    class Transport:
        conditional_outcomes: list[str] = []

        def preflight(self) -> None:
            self.conditional_outcomes.append("created")

    with pytest.raises(
        ArtifactStoreSmokeBlocked,
        match="artifact_store_conditional_write_unverified",
    ):
        run_probe(
            bucket="engineering-continuity-artifacts",
            region="us-east-2",
            endpoint=ENDPOINT,
            prefix="engineering-worker-pool/checkpoints",
            credential=CredentialInput(ACCESS_KEY, SECRET_KEY),
            factory=lambda _config: Transport(),
            environ={},
        )


def test_invalid_route_fails_before_factory_or_secret_output() -> None:
    called = False

    def factory(_config):
        nonlocal called
        called = True
        raise AssertionError("must not run")

    stderr = io.StringIO()
    code = main(
        [
            "--bucket", "engineering-continuity-artifacts",
            "--region", "us-east-2",
            "--endpoint", "http://user:password@example.invalid/?query=1",
            "--prefix", "engineering-worker-pool/checkpoints",
        ],
        stdin=credential_stream(),
        stdout=io.StringIO(),
        stderr=stderr,
        factory=factory,
        environ={},
    )

    assert code == 2
    assert called is False
    output = stderr.getvalue()
    assert ACCESS_KEY not in output
    assert SECRET_KEY not in output
    assert json.loads(output)["reason"] == "artifact_store_config_invalid"
