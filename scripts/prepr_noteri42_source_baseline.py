"""Reproduce an untrusted scheduled-task Exec action accepted by the Noteri launcher.

DEV / hosted CI ONLY. Uses an immutable source checkout and synthetic files.
Never runs schtasks, UAC, a worker, or a host command. A green baseline-probe
run means the defect was observed; it does not mean the product is fixed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

SOURCE_SHA = "078927faf7bf970e9158631003dc2804bd26c390"
SOURCE_FILE = "scripts/noteri_control_plane_watchdog.py"
LAUNCHER_FILE = "scripts/noteri_control_plane_watchdog_uac_launcher.py"


class BaselineBlocked(RuntimeError):
    pass


def need(value: bool, reason: str) -> None:
    if not value:
        raise BaselineBlocked(reason)


def source_identity(root: Path) -> None:
    need(root.is_dir() and not root.is_symlink(),
         "noteri_source_checkout_missing")
    for name in (SOURCE_FILE, LAUNCHER_FILE):
        f = root / name
        need(f.is_file() and not f.is_symlink(),
             "noteri_source_file_untrusted")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise BaselineBlocked("noteri_git_identity_unavailable") from None
    need(result.returncode == 0 and result.stdout.strip() == SOURCE_SHA,
         "noteri_source_sha_drift")


def _load_launcher(root: Path):
    scripts = root / "scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location(
        "noteri42_source_launcher_baseline", scripts / "noteri_control_plane_watchdog_uac_launcher.py"
    )
    need(spec is not None and spec.loader is not None,
         "noteri_launcher_import_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reproduce(source_root: Path) -> dict:
    """Report a verified failing behavioral baseline, not simulated Qwen work."""
    source_root = Path(source_root)
    need(not source_root.is_symlink(), "noteri_source_checkout_redirected")
    source_root = source_root.resolve()
    source_identity(source_root)
    launcher = _load_launcher(source_root)
    # This task meets the old S4U/boot predicate but has a WRONG/EXTRA Exec.
    wrong_action = {
        "exists": True, "enabled": True,
        "trigger_at_startup": True, "logon_type": "S4U",
        "action_count": 2, "action_python_observed": False,
        "action_arguments_sha256": None,
    }
    with tempfile.TemporaryDirectory(prefix="noteri42-source-baseline-") as d:
        runtime = Path(d)
        original = source_root / SOURCE_FILE
        release = runtime / "releases" / SOURCE_SHA / SOURCE_FILE
        release.parent.mkdir(parents=True)
        release.write_bytes(original.read_bytes())
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        metadata = {
            "source_sha": SOURCE_SHA, "release_script": str(release),
            "release_sha256": digest, "task": {"exists": True},
            "runner_home": str(runtime / "runner"),
        }
        finalized = []

        def refuse_install(*_args, **_kwargs):
            raise BaselineBlocked("noteri_uac_or_install_forbidden_in_probe")

        def fake_finalize(_path, _task):
            finalized.append(True)
            return {"metadata": {}, "cycle": {}}

        # Only host identity, read-only OS inventory, privileged actions and
        # durable write are simulated. Actual launcher guards are executed.
        with patch.object(launcher, "validate_launcher", return_value=None), \
             patch.object(launcher, "load_metadata", return_value=metadata), \
             patch.object(launcher, "task_status", return_value=wrong_action), \
             patch.object(launcher, "finalize", side_effect=fake_finalize), \
             patch.object(launcher, "shell_execute_runas", side_effect=refuse_install), \
             patch.object(launcher, "powershell_runas", side_effect=refuse_install), \
             patch.object(launcher, "shell_application_runas", side_effect=refuse_install), \
             patch.object(launcher, "is_admin", return_value=False), \
             patch.object(launcher.watchdog, "runtime_root", return_value=runtime):
            try:
                result = launcher.launch(
                    repo_root=source_root, source_sha=SOURCE_SHA,
                    metadata_path=runtime / "metadata.json",
                    confirm=launcher.LAUNCH_CONFIRM,
                    timeout_seconds=15,
                )
            except (BaselineBlocked, launcher.LauncherError):
                raise BaselineBlocked("noteri_baseline_not_reproduced") from None

    # New repair must reject this record BEFORE finalize (or any UAC);
    # existing code erroneously returns already_ready with invalid Exec.
    need(result.get("ok") is True
         and result.get("mode") == "already_ready"
         and result.get("result") == "NOTERI_CONTROL_PLANE_HEADLESS_READY"
         and len(finalized) == 1,
         "noteri_baseline_not_reproduced")
    return {
        "schema_version": "prepr.noteri42.baseline.v1",
        "status": "REAL_SOURCE_DEFECT_REPRODUCED_ONLY",
        "repository": "ericson-j-santos/noteri-runtime",
        "issue_number": 42, "source_sha": SOURCE_SHA,
        "scenario": "S4U_BOOT_WITH_UNATTESTED_EXEC",
        "observed": "launcher_already_ready_without_exec_attestation",
        "expected": "reject_before_finalize_or_elevation",
        "model_executed": False, "cas_written": False,
        "validator_executed": False, "ready_for_pr": False,
        "host_modified": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = reproduce(args.source_root)
    except (BaselineBlocked, OSError, ValueError, ImportError, TypeError) as exc:
        print(json.dumps({
            "status": "BLOCKED",
            "reason": str(exc) if isinstance(exc, BaselineBlocked)
                      else "noteri_source_probe_unavailable",
            "ready_for_pr": False,
        }, sort_keys=True))
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
