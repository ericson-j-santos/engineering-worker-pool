"""Pure, non-executing Noteri #42 model-candidate assembly and source-diff gate.

A valid proposal is *not* a repair authorization, Git commit, or model proof.
Only one existing Python function may differ from a trusted source checkout.
No candidate is imported or executed in this component.
"""
from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from typing import Any

TARGET = "attest_installed_release"
PATH = "scripts/noteri_control_plane_watchdog_uac_launcher.py"
MAX_FILE = 96 * 1024
MAX_FUNCTION = 12 * 1024
BANNED_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint",
    "getattr", "setattr", "delattr", "globals", "locals", "vars", "print",
})
BANNED_METHODS = frozenset({
    "system", "Popen", "run", "call", "check_call", "check_output",
    "write_text", "write_bytes", "unlink", "rename", "replace", "mkdir",
    "rmdir", "chmod", "chown", "remove", "rmtree", "connect", "send",
    "execv", "spawnv", "startfile", "ShellExecuteW", "sleep",
})
BLOCKED_NODES = (
    ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.AsyncFunctionDef,
    ast.Lambda, ast.With, ast.AsyncWith, ast.Try, ast.TryStar, ast.While,
    ast.Await, ast.Yield, ast.YieldFrom, ast.ClassDef, ast.Delete,
)


class CandidateBlocked(RuntimeError):
    """Stable, non-sensitive refusal; model text never enters an error."""


def need(condition: bool, reason: str) -> None:
    if not condition:
        raise CandidateBlocked(reason)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse(raw: str, reason: str) -> ast.Module:
    try:
        return ast.parse(raw, mode="exec")
    except (SyntaxError, ValueError, TypeError, RecursionError):
        raise CandidateBlocked(reason) from None


def _target(module: ast.Module) -> tuple[int, ast.FunctionDef]:
    matches = [
        (idx, node) for idx, node in enumerate(module.body)
        if isinstance(node, ast.FunctionDef) and node.name == TARGET
    ]
    need(len(matches) == 1, "noteri42_target_ambiguous")
    return matches[0]


def _calls_safe(fn: ast.FunctionDef) -> None:
    for node in ast.walk(fn):
        need(not isinstance(node, BLOCKED_NODES),
             "noteri42_candidate_forbidden_syntax")
        if isinstance(node, ast.FunctionDef):
            need(node is fn, "noteri42_candidate_nested_function_forbidden")
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                need(target.id not in BANNED_CALLS,
                     "noteri42_candidate_privileged_call")
            elif isinstance(target, ast.Attribute):
                need(target.attr not in BANNED_METHODS
                     and not target.attr.startswith("__"),
                     "noteri42_candidate_privileged_call")
                # Only the existing trusted helper may indirectly read the OS.
                # The sole allowed subprocess operation formats arguments.
                parent = target.value
                if isinstance(parent, ast.Name):
                    if parent.id == "subprocess":
                        need(target.attr == "list2cmdline",
                             "noteri42_candidate_privileged_call")
                    need(parent.id not in {
                        "ctypes", "socket", "urllib", "requests",
                        "httpx", "shutil", "winreg",
                    }, "noteri42_candidate_privileged_call")
                if isinstance(parent, ast.Attribute):
                    if isinstance(parent.value, ast.Name) and parent.value.id == "os":
                        need(parent.attr == "path"
                             and target.attr in {"abspath", "normcase"},
                             "noteri42_candidate_privileged_call")
            else:
                raise CandidateBlocked("noteri42_candidate_dynamic_call_forbidden")


@dataclass(frozen=True)
class Candidate:
    source_sha256: str
    candidate_sha256: str
    function_sha256: str
    source_path: str
    changed: bool
    patched_bytes: bytes


def assemble(original: bytes, function: str) -> Candidate:
    """Assemble a candidate without writing anything; provenance is external."""
    need(type(original) is bytes and 0 < len(original) <= MAX_FILE,
         "noteri42_original_size")
    need(type(function) is str
         and 0 < len(function.encode("utf-8")) <= MAX_FUNCTION,
         "noteri42_function_size")
    try:
        decoded = original.decode("utf-8")
    except UnicodeError:
        raise CandidateBlocked("noteri42_original_encoding") from None
    need("\r" not in decoded and "\r" not in function,
         "noteri42_line_endings_unexpected")
    old_tree = _parse(decoded, "noteri42_original_syntax")
    index, old = _target(old_tree)
    need(not old.decorator_list and old.col_offset == 0
         and old.end_lineno is not None,
         "noteri42_original_function_shape")
    new_tree = _parse(function, "noteri42_candidate_syntax")
    need(len(new_tree.body) == 1
         and type(new_tree.body[0]) is ast.FunctionDef
         and new_tree.body[0].name == TARGET,
         "noteri42_candidate_shape")
    candidate = new_tree.body[0]
    need(not candidate.decorator_list
         and candidate.col_offset == 0
         and ast.dump(candidate.args, include_attributes=False)
             == ast.dump(old.args, include_attributes=False)
         and ast.dump(candidate.returns, include_attributes=False)
             == ast.dump(old.returns, include_attributes=False)
         and not getattr(candidate, "type_params", []),
         "noteri42_candidate_signature_drift")
    _calls_safe(candidate)
    # Existing checks must not disappear even if the model simplifies code.
    old_errors = {
        node.value for node in ast.walk(old)
        if isinstance(node, ast.Constant) and type(node.value) is str
        and node.value.startswith("WATCHDOG_INSTALLED_")
    }
    new_errors = {
        node.value for node in ast.walk(candidate)
        if isinstance(node, ast.Constant) and type(node.value) is str
        and node.value.startswith("WATCHDOG_INSTALLED_")
    }
    need(old_errors.issubset(new_errors), "noteri42_existing_guards_removed")
    need(ast.dump(candidate, include_attributes=False)
         != ast.dump(old, include_attributes=False),
         "noteri42_candidate_no_change")
    # The security property must be present in actual AST comparisons/calls;
    # this only screens candidates. A separate runtime Validator still needs
    # positive/negative/Windows regressions before PR authorization.
    literals = {
        node.value for node in ast.walk(candidate)
        if isinstance(node, ast.Constant) and type(node.value) is str
    }
    need({
        "action_count", "action_python_observed", "action_arguments_sha256",
        "runner_home",
    }.issubset(literals), "noteri42_action_guard_absent")
    lines = decoded.splitlines(keepends=True)
    patched = ("".join(lines[:old.lineno - 1])
               + function.rstrip("\n") + "\n"
               + "".join(lines[old.end_lineno:])).encode("utf-8")
    parsed = _parse(patched.decode("utf-8"), "noteri42_assembled_syntax")
    need(len(parsed.body) == len(old_tree.body),
         "noteri42_assembled_scope_changed")
    for pos, (before, after) in enumerate(zip(old_tree.body, parsed.body)):
        if pos != index:
            need(ast.dump(before, include_attributes=False)
                 == ast.dump(after, include_attributes=False),
                 "noteri42_assembled_scope_changed")
    need(patched != original, "noteri42_candidate_no_change")
    need(len(patched) <= MAX_FILE, "noteri42_assembled_size")
    return Candidate(
        source_sha256=digest(original),
        candidate_sha256=digest(patched),
        function_sha256=digest(function.encode("utf-8")),
        source_path=PATH,
        changed=True,
        patched_bytes=patched,
    )
