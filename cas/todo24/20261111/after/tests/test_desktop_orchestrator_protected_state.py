"""Offline and Windows-native security checks for protected Orchestrator state."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from desktop_orchestrator_isolated_rollback import RollbackError
import desktop_orchestrator_protected_state as m


def toy_protect(data: bytes) -> bytes:
    # Synthetic obfuscation for isolated logic tests only; production uses DPAPI.
    return b"TEST_ENVELOPE:" + bytes(value ^ 0xA5 for value in data)


def toy_unprotect(data: bytes) -> bytes:
    if not data.startswith(b"TEST_ENVELOPE:"):
        raise m.StateError("SNAPSHOT_UNPROTECT_FAILED")
    return bytes(value ^ 0xA5 for value in data[len(b"TEST_ENVELOPE:"):])


class ProtectedStateTest(unittest.TestCase):
    def test_typed_rollback_failures_retain_only_literal_codes(self):
        for code in sorted(m.SAFE_ROLLBACK_BLOCKERS):
            with self.subTest(code=code):
                self.assertEqual(m.safe_blocker_code(RollbackError(code)), code)
        self.assertEqual(
            m.safe_blocker_code(RollbackError("SECRET_PASSWORD_ABC123")),
            "UNEXPECTED_FILE_ERROR",
        )
        self.assertEqual(
            m.safe_blocker_code(ValueError("FIXED_PATH_MISSING")),
            "UNEXPECTED_FILE_ERROR",
        )
        self.assertEqual(
            m.safe_blocker_code(OSError("C:\\private\\secrets.json")),
            "UNEXPECTED_FILE_ERROR",
        )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "runtime"
        self.root.mkdir()
        self.appdata = Path(self.tmp.name) / "appdata"
        startup = m._startup_path(self.appdata)
        startup.parent.mkdir(parents=True)
        startup.write_text(
            '@echo off\n"C:\\Python\\python.exe" -m scripts.service_supervisor --config "'
            + str(self.root / "service-config.json") + '"\n',
            encoding="utf-8"
        )
        (self.root / "data" / "runtime-backups").mkdir(parents=True)
        service = {
            "mode": "control-plane-worker", "port": 8787,
            "install_root": str(self.root),
            "worker_config": str(self.root / "worker-config.json"),
            "db_path": str(self.root / "data/orchestrator.db"),
            "backup_path": str(self.root / "data/orchestrator.backup.db"),
        }
        worker = {"worker_id": "desktop-pdqk954", "endpoint": "http://127.0.0.1:8787",
                  "private_field": "do-not-expose"}
        version = {"schema_version": 1, "source_sha": m.EXPECTED_INSTALLED_SHA}
        for name, value in (("service-config.json", service), ("worker-config.json", worker),
                            ("runtime-version.json", version)):
            (self.root / name).write_text(json.dumps(value), encoding="utf-8")
        self.db = self.root / "data/orchestrator.db"
        self.dbconn = sqlite3.connect(self.db)
        self.dbconn.execute("PRAGMA journal_mode=WAL")
        self.dbconn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, note TEXT)")
        self.dbconn.execute("INSERT INTO records(note) VALUES (?)", ("secret-database-value",))
        self.dbconn.commit()
        self.addCleanup(self.dbconn.close)
        self.state_calls = 0

    def state(self):
        self.state_calls += 1
        return {"installed_source_sha": m.EXPECTED_INSTALLED_SHA,
                "live_state_consistent": True, "pending_refresh_present": False}

    def execute(self, *, run_id="38310000001", attempt="1", state_fn=None):
        with patch.object(m, "_code_snapshot_ok", return_value=True):
            return m.snapshot(
                root=self.root, appdata=self.appdata, run_id=run_id,
                attempt=attempt, before_fn=state_fn or self.state,
                protect_fn=toy_protect, unprotect_fn=toy_unprotect,
            )

    def test_wal_online_backup_deserializes_and_preserves_committed_row(self):
        raw = m._sqlite_consistent_bytes(self.db)
        self.assertGreater(m._verify_db(raw), 0)
        mem = sqlite3.connect(":memory:")
        self.addCleanup(mem.close)
        mem.deserialize(raw)
        self.assertEqual(mem.execute("SELECT note FROM records").fetchone()[0],
                         "secret-database-value")
        self.assertEqual(raw[18:20], b"\x01\x01")

    def test_full_backup_in_memory_restore_and_idempotent_replay(self):
        first = self.execute()
        self.assertEqual(first["result"], "PROTECTED_STATE_RESTORE_VERIFIED")
        self.assertTrue(first["sqlite_restore_in_memory"])
        self.assertTrue(first["encrypted_local_snapshot_present"])
        self.assertFalse(first["replayed"])
        self.assertEqual(first["config_items_verified"], 4)
        self.assertFalse(first["service_rollback_verified"])
        self.assertFalse(first["recovery_allowed"])
        backup_dir = self.root / "data/runtime-backups"
        snapshots = list(backup_dir.glob("protected-state-*"))
        self.assertEqual(len(snapshots), 1)
        self.assertNotIn(b"do-not-expose", b"".join(p.read_bytes() for p in snapshots[0].glob("*.dpapi")))
        self.assertNotIn(b"secret-database-value", b"".join(p.read_bytes() for p in snapshots[0].glob("*.dpapi")))
        replay = self.execute()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["snapshot_manifest_sha256"], first["snapshot_manifest_sha256"])
        self.assertEqual(len(list(backup_dir.glob("protected-state-*"))), 1)
        self.assertEqual(self.state_calls, 4)

    def test_precondition_wrong_installed_sha_never_creates_backup(self):
        def stale():
            return {"installed_source_sha": "c" * 40,
                    "live_state_consistent": True, "pending_refresh_present": False}
        with self.assertRaisesRegex(m.StateError, "LIVE_PRECONDITION_CHANGED"):
            self.execute(state_fn=stale)
        self.assertEqual(list((self.root / "data/runtime-backups").glob("protected-state-*")), [])

    def test_pending_request_fail_closed(self):
        def pending():
            return {"installed_source_sha": m.EXPECTED_INSTALLED_SHA,
                    "live_state_consistent": True, "pending_refresh_present": True}
        with self.assertRaisesRegex(m.StateError, "LIVE_PRECONDITION_CHANGED"):
            self.execute(state_fn=pending)

    def test_configs_are_bound_to_exact_runtime_paths(self):
        path = self.root / "service-config.json"
        service = json.loads(path.read_text())
        service["db_path"] = str(self.root / "other.db")
        path.write_text(json.dumps(service))
        with self.assertRaisesRegex(m.StateError, "SUPERVISOR_CONFIG_MISMATCH"):
            self.execute()

    def test_file_redirect_is_rejected(self):
        real = self.root / "service-config.json"
        link = self.root / "redirected.json"
        real.rename(link)
        try:
            real.symlink_to(link)
        except OSError:
            self.skipTest("Symlink creation restricted on runner")
        with self.assertRaisesRegex(ValueError, "FIXED_PATH_REDIRECTED"):
            self.execute()

    def test_tampering_with_encrypted_snapshot_is_detected_on_replay(self):
        self.execute()
        folder = next((self.root / "data/runtime-backups").glob("protected-state-*"))
        item = folder / "worker.dpapi"
        item.write_bytes(b"malicious replacement")
        with self.assertRaisesRegex(m.StateError, "SNAPSHOT_CIPHER_MISMATCH"):
            self.execute()

    def test_malformed_database_never_returns_verified(self):
        self.dbconn.close()
        self.db.write_bytes(b"NOT A DATABASE")
        with self.assertRaises(m.StateError):
            self.execute()
        self.assertEqual(list((self.root / "data/runtime-backups").glob("protected-state-*")), [])

    def test_invalid_run_ids_never_create_backup(self):
        for run_id in ("", "1", "001", "../hack", "1;echo", "x" * 100):
            with self.subTest(run_id=run_id), self.assertRaisesRegex(m.StateError, "EXECUTION_ID_INVALID"):
                self.execute(run_id=run_id)

    def test_test_detects_injected_live_change(self):
        original = self.state
        def changed_on_second_call():
            result = original()
            if self.state_calls == 2:
                result["pending_refresh_present"] = True
            return result
        with self.assertRaisesRegex(m.StateError, "LIVE_POSTCONDITION_CHANGED"):
            self.execute(state_fn=changed_on_second_call)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI requires native Windows")
    def test_real_windows_dpapi_positive_and_tamper(self):
        input_data = b"sensitive bytes for dpapi test"
        encrypted = m.dpapi(input_data, encrypt=True)
        self.assertNotEqual(encrypted, input_data)
        self.assertEqual(m.dpapi(encrypted, encrypt=False), input_data)
        changed = bytearray(encrypted)
        changed[-1] ^= 0x01
        with self.assertRaises(m.StateError):
            m.dpapi(bytes(changed), encrypt=False)


class CurrentRunCodeSnapshotBindingTests(unittest.TestCase):
    """Never accept a historical snapshot in the new sequential recovery path."""

    def test_source_selected_by_trusted_workflow_and_run_identity(self):
        current = m.code_snapshot_name(
            "42927770513", "1", workflow=m.SEQUENTIAL_WORKFLOW,
        )
        self.assertEqual(current, "verified-code-33f312d0ac4c-42927770513-1")
        self.assertEqual(
            m.code_snapshot_name("42927770513", "1", workflow=m.LEGACY_WORKFLOW),
            m.SOURCE_CODE_BACKUP,
        )
        for bad in ("", "other", "Desktop Orchestrator Readback"):
            with self.subTest(workflow=bad), self.assertRaisesRegex(
                m.StateError, "CODE_SNAPSHOT_WORKFLOW_NOT_ADMITTED"
            ):
                m.code_snapshot_name("42927770513", "1", workflow=bad)
        for bad in ("", "../bad", "1", "0", "42;echo"):
            with self.subTest(run_id=bad), self.assertRaisesRegex(
                m.StateError, "CODE_SNAPSHOT_EXECUTION_ID_INVALID"
            ):
                m.code_snapshot_name(bad, "1", workflow=m.SEQUENTIAL_WORKFLOW)

    def test_same_run_manifest_origin_digest_and_tamper_guards(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "live"
            backups = root / "data/runtime-backups"
            backups.mkdir(parents=True)
            (root / "orchestrator").mkdir()
            (root / "scripts").mkdir()
            (root / "orchestrator/__init__.py").write_text("VERSION = 1\\n")
            (root / "scripts/service_supervisor.py").write_text("def main(): return 0\\n")
            current = m.code_manifest(root)
            from desktop_orchestrator_isolated_rollback import _manifest_digest
            name = m.code_snapshot_name("42927770513", "1", workflow=m.SEQUENTIAL_WORKFLOW)
            snapshot = backups / name
            snapshot.mkdir()
            shutil.copytree(root / "orchestrator", snapshot / "orchestrator")
            shutil.copytree(root / "scripts", snapshot / "scripts")
            manifest = {**current, "runtime_sha": m.EXPECTED_INSTALLED_SHA,
                        "kind": "isolated_code_backup"}
            stored = snapshot / "code-snapshot-manifest.json"
            stored.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(
                m._code_snapshot_ok(root, backups, snapshot_name=name),
                _manifest_digest(current),
            )
            # The old literal snapshot is not a fallback in sequential mode.
            with self.assertRaises((m.StateError, ValueError, OSError)):
                m._code_snapshot_ok(root, backups, snapshot_name=m.SOURCE_CODE_BACKUP)
            stored.write_text(json.dumps({**manifest, "runtime_sha": "a" * 40}))
            with self.assertRaisesRegex(m.StateError, "CODE_BACKUP_MISMATCH"):
                m._code_snapshot_ok(root, backups, snapshot_name=name)
            stored.write_text(json.dumps(manifest))
            (snapshot / "scripts/service_supervisor.py").write_text("def main(): return 8\\n")
            with self.assertRaisesRegex(m.StateError, "CODE_BACKUP_MISMATCH"):
                m._code_snapshot_ok(root, backups, snapshot_name=name)

    def test_reject_noncanonical_snapshot_name_without_reading_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            for bad in ("../older", "verified-code-latest", "not-a-snapshot"):
                with self.subTest(bad=bad), self.assertRaisesRegex(
                    m.StateError, "CODE_SNAPSHOT_NAME_INVALID"
                ):
                    m._code_snapshot_ok(Path(temp), Path(temp), snapshot_name=bad)


if __name__ == "__main__":
    unittest.main()
