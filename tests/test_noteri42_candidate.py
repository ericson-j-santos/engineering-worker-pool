"""Pure candidate tests; no Qwen inference, local host or Git writes."""
import hashlib

import pytest

from app.noteri42_candidate import CandidateBlocked, assemble, PATH


ORIGINAL = '''from pathlib import Path
from typing import Any

def harmless():
    return "unchanged"

def attest_installed_release(metadata: dict[str, Any], expected_sha: str, source: Path) -> None:
    if metadata.get("source_sha") != expected_sha:
        raise LauncherError("WATCHDOG_INSTALLED_SHA_MISMATCH")
    if not metadata.get("task"):
        raise LauncherError("WATCHDOG_INSTALLED_TASK_UNVERIFIED")

def following():
    return "also unchanged"
'''
CANDIDATE = '''def attest_installed_release(metadata: dict[str, Any], expected_sha: str, source: Path) -> None:
    if metadata.get("source_sha") != expected_sha:
        raise LauncherError("WATCHDOG_INSTALLED_SHA_MISMATCH")
    task = metadata.get("task") or {}
    if not task:
        raise LauncherError("WATCHDOG_INSTALLED_TASK_UNVERIFIED")
    runner_home = metadata.get("runner_home")
    action_count = task.get("action_count")
    action_python_observed = task.get("action_python_observed")
    action_arguments_sha256 = task.get("action_arguments_sha256")
    if action_count != 1 or action_python_observed is not True:
        raise LauncherError("WATCHDOG_INSTALLED_TASK_ACTION_UNVERIFIED")
    if not runner_home or not action_arguments_sha256:
        raise LauncherError("WATCHDOG_INSTALLED_TASK_ACTION_UNVERIFIED")
'''


def test_model_function_is_a_pure_bounded_source_transform():
    result = assemble(ORIGINAL.encode(), CANDIDATE)
    assert result.source_path == PATH
    assert result.changed is True
    assert result.source_sha256 == hashlib.sha256(ORIGINAL.encode()).hexdigest()
    assert result.candidate_sha256 == hashlib.sha256(result.patched_bytes).hexdigest()
    assert result.candidate_sha256 != result.source_sha256
    assert result.function_sha256 == hashlib.sha256(CANDIDATE.encode()).hexdigest()
    output = result.patched_bytes.decode()
    assert 'def harmless():\n    return "unchanged"' in output
    assert 'def following():\n    return "also unchanged"' in output
    assert "WATCHDOG_INSTALLED_TASK_ACTION_UNVERIFIED" in output
    # Do not mutate caller data or create a git object.
    assert "WATCHDOG_INSTALLED_TASK_ACTION_UNVERIFIED" not in ORIGINAL


@pytest.mark.parametrize("bad,reason", [
    (CANDIDATE + "\nimport os\n", "noteri42_candidate_shape"),
    (CANDIDATE + "\ndef another():\n    pass\n", "noteri42_candidate_shape"),
    ("@decorator\n" + CANDIDATE, "noteri42_candidate_signature_drift"),
    (CANDIDATE.replace("metadata: dict[str, Any]", "metadata: str"),
     "noteri42_candidate_signature_drift"),
    (CANDIDATE.replace('raise LauncherError("WATCHDOG_INSTALLED_SHA_MISMATCH")',
                       "return None"), "noteri42_existing_guards_removed"),
    (CANDIDATE.replace('action_arguments_sha256 = task.get("action_arguments_sha256")',
                       'action_arguments_sha256 = None'),
     "noteri42_action_guard_absent"),
    (CANDIDATE.replace('action_count = task.get("action_count")',
                       'action_count = subprocess.run("cmd")'),
     "noteri42_candidate_privileged_call"),
    (CANDIDATE.replace('action_count = task.get("action_count")',
                       'action_count = open("secret")'),
     "noteri42_candidate_privileged_call"),
    (CANDIDATE.replace('action_count = task.get("action_count")',
                       'action_count = eval("1")'),
     "noteri42_candidate_privileged_call"),
    (CANDIDATE.replace('action_count = task.get("action_count")',
                       'action_count = os.system("x")'),
     "noteri42_candidate_privileged_call"),
    (CANDIDATE.replace('action_count = task.get("action_count")',
                       'action_count = lambda: 1'),
     "noteri42_candidate_forbidden_syntax"),
])
def test_forbidden_model_candidates_never_become_patch(bad, reason):
    with pytest.raises(CandidateBlocked, match=reason):
        assemble(ORIGINAL.encode(), bad)


def test_replay_is_deterministic_without_model_calls():
    a = assemble(ORIGINAL.encode(), CANDIDATE)
    b = assemble(ORIGINAL.encode(), CANDIDATE)
    assert a == b


def test_unmodified_function_and_untrusted_source_fail_closed():
    copied = ORIGINAL.split("def attest_installed_release(")[1].split("def following()")[0]
    existing = "def attest_installed_release(" + copied
    with pytest.raises(CandidateBlocked, match="noteri42_candidate_no_change"):
        assemble(ORIGINAL.encode(), existing)
    with pytest.raises(CandidateBlocked, match="noteri42_original_size"):
        assemble(b"", CANDIDATE)
    with pytest.raises(CandidateBlocked, match="noteri42_original_encoding"):
        assemble(b"\xff", CANDIDATE)
