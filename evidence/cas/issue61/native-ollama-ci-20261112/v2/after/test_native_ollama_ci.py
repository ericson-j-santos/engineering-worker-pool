"""Tests exercise fail-closed admission and digest checks without downloads."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'native_ollama_ci.py'
spec = importlib.util.spec_from_file_location('native_ci', MODULE)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
SHA = 'a' * 40

class NativeCIContractTest(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        d = Path(self.sandbox.name)
        self.event = d / 'event.json'
        self.payload = {
            'pull_request': {'head': {'sha': SHA, 'repo': {'full_name': native.REPO}},
                             'base': {'ref': 'main'}}}
        self.event.write_text(json.dumps(self.payload), encoding='utf-8')
        self.env = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': native.REPO,
                    'GITHUB_ACTOR': 'ericson-j-santos', 'GITHUB_EVENT_NAME': 'pull_request',
                    'GITHUB_EVENT_PATH': str(self.event), 'RUNNER_TEMP': str(d),
                    'EXPECTED_SHA': SHA}

    def admitted(self, env=None, sha=SHA):
        with mock.patch.object(native.platform, 'system', return_value='Linux'), \
             mock.patch.object(native.platform, 'machine', return_value='x86_64'):
            return native.admission(env or self.env, head=sha)

    def test_valid_exact_same_repo_pr(self):
        self.assertEqual(self.admitted(), Path(self.sandbox.name) / 'ewp-native-ollama-ci')

    def test_wrong_head_rejected(self):
        with self.assertRaisesRegex(native.Blocked, '^HEAD_CHANGED$'):
            self.admitted(sha='b' * 40)

    def test_owner_only(self):
        with self.assertRaisesRegex(native.Blocked, '^ACTOR_NOT_ADMITTED$'):
            self.admitted(dict(self.env, GITHUB_ACTOR='someone'))

    def test_no_issue_comment_dispatch(self):
        with self.assertRaisesRegex(native.Blocked, '^EVENT_NOT_ADMITTED$'):
            self.admitted(dict(self.env, GITHUB_EVENT_NAME='issue_comment'))

    def test_no_fork(self):
        self.payload['pull_request']['head']['repo']['full_name'] = 'other/repo'
        self.event.write_text(json.dumps(self.payload))
        with self.assertRaisesRegex(native.Blocked, '^PR_HEAD_OR_BASE_NOT_ADMITTED$'):
            self.admitted()

    def test_no_alternate_base(self):
        self.payload['pull_request']['base']['ref'] = 'other'
        self.event.write_text(json.dumps(self.payload))
        with self.assertRaisesRegex(native.Blocked, '^PR_HEAD_OR_BASE_NOT_ADMITTED$'):
            self.admitted()

    def test_missing_event_payload(self):
        with self.assertRaisesRegex(native.Blocked, '^EVENT_PAYLOAD_MISSING$'):
            self.admitted(dict(self.env, GITHUB_EVENT_PATH=str(self.event) + '.missing'))

    def test_invalid_archive_hash_has_full_sha(self):
        self.assertRegex(native.ARCHIVE_SHA256, '^[a-f0-9]{64}$')
        self.assertEqual(native.URL, 'https://github.com/ollama/ollama/releases/download/v0.40.16/ollama-linux-amd64.tar.zst')

    def test_model_pins_are_distinct(self):
        self.assertNotEqual(native.MODEL_PINS['initial'], native.MODEL_PINS['structured'])

    def test_model_digest_accepts_expected(self):
        with mock.patch.object(native, 'api', return_value={'models': [
            {'name': native.MODEL_PINS['structured'][0], 'digest': 'dae161e27b0e' + 'a' * 52}] } ):
            self.assertEqual(native.model_digest(*native.MODEL_PINS['structured'])[:12], 'dae161e27b0e')

    def test_wrong_model_digest_rejected(self):
        with mock.patch.object(native, 'api', return_value={'models': [
            {'name': native.MODEL_PINS['structured'][0], 'digest': 'c' * 64}]}):
            with self.assertRaisesRegex(native.Blocked, '^MODEL_DIGEST_PIN_MISMATCH$'):
                native.model_digest(*native.MODEL_PINS['structured'])

    def test_model_missing_rejected(self):
        with mock.patch.object(native, 'api', return_value={'models': []}):
            with self.assertRaisesRegex(native.Blocked, '^MODEL_NOT_IN_INVENTORY$'):
                native.model_digest(*native.MODEL_PINS['initial'])

    def test_unauthorized_root_symlink_rejected(self):
        link = Path(self.sandbox.name) / 'ewp-native-ollama-ci'
        link.symlink_to(Path(self.sandbox.name) / 'nothing')
        with self.assertRaisesRegex(native.Blocked, '^ROOT_SYMLINK_DENIED$'):
            self.admitted()

    def test_process_starttime_is_checked(self):
        with mock.patch.object(native.Path, 'resolve', return_value=Path('/tmp/executable')), \
             mock.patch.object(native, 'process_starttime', return_value='100'):
            self.assertFalse(native.process_identity(42, Path('/tmp/executable'), '101'))
            self.assertTrue(native.process_identity(42, Path('/tmp/executable'), '100'))

    def test_running_other_daemon_rejected_before_create(self):
        root = Path(self.sandbox.name) / 'ewp-native-ollama-ci'
        sock = mock.MagicMock()
        sock.__enter__.return_value.connect_ex.return_value = 0
        with mock.patch.object(native.shutil, 'disk_usage') as usage, \
             mock.patch.object(native.socket, 'socket', return_value=sock), \
             mock.patch.object(native, 'streamed_archive') as download:
            usage.return_value.free = native.MIN_DISK_BYTES + 1
            with self.assertRaisesRegex(native.Blocked, '^LOOPBACK_PORT_ALREADY_OWNED$'):
                native.start(root)
            download.assert_not_called()
            self.assertFalse(root.exists())

    def test_no_other_pid_may_be_killed(self):
        with mock.patch.object(native, 'state', side_effect=native.Blocked('OWNED_PROCESS_NOT_VERIFIED')), \
             mock.patch.object(native.os, 'kill') as kill:
            with self.assertRaisesRegex(native.Blocked, '^OWNED_PROCESS_NOT_VERIFIED$'):
                native.stop(Path(self.sandbox.name))
            kill.assert_not_called()

if __name__ == '__main__':
    unittest.main()
