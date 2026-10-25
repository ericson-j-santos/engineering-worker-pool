"""Fail-closed, opt-in native DEV authority gate.

This is a configuration preflight, NOT a database startup, cutover, CAS write,
model dispatch, or proof that another authorized host can reach the service.
It has no HTTP or subprocess operation. The normal legacy backend is untouched.
"""
from __future__ import annotations

import os
import re
import stat
import uuid
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qsl, urlsplit

from app.postgres_authority import read_private_connection_file

PROFILE = "native-free-dev-v1"
ARTIFACT = "github-git-cas-v1"
DEV_SCHEMA = re.compile(r"^ewp_dev_[a-z0-9_]{1,35}$")
DBNAME = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
CLOUD_SETTINGS = (
    "EWP_RUNTIME_CONFIG_JSON",
    "EWP_ALLOWED_SHAS",
    "EWP_IDENTITY_PROFILE",
    "CODEX_ARTIFACT_S3_ENDPOINT",
    "CODEX_ARTIFACT_S3_BUCKET",
    "CODEX_ARTIFACT_S3_PREFIX",
    "CODEX_ARTIFACT_S3_REGION",
)


class NativeFreeBlocked(RuntimeError):
    """Constant reason codes only; never wrap connection strings or tokens."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise NativeFreeBlocked(reason)


def _private_file(raw: str, reason: str) -> Path:
    require(os.name == "posix" and isinstance(raw, str) and raw.startswith("/"),
            reason)
    path = Path(raw)
    require(path.is_absolute() and not path.is_symlink() and not any(
        parent.is_symlink() for parent in path.parents), reason)
    try:
        info = path.lstat()
    except OSError:
        raise NativeFreeBlocked(reason) from None
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) & 0o077 == 0
            and 0 < info.st_size <= 8192, reason)
    return path


def validate_native_free_dev(env: Mapping[str, str]) -> dict[str, object]:
    """Prove only preconditions; real DB/TLS, shared lease and CAS gates follow.

    A single local PostgreSQL authority can serve multiple authenticated HTTPS
    workers. A second independent local DB is NEVER an automatic failover.
    """
    require(env.get("CODEX_WORKER_POOL_AUTHORITY_PROFILE") == PROFILE
            and env.get("CODEX_WORKER_POOL_DEPLOYMENT_ENV") == "dev",
            "native_free_profile_invalid")
    require(os.name == "posix", "native_free_posix_required")
    require(env.get("CODEX_WORKER_POOL_BACKEND") == "postgres",
            "native_free_postgres_required")
    require(env.get("CODEX_WORKER_POOL_ARTIFACT_TRANSPORT") == ARTIFACT,
            "native_free_github_cas_required")
    require(env.get("CODEX_WORKER_POOL_FENCED_CUTOVER_ATTESTED", "0") == "0",
            "native_free_cutover_not_approved")
    require(not any(env.get(key) for key in CLOUD_SETTINGS),
            "native_free_external_credential_profile_denied")
    schema = env.get("CODEX_WORKER_POOL_DB_SCHEMA", "")
    require(type(schema) is str and DEV_SCHEMA.fullmatch(schema) is not None,
            "native_free_schema_invalid")
    try:
        aid = env.get("CODEX_WORKER_POOL_AUTHORITY_ID", "")
        require(type(aid) is str and str(uuid.UUID(aid)) == aid,
                "native_free_authority_id_invalid")
    except (ValueError, TypeError, AttributeError):
        raise NativeFreeBlocked("native_free_authority_id_invalid") from None
    dsn_file = _private_file(
        env.get("CODEX_WORKER_POOL_DATABASE_URL_FILE", ""),
        "native_free_connection_file_unverified")
    api_file = _private_file(
        env.get("CODEX_WORKER_POOL_API_TOKEN_FILE", ""),
        "native_free_worker_identity_unverified")
    reconcile_file = _private_file(
        env.get("CODEX_WORKER_POOL_RECONCILIATION_TOKEN_FILE", ""),
        "native_free_reconciliation_identity_unverified")
    require((api_file.stat().st_dev, api_file.stat().st_ino) !=
            (reconcile_file.stat().st_dev, reconcile_file.stat().st_ino),
            "native_free_token_roles_not_separated")
    try:
        raw = read_private_connection_file(dsn_file)
        require(type(raw) is str and raw.startswith("postgresql://"),
                "native_free_connection_invalid")
        url = urlsplit(raw)
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
        options = dict(pairs)
        require(len(options) == len(pairs), "native_free_connection_invalid")
        require(url.scheme == "postgresql" and url.hostname == "127.0.0.1"
                and url.port is not None and 1 <= url.port <= 65535
                and not url.fragment and url.username is not None
                and bool(url.password) and url.path.startswith("/")
                and DBNAME.fullmatch(url.path[1:]) is not None,
                "native_free_remote_authority_denied")
        require(set(options) == {
            "sslmode", "sslrootcert", "channel_binding", "connect_timeout"
        } and options["sslmode"] == "verify-full"
                and options["channel_binding"] == "require"
                and 1 <= int(options["connect_timeout"]) <= 30,
                "native_free_tls_identity_invalid")
        ca = options["sslrootcert"]
        if ca != "system":
            cert = Path(ca)
            require(cert.is_absolute() and cert.is_file()
                    and not cert.is_symlink(), "native_free_ca_unverified")
    except NativeFreeBlocked:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise NativeFreeBlocked("native_free_connection_unverified") from None

    return {
        "result": "NATIVE_FREE_DEV_PREFLIGHT_ONLY",
        "profile": PROFILE,
        "storage_backend": "postgres",
        "connection_host_class": "loopback",
        "tls_configuration": "verify-full-and-channel-binding",
        "artifact_provider_declared": ARTIFACT,
        "fenced_cutover_enabled": False,
        "external_storage_contacted": False,
        "database_connected": False,
        "shared_authority_proved": False,
        "cas_readback_proved": False,
        "ready_for_pr": False,
    }
