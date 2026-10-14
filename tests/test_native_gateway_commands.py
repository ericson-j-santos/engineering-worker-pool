from __future__ import annotations

import pytest

from app.native_gateway_commands import GovernedCommand
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
