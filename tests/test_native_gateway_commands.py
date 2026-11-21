from __future__ import annotations

import pytest

from app.native_gateway_commands import GovernedCommand, ImmutableScriptSource, NativeGatewayCommandRegistry
from pathlib import Path
import subprocess
import hashlib
from unittest.mock import Mock
from app.native_one_shot import AdapterDescriptor, NativeExecutorBlocked

D = AdapterDescriptor(
    "ci.native.patch.v1", ("src/calculator.py",),
    ("ci.commit", "ci.fix", "ci.stage"), ("ci.validate",),
)


def test_command_descriptors_validate_fixed_contracts():
    for spec in (
        GovernedCommand("ci.fix", "builder", ("python", "scripts/ci_fix.py"),
                        2, script_sha256="a" * 64),
        GovernedCommand("ci.stage", "builder",
                        ("git", "add", "--", "src/calculator.py"), 2,
                        allow_dirty=True),
        GovernedCommand("ci.commit", "builder",
                        ("git", "commit", "-m", "fixture repair"), 2,
                        allow_dirty=True, allow_head_change=True),
        GovernedCommand("ci.validate", "validator",
                        ("python", "scripts/ci_validate.py"), 1,
                        script_sha256="b" * 64),
    ):
        spec.validate(D)


@pytest.mark.parametrize("spec,blocked", [
    (GovernedCommand("ci.fix", "builder", ("python", "-c", "print(1)"),
                     2, script_sha256="a" * 64), "native_script_contract"),
    (GovernedCommand("ci.fix", "builder", ("python", "../unsafe.py"),
                     2, script_sha256="a" * 64), "native_script_path"),
    (GovernedCommand("ci.fix", "builder", ("python", "scripts/ci_fix.py"),
                     2), "native_script_contract"),
    (GovernedCommand("ci.fix", "builder", ("python", "scripts/ci_fix.py"),
                     3, script_sha256="a" * 64), "native_command_risk"),
    (GovernedCommand("ci.fix", "builder", ("python", "scripts/ci_fix.py"),
                     2, timeout_seconds=999, script_sha256="a" * 64),
     "native_command_risk"),
    (GovernedCommand("ci.validate", "validator", ("git", "commit", "-m", "x"),
                     2, allow_dirty=True, allow_head_change=True),
     "native_command_risk"),
    (GovernedCommand("ci.stage", "builder",
                     ("git", "push", "origin", "main"), 2),
     "native_git_contract"),
    (GovernedCommand("ci.stage", "builder",
                     ("git", "add", "--", ".env"), 2),
     "native_git_contract"),
    (GovernedCommand("ci.commit", "builder",
                     ("git", "commit", "-m", "foo"), 2,
                     allow_head_change=True), "native_git_contract"),
    (GovernedCommand("ci.commit", "builder",
                     ("git", "commit", "-m", "foo"), 2,
                     allow_dirty=True), "native_git_contract"),
    (GovernedCommand("ci.other", "builder",
                     ("git", "status", "--porcelain"), 1),
     "native_command_contract"),
    (GovernedCommand("ci.validate", "validator", ("bash", "-c", "echo"), 1),
     "native_command_executable"),
])
def test_untrusted_or_wrong_role_spec_fails_before_execution(spec, blocked):
    with pytest.raises(NativeExecutorBlocked, match=blocked):
        spec.validate(D)



def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def test_external_immutable_script_verified_in_separate_checkout(tmp_path):
    source = tmp_path / "installed-ewp"
    source.mkdir()
    _git(source, "init", "-q")
    script = source / "scripts" / "ci_fix.py"
    script.parent.mkdir()
    script.write_text("print('no task claim')\n", encoding="utf-8")
    _git(source, "add", ".")
    _git(source, "-c", "user.name=CI", "-c",
         "user.email=ci@example.invalid", "commit", "-qm", "fixed")
    head = _git(source, "rev-parse", "HEAD")
    _git(source, "checkout", "--detach", "-q", head)
    _git(source, "remote", "add", "origin",
         "https://github.com/ericson-j-santos/engineering-worker-pool.git")
    task = tmp_path / "noteri-task"
    task.mkdir()
    registry = object.__new__(NativeGatewayCommandRegistry)
    registry.workspace = task
    registry.gateway = Mock()
    sig = hashlib.sha256(script.read_bytes()).hexdigest()
    source_pin = ImmutableScriptSource(
        source, "ericson-j-santos/engineering-worker-pool",
        head, "scripts/ci_fix.py")
    spec = GovernedCommand(
        "ci.fix", "builder", ("python", str(script)), 2,
        script_sha256=sig, immutable_source=source_pin)
    spec.validate(D)
    registry._verify_script(spec, {"allowed_roots": []})
    registry.gateway.assert_allowed_path.assert_called_once()
    assert not (task / "scripts" / "ci_fix.py").exists()
    script.write_text("print('tampered')\n", encoding="utf-8")
    with pytest.raises(NativeExecutorBlocked, match="native_external_script_drift"):
        registry._verify_script(spec, {"allowed_roots": []})


def test_external_pin_rejects_wrong_remote_head_or_scope(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    script = source / "scripts" / "ci_fix.py"
    script.parent.mkdir()
    script.write_text("pass\n")
    _git(source, "add", ".")
    _git(source, "-c", "user.name=CI", "-c",
         "user.email=ci@example.invalid", "commit", "-qm", "initial")
    head = _git(source, "rev-parse", "HEAD")
    _git(source, "checkout", "--detach", "-q", head)
    _git(source, "remote", "add", "origin", "https://example.invalid/evil.git")
    registry = object.__new__(NativeGatewayCommandRegistry)
    task = tmp_path / "task"
    task.mkdir()
    registry.workspace = task
    registry.gateway = Mock()
    ref = ImmutableScriptSource(
        source, "ericson-j-santos/engineering-worker-pool",
        head, "scripts/ci_fix.py")
    spec = GovernedCommand("ci.fix", "builder", ("python", str(script)), 2,
                           script_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
                           immutable_source=ref)
    spec.validate(D)
    with pytest.raises(NativeExecutorBlocked, match="native_external_source_integrity_invalid"):
        registry._verify_script(spec, {"allowed_roots": []})
    with pytest.raises(NativeExecutorBlocked, match="native_external_script_contract"):
        ImmutableScriptSource(source, ref.repository, head, "../escape.py").validate(str(script))
