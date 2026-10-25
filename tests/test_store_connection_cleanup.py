"""Deterministic SQLite connection lifecycle controls for the shared Worker Pool.

All database files are disposable and tests need no credentials, model or API.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3

import pytest

from app import store as store_module
from app.store import WorkerPoolStore


def test_original_stdlib_with_context_keeps_file_handle_open(tmp_path: Path):
    path = tmp_path / "negative.db"
    connection = sqlite3.connect(path)
    with connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1
    # Negative control: sqlite3.Connection.__exit__ never closes the handle.
    assert connection.execute("SELECT 1").fetchone()[0] == 1
    connection.close()
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    path.unlink()
    assert not path.exists()


def test_read_context_closes_connection_and_replays_without_leaks(tmp_path: Path):
    path = tmp_path / "reader.db"
    store = WorkerPoolStore(path)
    for _ in range(3):
        with store._read_db() as connection:
            row = connection.execute("SELECT enabled FROM pool_control").fetchone()
            assert row is not None
        assert row["enabled"] == 0
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        assert store.fenced_cutover_status()["enabled"] is False
        assert store.list_quarantine() == []
    # On Windows a leaked open SQLite handle blocks this real OS operation.
    path.unlink()
    assert not path.exists()


def test_exception_rolls_back_and_closes_reader(tmp_path: Path):
    path = tmp_path / "rollback.db"
    store = WorkerPoolStore(path)
    with pytest.raises(RuntimeError, match="controlled_rollback"):
        with store._read_db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE pool_control SET enabled=1")
            raise RuntimeError("controlled_rollback")
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT enabled FROM pool_control")
    assert store.fenced_cutover_status()["enabled"] is False
    path.unlink()
    assert not path.exists()


def test_concurrent_independent_readers_close_deterministically(tmp_path: Path):
    path = tmp_path / "parallel.db"
    store = WorkerPoolStore(path)
    def probe(_):
        with store._read_db() as db:
            row = db.execute("SELECT enabled FROM pool_control").fetchone()
        with pytest.raises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")
        return row["enabled"]
    with ThreadPoolExecutor(max_workers=3) as worker_threads:
        observed = list(worker_threads.map(probe, range(9)))
    assert observed == [0] * 9
    path.unlink()
    assert not path.exists()


def test_pragma_configuration_failure_closes_handle(monkeypatch, tmp_path: Path):
    store = WorkerPoolStore(tmp_path / "safe.db")
    class BrokenConnection:
        def __init__(self):
            self.closed = False
            self.row_factory = None
        def execute(self, _sql):
            raise sqlite3.OperationalError("controlled_pragma_failure")
        def close(self):
            self.closed = True
    fake = BrokenConnection()
    monkeypatch.setattr(store_module.sqlite3, "connect", lambda *a, **k: fake)
    with pytest.raises(sqlite3.OperationalError, match="controlled_pragma_failure"):
        store._db()
    assert fake.closed is True


def test_existing_read_api_keeps_results_and_idempotence(tmp_path: Path):
    path = tmp_path / "api.db"
    store = WorkerPoolStore(path, expected_rules_sha="a" * 40)
    store.configure_repository(
        repository="ericson-j-santos/engineering-worker-pool",
        enabled=True, max_in_flight=1,
    )
    first, second = store.snapshot(), store.snapshot()
    assert first["queue"] == second["queue"]
    assert first["quarantine_count"] == second["quarantine_count"] == 0
    assert store.get_repository(
        "ericson-j-santos/engineering-worker-pool")["enabled"] is True
    path.unlink()
    assert not path.exists()


def test_native_windows_real_file_removal_after_reader_close(tmp_path: Path):
    if os.name != "nt":
        pytest.skip("This OS-specific case runs in the existing Windows CI")
    path = tmp_path / "windows.db"
    store = WorkerPoolStore(path)
    for _ in range(2):
        assert store.fenced_cutover_status()["enabled"] is False
        assert store.list_quarantine() == []
    path.unlink()
    assert not path.exists()
