"""Fail-closed, local-only staging of a *validated* model-produced Git delta.

This is NOT a remote/durable CAS, a READY_FOR_PR decision, or authorization to
publish a branch. The caller must promote the resulting bytes into an approved
immutable remote CAS and independently read them back before PR admission.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
TARGET = "app/portfolio_bridge.py"
MAX_PATCH = 128 * 1024
MAX_BUNDLE = 4 * 1024 * 1024


class StageBlocked(ValueError):
    pass


def need(condition: bool, code: str) -> None:
    if not condition:
        raise StageBlocked(code)


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=20, check=False, env={
                              **os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                              "GIT_CONFIG_GLOBAL": os.devnull,
                              "GIT_TERMINAL_PROMPT": "0",
                          })
    need(proc.returncode == 0, "git_verification_failed")
    return proc.stdout.strip()


def _entry(name: str, content: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = 0o600 << 16
    return info, content


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require_runner_stage(output_root: Path, runner_temp: str) -> None:
    """An explicit, pre-created direct child of the trusted runner temp only."""
    need(bool(runner_temp), "runner_temp_missing")
    trusted = Path(runner_temp)
    need(trusted.is_dir() and not trusted.is_symlink(), "runner_temp_invalid")
    need(output_root.is_dir() and not output_root.is_symlink() and
         output_root.resolve().parent == trusted.resolve(),
         "patch_stage_outside_runner_temp")


def stage_validated_patch(*, builder: Path, validator: Path, state: Path,
                          output_root: Path, source_sha: str, base_sha: str,
                          produced_sha: str, patch_sha256: str,
                          correlation_id: str, passed: int, total: int,
                          regression_passed: int) -> dict:
    """Stage only an already independently validated candidate; never infer success."""
    need(all(type(x) is str and SHA40.fullmatch(x) for x in
             (source_sha, base_sha, produced_sha)), "sha_invalid")
    need(base_sha == source_sha and produced_sha != base_sha, "not_a_real_patch")
    need(type(patch_sha256) is str and bool(SHA256.fullmatch(patch_sha256)),
         "patch_digest_invalid")
    need(type(correlation_id) is str and
         bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{5,100}", correlation_id)),
         "correlation_invalid")
    need(passed == total == 42 and regression_passed >= 52,
         "independent_tests_not_proven")
    need(output_root.is_dir() and not output_root.is_symlink(),
         "stage_root_unavailable")
    need(all(p.is_dir() and not p.is_symlink() for p in (builder, validator, state)),
         "workspace_invalid")
    need(output_root.resolve() != builder.resolve() and
         output_root.resolve() != validator.resolve() and
         output_root.resolve() != state.resolve(), "stage_root_alias")
    need(git(builder, "rev-parse", "HEAD") == produced_sha and
         git(validator, "rev-parse", "HEAD") == produced_sha,
         "validator_commit_mismatch")
    need(git(builder, "remote") == "", "builder_remote_present")
    need(git(builder, "status", "--porcelain", "--untracked-files=all") == "" and
         git(validator, "status", "--porcelain", "--untracked-files=all") == "",
         "checkout_dirty")
    need(git(builder, "merge-base", base_sha, produced_sha) == base_sha,
         "commit_not_derived_from_base")
    need(git(builder, "diff", "--name-only", base_sha, produced_sha) == TARGET,
         "patch_scope_invalid")
    need(git(validator, "diff", "--name-only", base_sha, produced_sha) == TARGET,
         "validator_scope_invalid")
    patch_path = state / "change.patch"
    need(patch_path.is_file() and not patch_path.is_symlink(), "patch_missing")
    patch = patch_path.read_bytes()
    need(0 < len(patch) <= MAX_PATCH and _sha(patch) == patch_sha256,
         "patch_bytes_invalid")
    delta = (git(builder, "diff", base_sha, produced_sha, "--", TARGET) + "\n").encode()
    need(bool(delta) and _sha(delta) == patch_sha256,
         "patch_not_matching_commit")
    need((builder / TARGET).read_bytes() == (validator / TARGET).read_bytes(),
         "validator_source_mismatch")
    with tempfile.TemporaryDirectory(prefix="validated-bundle-") as work:
        bundle_path = Path(work) / "result.bundle"
        git(builder, "bundle", "create", str(bundle_path), "HEAD", "^" + base_sha)
        bundle = bundle_path.read_bytes()
    need(0 < len(bundle) <= MAX_BUNDLE, "bundle_size_invalid")
    receipt = {
        "schema": "native-validated-patch-stage-v1",
        "source_sha": source_sha,
        "base_sha": base_sha,
        "produced_sha": produced_sha,
        "patch_sha256": patch_sha256,
        "bundle_sha256": _sha(bundle),
        "target": TARGET,
        "correlation_id": correlation_id,
        "tests_passed": 42,
        "tests_total": 42,
        "regression_passed": regression_passed,
        "independent_validator": True,
        "stage_only_not_durable": True,
        "ready_for_pr": False,
    }
    receipt_bytes = (json.dumps(receipt, sort_keys=True, separators=(",", ":"))
                     + "\n").encode("utf-8")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, data in [
            ("receipt.json", receipt_bytes), ("change.patch", patch),
            ("result.bundle", bundle),
        ]:
            zi, payload = _entry(name, data)
            zf.writestr(zi, payload)
    artifact = out.getvalue()
    digest = _sha(artifact)
    dest = output_root / (digest + ".zip")
    if dest.exists():
        need(dest.is_file() and not dest.is_symlink() and
             dest.read_bytes() == artifact, "stage_cas_conflict")
        replayed = True
    else:
        try:
            with dest.open("xb") as stream:
                stream.write(artifact)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            need(dest.is_file() and not dest.is_symlink() and
                 dest.read_bytes() == artifact, "stage_cas_conflict")
            replayed = True
        else:
            replayed = False
    need(dest.is_file() and not dest.is_symlink() and
         _sha(dest.read_bytes()) == digest, "stage_readback_failed")
    return {
        "result": "NATIVE_PATCH_STAGED_NOT_DURABLE",
        "artifact_sha256": digest,
        "artifact_size": len(artifact),
        "artifact_path": str(dest),
        "produced_sha": produced_sha,
        "replayed": replayed,
        "ready_for_pr": False,
    }
