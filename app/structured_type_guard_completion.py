"""Bounded AST-derived type-guard search, accepted only by the trusted validator.

No task-specific code, model response, solution fixture or additional LLM call
is used. Failure to prove every supplied case leaves the candidate rejected.
"""
from __future__ import annotations

import ast
import json

from app.structured_anchors import (
    AnchorRejected, assemble_anchors, guard_return_literals, statement_anchors,
)


MAX_GUARD_TRIALS = 12


def _guard_target(value: ast.expr) -> str | None:
    """Use only local names or one dict.get call with an immutable string key."""
    if type(value) is ast.Name:
        return value.id if not value.id.startswith("__") else None
    if type(value) is not ast.Call or type(value.func) is not ast.Attribute:
        return None
    func = value.func
    if (func.attr != "get" or type(func.value) is not ast.Name
            or func.value.id.startswith("__") or value.keywords
            or not 1 <= len(value.args) <= 2 or type(value.args[0]) is not ast.Constant
            or type(value.args[0].value) is not str
            or len(value.args[0].value) > 128):
        return None
    if len(value.args) == 2 and (
            type(value.args[1]) is not ast.Constant
            or value.args[1].value is not None
               and type(value.args[1].value) is not str):
        return None
    return func.value.id + ".get(" + repr(value.args[0].value) + ")"


def _guard_sites(function: str) -> list[tuple[str, str]]:
    """Generate only minimal type checks based on source AST, never text guesses."""
    allowed = set(statement_anchors(function))
    tree = ast.parse(function)
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(node: ast.stmt, condition: str | None) -> None:
        anchor = f"L{node.lineno}"
        if condition is not None and anchor in allowed:
            item = (anchor, condition)
            if item not in seen:
                seen.add(item)
                candidates.append(item)

    for node in ast.walk(tree.body[0]):
        if type(node) in (ast.Assign, ast.Return, ast.Expr):
            for child in ast.walk(node):
                if (type(child) is ast.Call and type(child.func) is ast.Attribute
                        and child.func.attr == "strip" and not child.args
                        and not child.keywords):
                    subject = _guard_target(child.func.value)
                    if subject:
                        add(node, "not isinstance(" + subject + ", str)")
        elif type(node) is ast.If and node.orelse:
            # Optional string data with an alternative branch must reject
            # an invalid non-None value instead of silently taking fallback.
            for child in ast.walk(node.test):
                if (type(child) is ast.Call and type(child.func) is ast.Name
                        and child.func.id == "isinstance"
                        and len(child.args) == 2 and not child.keywords
                        and type(child.args[1]) is ast.Name
                        and child.args[1].id == "str"):
                    subject = _guard_target(child.args[0])
                    if subject and ".get(" in subject:
                        add(node, subject + " is not None and not isinstance("
                            + subject + ", str)")
    return candidates[:MAX_GUARD_TRIALS]


def complete_checked_type_guard(candidate: str, task: dict, failed: dict
                                ) -> tuple[str, dict, dict] | None:
    """Produce a full-pass candidate or None; never write or accept partial fixes."""
    from app.structured_code_worker import (
        StructuredBlocked, extracted, render, validate, validate_report,
        task_spec, sha256,
    )
    task_spec(task)
    validate_report(failed, task)
    if failed["passed"] == failed["total"]:
        return None

    function = extracted(candidate, task)
    source_hash = sha256(candidate.encode())
    literals = guard_return_literals(function)
    if not literals:
        return None

    trials = 0
    for anchor, condition in _guard_sites(function):
        for literal in literals:
            if trials >= MAX_GUARD_TRIALS:
                return None
            trials += 1
            raw = json.dumps({"edits": [], "guards": [{
                "anchor": anchor, "guard": condition,
                "return_expression": literal,
            }]})
            try:
                assembled, _ = assemble_anchors(raw, function, model_split=True)
                revised = render(candidate, task, json.dumps({"function": assembled}))
                checked = validate(revised, task)
            except (AnchorRejected, StructuredBlocked):
                continue
            if (checked["passed"] == checked["total"] and checked["errors"] == 0
                    and checked["failures"] == [] and revised != candidate):
                proof = {
                    "result": "AST_GUARD_COMPLETED",
                    "strategy": "bounded_source_derived_type_guard",
                    "trials": trials,
                    "before_sha256": source_hash,
                    "after_sha256": sha256(revised.encode()),
                    "guard_sha256": sha256(condition.encode()),
                    "validated_cases": checked["total"],
                }
                return revised, checked, proof
    return None
