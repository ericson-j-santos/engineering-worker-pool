"""Independent, read-only Git candidate inspection for an existing pre-PR flow.

This is NOT an authenticated Builder, durable CAS, formal Validator or READY_FOR_PR.
The caller must independently authenticate the issue and GitHub branch observations.
No remote fetch, branch publication, task claim or state mutation is performed here.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")
MAX_BYTES = 4 * 1024 * 1024
MAX_PATHS = 32


class CandidateBlocked(ValueError):
    """Sanitized, closed-set candidate inspection rejection."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise CandidateBlocked(code)


def _path(value: str) -> str:
    _require(type(value) is str and 0 < len(value) <= 240
             and "\x00" not in value and "\\" not in value
             and "\n" not in value and "\r" not in value,
             "candidate_path_invalid")
    path = PurePosixPath(value)
    _require(not path.is_absolute() and path.as_posix() == value
             and all(part not in ("", ".", "..") for part in value.split("/"))
             and not value.startswith("-"), "candidate_path_invalid")
    return value


def _git(root: Path, *args: str) -> bytes:
    """Only fixed read operations; never execute strings supplied as commands."""
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "LC_ALL": "C",
    }
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, check=False,
            timeout=15, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CandidateBlocked("candidate_git_unavailable") from None
    _require(result.returncode == 0 and len(result.stdout) <= MAX_BYTES,
             "candidate_git_read_failed")
    return result.stdout


@dataclass(frozen=True)
class CandidateSpec:
    repository: str
    base_sha: str
    head_sha: str
    observed_main_sha: str
    observed_branch_sha: str
    allowed_paths: tuple[str, ...]
    expected_paths: tuple[str, ...]

    def validate(self) -> None:
        _require(type(self.repository) is str and bool(REPO.fullmatch(self.repository)),
                 "candidate_repository_invalid")
        _require(all(type(s) is str and bool(SHA.fullmatch(s)) for s in (
            self.base_sha, self.head_sha, self.observed_main_sha,
            self.observed_branch_sha,
        )), "candidate_sha_invalid")
        _require(self.base_sha != self.head_sha
                 and self.base_sha == self.observed_main_sha
                 and self.head_sha == self.observed_branch_sha,
                 "candidate_authoritative_head_mismatch")
        for paths in (self.allowed_paths, self.expected_paths):
            _require(type(paths) is tuple and 0 < len(paths) <= MAX_PATHS,
                     "candidate_scope_invalid")
            _require(paths == tuple(sorted(set(paths)))
                     and all(_path(path) == path for path in paths),
                     "candidate_scope_invalid")
        _require(set(self.expected_paths).issubset(self.allowed_paths),
                 "candidate_scope_invalid")


def inspect_candidate(root: Path, spec: CandidateSpec) -> dict:
    """Verify exact clean Git content, ancestry, paths and patch hash.

    The GitHub main/branch SHAs in spec MUST be supplied from a trusted,
    contemporaneous API read by the caller. No result can authorize a PR.
    """
    _require(type(spec) is CandidateSpec, "candidate_contract_invalid")
    spec.validate()
    _require(isinstance(root, Path) and root.is_dir()
             and not root.is_symlink(), "candidate_checkout_invalid")
    root = root.resolve()
    _require(Path(_git(root, "rev-parse", "--show-toplevel").decode().strip())
             .resolve() == root, "candidate_checkout_invalid")
    _require(_git(root, "rev-parse", "HEAD").decode().strip()
             == spec.head_sha, "candidate_checkout_head_mismatch")
    _require(not _git(root, "status", "--porcelain=v1", "-z",
                      "--untracked-files=all"), "candidate_checkout_dirty")
    _require(_git(root, "cat-file", "-t", spec.base_sha).strip() == b"commit"
             and _git(root, "cat-file", "-t", spec.head_sha).strip() == b"commit",
             "candidate_commit_invalid")
    _git(root, "merge-base", "--is-ancestor", spec.base_sha, spec.head_sha)
    raw_names = _git(root, "diff", "--no-ext-diff", "--no-renames",
                     "--name-only", "-z", spec.base_sha, spec.head_sha, "--")
    try:
        names = tuple(sorted(part.decode("utf-8") for part in raw_names.split(b"\0")
                             if part))
    except UnicodeError:
        raise CandidateBlocked("candidate_path_invalid") from None
    _require(names == spec.expected_paths and len(names) == len(set(names)),
             "candidate_changed_scope_mismatch")
    patch = _git(root, "diff", "--no-ext-diff", "--no-renames", "--binary",
                 spec.base_sha, spec.head_sha, "--")
    _require(bool(patch), "candidate_patch_empty")
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    for path in names:
        _path(path)
        entry = _git(root, "ls-tree", "-z", spec.head_sha, "--", path)
        _require(entry.startswith(b"100644 blob ")
                 and entry.endswith(b"\t" + path.encode("utf-8") + b"\0")
                 and entry.count(b"\0") == 1,
                 "candidate_file_type_invalid")
        old = _git(root, "show", f"{spec.base_sha}:{path}")
        new = _git(root, "show", f"{spec.head_sha}:{path}")
        _require(old != new, "candidate_file_unchanged")
        before[path] = hashlib.sha256(old).hexdigest()
        after[path] = hashlib.sha256(new).hexdigest()
    # This report is evidence of source inspection only. It must not claim
    # artifact durability, model invocation, Validator or real-world E2E.
    return {
        "result": "CANDIDATE_INSPECTED",
        "repository": spec.repository,
        "base_sha": spec.base_sha,
        "head_sha": spec.head_sha,
        "changed_paths": list(names),
        "patch_sha256": hashlib.sha256(patch).hexdigest(),
        "before_sha256": before,
        "after_sha256": after,
        "read_only": True,
        "model_calls_verified": False,
        "cas_artifact_ref": None,
        "validator_independent_verified": False,
        "ready_for_pr": False,
    }
