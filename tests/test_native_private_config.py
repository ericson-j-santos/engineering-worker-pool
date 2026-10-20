from __future__ import annotations
import json
import os
from pathlib import Path
import pytest
from scripts.native_private_config import (
    PrivateConfigurationError, identity_expectation, read_private, store_configuration, validate_url,
)

URL = "postgresql://neondb_owner:fixture@ep-fixture.neon.tech/neondb?sslmode=verify-full&sslrootcert=system"

def test_private_store_readback_and_replay(tmp_path):
    directory = tmp_path / "private"
    first = store_configuration("admin", {"database_url": URL}, directory)
    assert first["replayed"] is False
    assert read_private(directory / "admin.url").decode().strip() == URL
    assert (directory / "admin.url").stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert store_configuration("admin", {"database_url": URL}, directory)["replayed"] is True
    with pytest.raises(PrivateConfigurationError, match="conflict"):
        store_configuration("admin", {"database_url": URL.replace("fixture@", "changed@")}, directory)
    assert read_private(directory / "admin.url").decode().strip() == URL

@pytest.mark.parametrize("mutation", [
    lambda u: u.replace("verify-full", "require"),
    lambda u: u.replace("sslrootcert=system", "sslrootcert=invalid"),
    lambda u: u + "&host=attacker.invalid",
    lambda u: u + "&sslmode=require",
    lambda u: u.replace("ep-fixture.neon.tech", "attacker.invalid"),
    lambda u: u.replace("postgresql:", "http:"),
    lambda u: u.replace("/neondb", "/product"),
    lambda u: u + "#fragment",
])
def test_unsafe_database_configuration_rejected(mutation):
    with pytest.raises(PrivateConfigurationError):
        validate_url(mutation(URL))

def test_private_file_link_and_permission_controls(tmp_path):
    directory = tmp_path / "private"
    store_configuration("admin", {"database_url": URL}, directory)
    source = directory / "admin.url"
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises((OSError, PrivateConfigurationError)):
        read_private(link)
    hard = tmp_path / "hard"
    os.link(source, hard)
    with pytest.raises(PrivateConfigurationError):
        read_private(source)
    hard.unlink()
    source.chmod(0o644)
    with pytest.raises(PrivateConfigurationError):
        read_private(source)

def test_private_directory_cannot_be_redirected(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    redirected = tmp_path / "redirected"
    redirected.symlink_to(target, target_is_directory=True)
    with pytest.raises(PrivateConfigurationError):
        store_configuration("admin", {"database_url": URL}, redirected)
    assert list(target.iterdir()) == []

def test_private_schema_rejects_extra_keys(tmp_path):
    with pytest.raises(PrivateConfigurationError):
        store_configuration("admin", {"database_url": URL, "extra": "not-allowed"}, tmp_path / "private")
    assert not (tmp_path / "private").exists()

@pytest.mark.parametrize("stage", ["github", "broker"])
def test_identity_http_failure_reports_only_stage_and_status(monkeypatch, stage):
    import io
    import urllib.error
    import urllib.request
    from scripts.native_private_config import _json_request

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 403, "untrusted private response",
                {}, io.BytesIO(b"private response body must remain unread"))

    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(PrivateConfigurationError) as captured:
        _json_request(urllib.request.Request("https://example.invalid/identity"),
                      maximum=1024, stage=stage)
    assert str(captured.value) == "identity_" + stage + "_http_403"


def test_identity_expectation_is_closed_and_profile_specific(monkeypatch):
    monkeypatch.delenv("EWP_IDENTITY_PROFILE", raising=False)
    default = identity_expectation()
    assert default["repository"] == "ericson-j-santos/engineering-worker-pool"
    assert default["repository_id"] == "1384155635"
    assert default["event_name"] == "push"

    monkeypatch.setenv("EWP_IDENTITY_PROFILE", "reqsys-handoff-v1")
    reqsys = identity_expectation()
    assert reqsys["repository"] == "ericson-j-santos/reqsys-v2-enterprise-real"
    assert reqsys["repository_id"] == "1228361750"
    assert reqsys["actor"] == "github-actions[bot]"
    assert reqsys["event_name"] == "workflow_dispatch"
    assert reqsys["ref"] == "refs/heads/main"
    assert reqsys["workflow_ref"].endswith(
        "/.github/workflows/codex-worker-pool-handoff.yml@refs/heads/main"
    )

    monkeypatch.setenv("EWP_IDENTITY_PROFILE", "unknown")
    with pytest.raises(PrivateConfigurationError, match="identity_profile_invalid"):
        identity_expectation()
