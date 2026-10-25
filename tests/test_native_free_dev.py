"""Positive, negative, replay and source-order checks for native free DEV.

No database, network, Docker, paid API or external provider is contacted.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

from app.native_free_dev import (
    ARTIFACT, PROFILE, NativeFreeBlocked, validate_native_free_dev,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux",
                                reason="POSIX native-free contract runs in Linux CI")


def private(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def settings(tmp_path):
    dsn = private(tmp_path / "worker.conn", (
        "postgresql://dev_worker:fictional@127.0.0.1:5432/ewp_dev_issues"
        "?sslmode=verify-full&sslrootcert=system"
        "&channel_binding=require&connect_timeout=8"))
    api = private(tmp_path / "worker.token", "fake-worker-only")
    reconciler = private(tmp_path / "reconcile.token", "fake-reconciler-only")
    return {
        "CODEX_WORKER_POOL_AUTHORITY_PROFILE": PROFILE,
        "CODEX_WORKER_POOL_DEPLOYMENT_ENV": "dev",
        "CODEX_WORKER_POOL_BACKEND": "postgres",
        "CODEX_WORKER_POOL_ARTIFACT_TRANSPORT": ARTIFACT,
        "CODEX_WORKER_POOL_DB_SCHEMA": "ewp_dev_issue8",
        "CODEX_WORKER_POOL_AUTHORITY_ID": "51f8d7cb-133a-4e57-9cd9-d22b2e224aff",
        "CODEX_WORKER_POOL_DATABASE_URL_FILE": str(dsn),
        "CODEX_WORKER_POOL_API_TOKEN_FILE": str(api),
        "CODEX_WORKER_POOL_RECONCILIATION_TOKEN_FILE": str(reconciler),
        "CODEX_WORKER_POOL_FENCED_CUTOVER_ATTESTED": "0",
    }


def test_replay_preflight_is_deterministic_and_has_no_external_effects(settings):
    first = validate_native_free_dev(settings)
    second = validate_native_free_dev(settings)
    assert first == second
    assert first["result"] == "NATIVE_FREE_DEV_PREFLIGHT_ONLY"
    assert first["database_connected"] is False
    assert first["cas_readback_proved"] is False
    assert first["ready_for_pr"] is False
    assert not any(value == settings["CODEX_WORKER_POOL_DATABASE_URL_FILE"]
                   for value in first.values())


@pytest.mark.parametrize(("field", "value", "reason"), [
    ("CODEX_WORKER_POOL_AUTHORITY_PROFILE", "legacy", "profile_invalid"),
    ("CODEX_WORKER_POOL_DEPLOYMENT_ENV", "prod", "profile_invalid"),
    ("CODEX_WORKER_POOL_BACKEND", "sqlite", "postgres_required"),
    ("CODEX_WORKER_POOL_BACKEND", "unknown", "postgres_required"),
    ("CODEX_WORKER_POOL_ARTIFACT_TRANSPORT", "s3", "github_cas_required"),
    ("CODEX_WORKER_POOL_FENCED_CUTOVER_ATTESTED", "1", "cutover_not_approved"),
    ("CODEX_WORKER_POOL_DB_SCHEMA", "public", "schema_invalid"),
    ("CODEX_WORKER_POOL_DB_SCHEMA", "worker_pool", "schema_invalid"),
    ("CODEX_WORKER_POOL_AUTHORITY_ID", "invalid", "authority_id_invalid"),
    ("EWP_RUNTIME_CONFIG_JSON", "should-not-be-present", "external_credential_profile_denied"),
    ("CODEX_ARTIFACT_S3_ENDPOINT", "https://not-used.invalid", "external_credential_profile_denied"),
])
def test_wrong_modes_fail_closed(settings, field, value, reason):
    settings[field] = value
    with pytest.raises(NativeFreeBlocked, match="native_free_" + reason):
        validate_native_free_dev(settings)


@pytest.mark.parametrize(("dsn", "reason"), [
    ("postgresql://dev:fake@ep-cloud.invalid:5432/ewp_dev"
     "?sslmode=verify-full&sslrootcert=system&channel_binding=require&connect_timeout=8",
     "remote_authority_denied"),
    ("postgresql://dev:fake@127.0.0.1:5432/ewp_dev"
     "?sslmode=require&sslrootcert=system&channel_binding=require&connect_timeout=8",
     "tls_identity_invalid"),
    ("postgresql://dev:fake@127.0.0.1:5432/ewp_dev"
     "?sslmode=verify-full&sslrootcert=system&channel_binding=disable&connect_timeout=8",
     "tls_identity_invalid"),
    ("postgresql://dev:fake@127.0.0.1:5432/ewp_dev"
     "?sslmode=verify-full&sslrootcert=system&channel_binding=require&connect_timeout=8"
     "&host=ep-cloud.invalid", "tls_identity_invalid"),
])
def test_non_local_or_weaker_tls_never_enters_real_database(settings, dsn, reason):
    Path(settings["CODEX_WORKER_POOL_DATABASE_URL_FILE"]).write_text(
        dsn, encoding="utf-8")
    with pytest.raises(NativeFreeBlocked, match="native_free_" + reason):
        validate_native_free_dev(settings)


def test_readable_to_group_credentials_are_rejected(settings):
    secret = Path(settings["CODEX_WORKER_POOL_DATABASE_URL_FILE"])
    secret.chmod(0o640)
    with pytest.raises(NativeFreeBlocked, match="native_free_connection_file_unverified"):
        validate_native_free_dev(settings)


def test_symlink_and_same_identity_file_are_rejected(settings, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(Path(settings["CODEX_WORKER_POOL_DATABASE_URL_FILE"]))
    settings["CODEX_WORKER_POOL_DATABASE_URL_FILE"] = str(alias)
    with pytest.raises(NativeFreeBlocked, match="native_free_connection_file_unverified"):
        validate_native_free_dev(settings)


def test_token_roles_must_be_distinct_inodes(settings):
    settings["CODEX_WORKER_POOL_RECONCILIATION_TOKEN_FILE"] = (
        settings["CODEX_WORKER_POOL_API_TOKEN_FILE"])
    with pytest.raises(NativeFreeBlocked, match="native_free_token_roles_not_separated"):
        validate_native_free_dev(settings)


def test_no_unconditional_profile_or_cloud_fallback_at_service_startup():
    source = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert 'if AUTHORITY_PROFILE:' in source
    assert 'validate_native_free_dev(os.environ)' in source
    assert source.index('if AUTHORITY_PROFILE:') < source.index('if DATABASE_BACKEND == "sqlite":')
