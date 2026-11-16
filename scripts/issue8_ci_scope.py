"""Fail-closed applicability selector for the live issue8 GitHub+Qwen PR gate."""
from __future__ import annotations

import os
from pathlib import PurePosixPath
import re
import subprocess
import sys

SHA40 = re.compile(r"[0-9a-f]{40}\Z")
EXACT = frozenset({
    ".github/workflows/ci.yml",
    "requirements.txt",
    "requirements-rag.txt",
    "app/agent_rag_consumer.py",
    "app/native_structured_ollama.py",
    "app/structured_code_worker.py",
    "docs/ISSUE_QWEN7B_DRAFT_HANDOFF.md",
})
PREFIXES = ("app/issue_", "scripts/issue_", "tests/test_issue_", "tests/test_issue8_", "tests/issue_rag_")


class ScopeBlocked(RuntimeError):
    """Sanitized failure: an uncertain gate must never be skipped."""


def issue8_applicable(paths: list[str]) -> bool:
    if type(paths) is not list or not paths or len(paths) > 5000:
        raise ScopeBlocked("issue8_diff_invalid")
    observed: set[str] = set()
    for name in paths:
        if (type(name) is not str or not name or len(name) > 512
                or name.startswith("/") or "\\" in name or "\x00" in name
                or any(ord(char) < 32 for char in name)
                or name.startswith("../") or "/../" in name
                or name in observed or PurePosixPath(name).as_posix() != name):
            raise ScopeBlocked("issue8_diff_invalid")
        observed.add(name)
    return any(name in EXACT or name.startswith(PREFIXES) for name in paths)


def main() -> int:
    base = os.environ.get("PR_BASE_SHA", "")
    head = os.environ.get("EXPECTED_SHA", "")
    output = os.environ.get("GITHUB_OUTPUT", "")
    if (not SHA40.fullmatch(base) or not SHA40.fullmatch(head)
            or base == head or not output or os.environ.get("GITHUB_ACTIONS") != "true"):
        raise ScopeBlocked("issue8_source_identity_invalid")
    try:
        subprocess.run(["git", "fetch", "--no-tags", "--depth=1", "origin", base],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=25)
        actual = subprocess.run(["git", "diff", "--name-only", "-z", base, head],
                                check=True, capture_output=True, timeout=12).stdout
        if not actual.endswith(b"\x00"):
            raise ScopeBlocked("issue8_diff_invalid")
        paths = [part.decode("utf-8") for part in actual[:-1].split(b"\x00")]
        applicable = issue8_applicable(paths)
        with open(output, "a", encoding="utf-8") as stream:
            stream.write("applicable=" + ("true" if applicable else "false") + "\n")
        print("ISSUE8_E2E_APPLICABLE" if applicable else "ISSUE8_E2E_NOT_APPLICABLE")
        return 0
    except ScopeBlocked:
        raise
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError, UnicodeError, ValueError):
        raise ScopeBlocked("issue8_diff_unavailable") from None


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ScopeBlocked as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
