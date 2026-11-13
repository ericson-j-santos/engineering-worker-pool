"""Regression: no leaked temp directory / daemon after failing native startup."""
from pathlib import Path
from unittest import mock
import importlib.util
import pytest

FILE = Path(__file__).resolve().parents[1] / 'scripts' / 'native_ollama_ci.py'
spec = importlib.util.spec_from_file_location('native_ci_cleanup', FILE)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def test_failed_download_removes_only_owned_temporary_files(tmp_path):
    root = tmp_path / 'ewp-native-ollama-ci'
    sock = mock.MagicMock()
    sock.__enter__.return_value.connect_ex.return_value = 1
    with mock.patch.object(native.shutil, 'disk_usage') as disk, \
         mock.patch.object(native.socket, 'socket', return_value=sock), \
         mock.patch.object(native, 'streamed_archive', side_effect=OSError('network failed')), \
         mock.patch.object(native.subprocess, 'Popen') as popen:
        disk.return_value.free = native.MIN_DISK_BYTES + 100
        with pytest.raises(OSError, match='network failed'):
            native.start(root)
        popen.assert_not_called()
    assert not root.exists(), 'LEAKED_DIRECTORY_AFTER_DOWNLOAD_FAILURE'


def test_cleanup_is_idempotent_if_start_never_created_directory(tmp_path):
    root = tmp_path / 'ewp-native-ollama-ci'
    native.stop(root)
    assert not root.exists()


def test_start_daemon_failure_terminates_only_own_popen(tmp_path):
    root = tmp_path / 'ewp-native-ollama-ci'
    sock = mock.MagicMock()
    sock.__enter__.return_value.connect_ex.return_value = 1
    def fake_archive(path):
        path.write_bytes(b'fake archive, never used as code')
    def fake_tar(*args, **kwargs):
        binary = root / 'binary' / 'bin' / 'ollama'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'test-placeholder')
    proc = mock.MagicMock()
    proc.pid = 12345
    proc.poll.return_value = None
    proc.wait.return_value = 0
    with mock.patch.object(native.shutil, 'disk_usage') as disk, \
         mock.patch.object(native.socket, 'socket', return_value=sock), \
         mock.patch.object(native, 'streamed_archive', side_effect=fake_archive), \
         mock.patch.object(native.subprocess, 'run', side_effect=fake_tar), \
         mock.patch.object(native.subprocess, 'Popen', return_value=proc), \
         mock.patch.object(native, 'process_starttime', return_value='777'), \
         mock.patch.object(native, 'process_identity', return_value=False), \
         mock.patch.object(native.os, 'access', return_value=True), \
         mock.patch.object(native, 'api', return_value={'models':[]}), \
         mock.patch.dict(native.os.environ, {'EXPECTED_SHA': 'a'*40}):
        disk.return_value.free = native.MIN_DISK_BYTES + 100
        with pytest.raises(native.Blocked, match='NATIVE_DAEMON_IDENTITY_INVALID'):
            native.start(root)
    proc.terminate.assert_called_once()
    proc.wait.assert_called()
    assert not root.exists(), 'LEAKED_DIRECTORY_AFTER_UNVERIFIED_DAEMON'
