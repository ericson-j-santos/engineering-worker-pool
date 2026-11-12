"""Protected fixed-state DEV snapshot for DESKTOP-PDQK954.

Copies only canonical service/worker/version/Startup content and a consistent
SQLite online backup. All content is DPAPI encrypted under the local runner
user, never sent to logs or GitHub. Restore is validated IN MEMORY only.
It never modifies the live Orchestrator, service, Startup, or database.
"""
from __future__ import annotations

import argparse
import ctypes
from contextlib import closing
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import tempfile
from typing import Any, Callable
from urllib.parse import quote

from desktop_orchestrator_isolated_rollback import (
    EXPECTED_INSTALLED_SHA, HOST, REPO, RUNNER, RUNTIME_ROOT,
    RollbackError, _guard, code_manifest,
)

CONFIRM = "VERIFY-DESKTOP-ORCHESTRATOR-PROTECTED-STATE-DEV"
SOURCE_CODE_BACKUP = (
    "verified-code-33f312d0ac4c-38297556070-1"
)
# The historical scope is retained for the two legacy owner-only workflows.
LEGACY_WORKFLOW = "Desktop Protected Orchestrator State Recovery Gate"
SEQUENTIAL_WORKFLOW = "Desktop Orchestrator Sequential Recovery Readback DEV"
CIPHER_FILES = {
    "service": "service-config.json",
    "worker": "worker-config.json",
    "version": "runtime-version.json",
    "startup": "startup-wrapper.cmd",
    "sqlite": "orchestrator.db",
}
MAX_CONFIG_BYTES = 64 * 1024
MAX_SQLITE_BYTES = 64 * 1024 * 1024
RUN_RE = re.compile(r"[1-9][0-9]{1,17}\Z")
ATTEMPT_RE = re.compile(r"[1-9][0-9]{0,3}\Z")


class StateError(ValueError):
    pass


# These already originate from the fixed-path/code-manifest guard. They do not
# contain file names, arbitrary exception messages, or source file contents.
# Keep this list identical to the readback workflow's exact literal allowlist.
SAFE_ROLLBACK_BLOCKERS = frozenset({
    "FIXED_PATH_MISSING",
    "FIXED_PATH_REDIRECTED",
    "FIXED_PATH_INVALID",
    "CODE_FILE_SIZE_LIMIT",
    "CODE_FILE_READ_FAILED",
    "CODE_CHANGED_DURING_READ",
    "CODE_ENTRY_LIMIT",
    "CODE_PATH_REDIRECTED",
    "CODE_FILE_TYPE_INVALID",
    "CODE_FILE_NOT_ALLOWLISTED",
    "CODE_VOLUME_LIMIT",
    "CODE_CONTRACT_INCOMPLETE",
})


