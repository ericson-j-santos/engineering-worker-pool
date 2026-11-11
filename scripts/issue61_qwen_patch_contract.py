"""Closed contract for a model-authored, single-line Python regex repair.

No eval, exec, subprocess or import of generated code. The returned code is AST
inspected and the only literal regex argument is evaluated against held-out tests.
This is a narrowly scoped Builder probe, NOT a Validator authorization.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re


ACCEPT_VALID = ("TODO-1", "TODO-24", "TODO-54", "TODO-999", "TODO-123456")
ACCEPT_INVALID = (
    "TODO-0", "TODO-00", "TODO-01", "TODO-54-validation", "TODO-54/..",
    "todo-54", "TODO-54\n", "TODO--1", "TODO-54 ", "", "TODO-+1",
)


class PatchRejected(ValueError):
    """Only fixed error codes, never generated source, go in CI logs."""


def source_digest(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def extract_model_patch(reply: str) -> tuple[str, str]:
    if not isinstance(reply, str) or len(reply) > 1500:
        raise PatchRejected("MODEL_JSON_SIZE_INVALID")
    try:
        data = json.loads(reply)
    except (ValueError, TypeError) as exc:
        raise PatchRejected("MODEL_JSON_INVALID") from exc
    if not isinstance(data, dict) or set(data) != {"replacement_line"}:
        raise PatchRejected("MODEL_SCHEMA_INVALID")
    line = data["replacement_line"]
    if not isinstance(line, str) or len(line) > 155 or "\n" in line or "\r" in line:
        raise PatchRejected("MODEL_CODE_FORMAT_INVALID")
    try:
        tree = ast.parse(line, mode="exec")
    except SyntaxError as exc:
        raise PatchRejected("MODEL_CODE_SYNTAX_INVALID") from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assign):
        raise PatchRejected("MODEL_CODE_ASSIGNMENT_INVALID")
    stmt = tree.body[0]
    if (len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name)
            or stmt.targets[0].id != "TODO_ID"):
        raise PatchRejected("MODEL_CODE_TARGET_NOT_ALLOWED")
    expr = stmt.value
    if (not isinstance(expr, ast.Call)
            or not isinstance(expr.func, ast.Attribute)
            or not isinstance(expr.func.value, ast.Name)
            or expr.func.value.id != "re" or expr.func.attr != "compile"
            or len(expr.args) != 1 or expr.keywords
            or not isinstance(expr.args[0], ast.Constant)
            or not isinstance(expr.args[0].value, str)):
        raise PatchRejected("MODEL_CODE_CALL_NOT_ALLOWED")
    pattern = expr.args[0].value
    if len(pattern) > 90 or not pattern.startswith(("TODO-", "^TODO-", r"\ATODO-")):
        raise PatchRejected("MODEL_PATTERN_SCOPE_INVALID")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise PatchRejected("MODEL_PATTERN_SYNTAX_INVALID") from exc
    bad_valid = tuple(x for x in ACCEPT_VALID if regex.fullmatch(x) is None)
    bad_invalid = tuple(x for x in ACCEPT_INVALID if regex.fullmatch(x) is not None)
    if bad_valid or bad_invalid:
        raise PatchRejected("MODEL_REPAIR_ACCEPTANCE_FAILED")
    return line, pattern
