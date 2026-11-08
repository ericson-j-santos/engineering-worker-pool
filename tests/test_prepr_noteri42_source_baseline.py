"""Negative gates for the real-source Noteri #42 baseline runner.

These tests never invoke Noteri code, a host API, UAC, model or CAS.
The real-source positive regression is verified separately by two CI jobs.
"""
from __future__ import annotations

import subprocess

import pytest

from scripts.prepr_noteri42_source_baseline import (
    BaselineBlocked, SOURCE_SHA, source_identity, reproduce,
)


def test_missing_checkout_blocks_before_import(tmp_path):
    with pytest.raises(BaselineBlocked, match="noteri_source_checkout_missing"):
        source_identity(tmp_path / "absent")


def test_empty_directory_blocks_before_git(tmp_path):
    with pytest.raises(BaselineBlocked, match="noteri_source_file_untrusted"):
        source_identity(tmp_path)


def test_source_file_symlink_blocks_before_import(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "noteri_control_plane_watchdog.py").write_text("test", encoding="utf-8")
    external = tmp_path / "external"
    external.write_text("test", encoding="utf-8")
    try:
        (scripts / "noteri_control_plane_watchdog_uac_launcher.py").symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(BaselineBlocked, match="noteri_source_file_untrusted"):
        source_identity(tmp_path)


@pytest.mark.parametrize("actual", ["f" * 40, "short", ""])
def test_wrong_sha_blocks_before_execution(monkeypatch, tmp_path, actual):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "noteri_control_plane_watchdog.py").write_text("fixture", encoding="utf-8")
    (scripts / "noteri_control_plane_watchdog_uac_launcher.py").write_text("fixture", encoding="utf-8")
    calls = []

    def fake_git(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=actual + "\n", stderr="")

    monkeypatch.setattr(
        "scripts.prepr_noteri42_source_baseline.subprocess.run", fake_git
    )
    with pytest.raises(BaselineBlocked, match="noteri_source_sha_drift"):
        source_identity(tmp_path)
    assert len(calls) == 1
    assert calls[0][:3] == ["git", "-C", str(tmp_path)]
    assert SOURCE_SHA != actual


def test_redirected_checkout_is_rejected_before_git(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(BaselineBlocked, match="noteri_source_checkout_redirected"):
        reproduce(alias)


def test_unavailable_git_blocks_before_import(monkeypatch, tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "noteri_control_plane_watchdog.py").write_text("fixture", encoding="utf-8")
    (scripts / "noteri_control_plane_watchdog_uac_launcher.py").write_text("fixture", encoding="utf-8")

    def no_git(*_args, **_kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(
        "scripts.prepr_noteri42_source_baseline.subprocess.run", no_git
    )
    with pytest.raises(BaselineBlocked, match="noteri_git_identity_unavailable"):
        source_identity(tmp_path)