def safe_blocker_code(failure: BaseException) -> str:
    """Preserve an approved typed failure, never report raw exception text."""
    if isinstance(failure, StateError):
        return str(failure)  # Existing StateError contract; workflow filters it.
    if type(failure) is RollbackError and str(failure) in SAFE_ROLLBACK_BLOCKERS:
        return str(failure)
    return "UNEXPECTED_FILE_ERROR"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def dpapi(data: bytes, *, encrypt: bool) -> bytes:
    """Windows user-scope DPAPI; UI disabled; never machine scope or a key file."""
    if os.name != "nt":
        raise StateError("DPAPI_WINDOWS_REQUIRED")
    if not data or len(data) > MAX_SQLITE_BYTES + 8192:
        raise StateError("DPAPI_INPUT_INVALID")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    input_buffer = ctypes.create_string_buffer(data)
    input_blob = _Blob(len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output_blob = _Blob()
    if encrypt:
        proc = crypt.CryptProtectData
        proc.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        proc.restype = wintypes.BOOL
        ok = proc(ctypes.byref(input_blob), None, None, None, None, 0x1, ctypes.byref(output_blob))
    else:
        proc = crypt.CryptUnprotectData
        proc.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        proc.restype = wintypes.BOOL
        ok = proc(ctypes.byref(input_blob), None, None, None, None, 0x1, ctypes.byref(output_blob))
    if not ok or not output_blob.pbData or output_blob.cbData == 0:
        raise StateError("DPAPI_PROTECT_FAILED" if encrypt else "DPAPI_UNPROTECT_FAILED")
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def _bytes(path: Path, maximum: int = MAX_CONFIG_BYTES) -> bytes:
    before = _guard(path, directory=False)
    if before.st_size <= 0 or before.st_size > maximum:
        raise StateError("FIXED_FILE_SIZE_INVALID")
    try:
        with path.open("rb") as stream:
            data = stream.read(maximum + 1)
    except OSError:
        raise StateError("FIXED_FILE_READ_FAILED") from None
    after = _guard(path, directory=False)
    if len(data) != before.st_size or (
        before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size
    ) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
        raise StateError("FIXED_FILE_CHANGED")
    return data


def _json(raw: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise StateError("CONFIG_JSON_INVALID") from None
    if not isinstance(obj, dict):
        raise StateError("CONFIG_JSON_INVALID")
    return obj


def _startup_path(appdata: Path) -> Path:
    if not appdata.is_absolute():
        raise StateError("APPDATA_INVALID")
    return (appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" /
            "Startup" / "ReqSys-Orchestrator-desktop-pdqk954.cmd")


def _fixed_inputs(root: Path, *, appdata: Path) -> dict[str, bytes]:
    _guard(root, directory=True)
    files = {
        "service": root / "service-config.json",
        "worker": root / "worker-config.json",
        "version": root / "runtime-version.json",
        "startup": _startup_path(appdata),
    }
    raw = {name: _bytes(path) for name, path in files.items()}
    _check_contract(root, raw)
    return raw


def _check_contract(root: Path, raw: dict[str, bytes]) -> None:
    service, worker, version = (_json(raw[name]) for name in ("service", "worker", "version"))
    if (service.get("mode") != "control-plane-worker" or
        service.get("port") != 8787 or
        Path(str(service.get("install_root") or "")).resolve() != root.resolve() or
        Path(str(service.get("worker_config") or "")).resolve() != (root / "worker-config.json").resolve() or
        Path(str(service.get("db_path") or "")).resolve() != (root / "data/orchestrator.db").resolve() or
        Path(str(service.get("backup_path") or "")).resolve() != (root / "data/orchestrator.backup.db").resolve()):
        raise StateError("SUPERVISOR_CONFIG_MISMATCH")
    if (worker.get("worker_id") != "desktop-pdqk954" or
        str(worker.get("endpoint") or "").rstrip("/") != "http://127.0.0.1:8787" or
        version.get("source_sha") != EXPECTED_INSTALLED_SHA):
        raise StateError("WORKER_OR_VERSION_MISMATCH")
    try:
        wrapper = raw["startup"].decode("utf-8-sig")
    except UnicodeError:
        raise StateError("STARTUP_WRAPPER_INVALID") from None
    needle = f'--config "{root / "service-config.json"}"'
    if (wrapper.lower().count(needle.lower()) != 1 or
        len(re.findall(r"(?im)^\s*\"[^\"]+\"\s+-m\s+scripts\.service_supervisor\s+", wrapper)) != 1):
        raise StateError("STARTUP_WRAPPER_MISMATCH")


def _sqlite_consistent_bytes(db: Path) -> bytes:
    _guard(db, directory=False)
    if db.stat().st_size <= 0 or db.stat().st_size > MAX_SQLITE_BYTES:
        raise StateError("SQLITE_SIZE_INVALID")
    try:
        uri = "file:" + quote(db.as_posix(), safe="/:") + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as source:
            with closing(sqlite3.connect(":memory:")) as target:
                source.execute("PRAGMA busy_timeout=5000")
                source.backup(target, pages=128, sleep=0.02)
                serialized = bytearray(target.serialize())
        if (len(serialized) > MAX_SQLITE_BYTES or
            len(serialized) < 100 or bytes(serialized[:16]) != b"SQLite format 3\x00"):
            raise StateError("SQLITE_IMAGE_INVALID")
        # SQLite's online backup may retain WAL header flags even with its pages
        # checkpointed in memory. Normalize ONLY the fixed read/write header bytes.
        if serialized[18:20] == b"\x02\x02":
            serialized[18:20] = b"\x01\x01"
        elif serialized[18:20] != b"\x01\x01":
            raise StateError("SQLITE_IMAGE_FLAGS_INVALID")
        result = bytes(serialized)
        _verify_db(result)
        return result
    except (sqlite3.Error, OverflowError):
        raise StateError("SQLITE_SNAPSHOT_FAILED") from None


def _verify_db(raw: bytes) -> int:
    if len(raw) < 100 or raw[:16] != b"SQLite format 3\x00":
        raise StateError("SQLITE_RESTORE_FORMAT_INVALID")
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(raw)
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise StateError("SQLITE_RESTORE_INTEGRITY_FAILED")
            return int(connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0])
        finally:
            connection.close()
    except sqlite3.Error:
        raise StateError("SQLITE_RESTORE_FAILED") from None


def _cipher_filename(name: str) -> str:
    if name not in CIPHER_FILES:
        raise StateError("SNAPSHOT_ENTRY_INVALID")
    return name + ".dpapi"


def _manifest_digest(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _read_snapshot(snapshot: Path, *, decrypt_fn: Callable[[bytes], bytes]) -> tuple[dict[str, bytes], int, dict[str, Any]]:
    _guard(snapshot, directory=True)
    manifest = _json(_bytes(snapshot / "snapshot.json", maximum=64 * 1024))
    if (manifest.get("schema") != 1 or
        manifest.get("source_sha") != EXPECTED_INSTALLED_SHA or
        set(manifest.get("entries", {})) != set(CIPHER_FILES)):
        raise StateError("SNAPSHOT_MANIFEST_INVALID")
    restored: dict[str, bytes] = {}
    for name in CIPHER_FILES:
        encoded = _bytes(snapshot / _cipher_filename(name), maximum=MAX_SQLITE_BYTES + 8192)
        envelope = manifest["entries"].get(name)
        if not isinstance(envelope, dict) or hashlib.sha256(encoded).hexdigest() != envelope.get("cipher_sha256"):
            raise StateError("SNAPSHOT_CIPHER_MISMATCH")
        try:
            original = decrypt_fn(encoded)
        except (OSError, ValueError):
            raise StateError("SNAPSHOT_UNPROTECT_FAILED") from None
        if (hashlib.sha256(original).hexdigest() != envelope.get("plain_sha256") or
            len(original) != envelope.get("size")):
            raise StateError("SNAPSHOT_PLAINTEXT_MISMATCH")
        restored[name] = original
    return restored, _verify_db(restored["sqlite"]), manifest


def code_snapshot_name(run_id: str, attempt: str, *, workflow: str) -> str:
    """Resolve only a fixed historical or the exact current-run code snapshot.

    Never choose a snapshot by mtime/glob or fall back to old data in the
    sequential workflow: both would permit accidentally restoring stale code.
    """
    if not RUN_RE.fullmatch(run_id) or not ATTEMPT_RE.fullmatch(attempt):
        raise StateError("CODE_SNAPSHOT_EXECUTION_ID_INVALID")
    if workflow == SEQUENTIAL_WORKFLOW:
        return f"verified-code-{EXPECTED_INSTALLED_SHA[:12]}-{run_id}-{attempt}"
    if workflow == LEGACY_WORKFLOW:
        return SOURCE_CODE_BACKUP
    raise StateError("CODE_SNAPSHOT_WORKFLOW_NOT_ADMITTED")


def _code_snapshot_ok(root: Path, backup_root: Path, *, snapshot_name: str = SOURCE_CODE_BACKUP) -> str:
    """Validate snapshot origin and content; return canonical digest, no secrets."""
    if not re.fullmatch(r"verified-code-[0-9a-f]{12}-[1-9][0-9]{1,17}-[1-9][0-9]{0,3}", snapshot_name):
        raise StateError("CODE_SNAPSHOT_NAME_INVALID")
    existing = backup_root / snapshot_name
    _guard(existing, directory=True)
    manifest = _json(_bytes(existing / "code-snapshot-manifest.json", maximum=MAX_CONFIG_BYTES))
    from desktop_orchestrator_isolated_rollback import _same_manifest, _manifest_digest as code_digest
    source = code_manifest(root)
    digest = code_digest(source)
    if (manifest.get("runtime_sha") != EXPECTED_INSTALLED_SHA or
        manifest.get("kind") != "isolated_code_backup" or
        not _same_manifest(manifest, source) or
        not _same_manifest(manifest, code_manifest(existing)) or
        (snapshot_name == SOURCE_CODE_BACKUP and
         digest != "b6ac3457718c1d6ed1ff4b09967b527254105ab75be5dd23031ca5fa5377f62f")):
        raise StateError("CODE_BACKUP_MISMATCH")
    return digest


def snapshot(*, root: Path, appdata: Path, run_id: str, attempt: str,
             before_fn: Callable[[], dict[str, Any]],
             protect_fn: Callable[[bytes], bytes],
             unprotect_fn: Callable[[bytes], bytes],
             workflow: str = LEGACY_WORKFLOW) -> dict[str, Any]:
    if not RUN_RE.fullmatch(run_id) or not ATTEMPT_RE.fullmatch(attempt):
        raise StateError("EXECUTION_ID_INVALID")
    def state_ok(value: dict[str, Any]) -> bool:
        return (value.get("installed_source_sha") == EXPECTED_INSTALLED_SHA and
                value.get("live_state_consistent") is True and
                value.get("pending_refresh_present") is False)
    if not state_ok(before_fn()):
        raise StateError("LIVE_PRECONDITION_CHANGED")
    config = _fixed_inputs(root, appdata=appdata)
    backup_root = root / "data/runtime-backups"
    _guard(backup_root, directory=True)
    code_name = code_snapshot_name(run_id, attempt, workflow=workflow)
    code_sha256 = _code_snapshot_ok(root, backup_root, snapshot_name=code_name)
    selected = backup_root / f"protected-state-{EXPECTED_INSTALLED_SHA[:12]}-{run_id}-{attempt}"
    replayed = selected.exists() or selected.is_symlink()
    if not replayed:
        sqlite_bytes = _sqlite_consistent_bytes(root / "data/orchestrator.db")
        inputs = {**config, "sqlite": sqlite_bytes}
        with tempfile.TemporaryDirectory(prefix="state-stage-", dir=backup_root) as staging:
            stage = Path(staging)
            content: dict[str, Any] = {}
            for name in CIPHER_FILES:
                encrypted = protect_fn(inputs[name])
                if not isinstance(encrypted, bytes) or not encrypted:
                    raise StateError("DPAPI_ENCRYPTED_DATA_INVALID")
                (stage / _cipher_filename(name)).write_bytes(encrypted)
                content[name] = {
                    "cipher_sha256": hashlib.sha256(encrypted).hexdigest(),
                    "plain_sha256": hashlib.sha256(inputs[name]).hexdigest(),
                    "size": len(inputs[name]),
                }
            manifest = {"schema": 1, "source_sha": EXPECTED_INSTALLED_SHA, "entries": content}
            (stage / "snapshot.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            if selected.exists() or selected.is_symlink():
                raise StateError("SNAPSHOT_TARGET_CHANGED")
            try:
                stage.rename(selected)
            except OSError:
                raise StateError("SNAPSHOT_PUBLISH_FAILED") from None
    restored, tables, manifest = _read_snapshot(selected, decrypt_fn=unprotect_fn)
    _check_contract(root, restored)
    if not replayed:
        for key, value in config.items():
            if restored[key] != value:
                raise StateError("SNAPSHOT_CONFIG_CHANGED")
    if not state_ok(before_fn()) or _fixed_inputs(root, appdata=appdata) != config:
        raise StateError("LIVE_POSTCONDITION_CHANGED")
    return {
        "result": "PROTECTED_STATE_RESTORE_VERIFIED",
        "encrypted_local_snapshot_present": True,
        "replayed": replayed,
        "config_items_verified": len(config),
        "sqlite_restore_in_memory": True,
        "sqlite_table_count": tables,
        "snapshot_manifest_sha256": _manifest_digest(manifest),
        "code_snapshot_sha256": code_sha256,
        "user_scoped_dpapi": True,
        "raw_content_uploaded": False,
        "service_rollback_verified": False,
        "recovery_allowed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore test without changing live service")
    parser.add_argument("--confirm", choices=[CONFIRM], required=True)
    args = parser.parse_args()
    outcome: dict[str, Any] = {
        "schema": 1, "host": HOST, "repository": REPO,
        "source_sha": os.environ.get("GITHUB_SHA", ""),
        "result": "PROTECTED_STATE_RESTORE_BLOCKED",
        "recovery_allowed": False, "production_touched": False,
        "raw_content_uploaded": False, "service_rollback_verified": False,
    }
    try:
        if (os.name != "nt" or socket.gethostname().casefold() != HOST.casefold() or
            os.environ.get("RUNNER_NAME") != RUNNER or
            os.environ.get("GITHUB_REPOSITORY") != REPO or
            os.environ.get("GITHUB_ACTIONS") != "true" or
            not re.fullmatch(r"[0-9a-f]{40}", outcome["source_sha"])):
            raise StateError("HOST_OR_RUNNER_NOT_AUTHORIZED")
        appdata = os.environ.get("APPDATA", "")
        if not appdata:
            raise StateError("APPDATA_MISSING")
        from orchestrator_supervisor_readonly_probe import collect
        result = snapshot(
            root=RUNTIME_ROOT, appdata=Path(appdata),
            run_id=os.environ.get("GITHUB_RUN_ID", ""),
            attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
            before_fn=collect,
            protect_fn=lambda x: dpapi(x, encrypt=True),
            unprotect_fn=lambda x: dpapi(x, encrypt=False),
            workflow=os.environ.get("GITHUB_WORKFLOW", ""),
        )
        outcome.update(result)
        code = 0
    except (StateError, OSError, ValueError) as failure:
        outcome["blocker_code"] = safe_blocker_code(failure)
        code = 2
    print(json.dumps(outcome, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
