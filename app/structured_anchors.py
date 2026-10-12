"""Candidate-bound line anchors for model revisions; no source matching by guessed text."""
from __future__ import annotations

import ast
import json

from app.structured_diagnostics import RevisionTrace

NEW_PATTERN = r"^[^\[\]]*$"
HEADER_NEW_PATTERN = r"^[^\[\]]*:[ \n]*$"
GUARD_EXPRESSION_PATTERN = r"^[^\[\]\r\n\t\x00]+$"


class AnchorRejected(ValueError):
    """Closed reason codes, with optional source-free anchor failure classification."""

    def __init__(self, code: str, *, anchor_failure_kind: str | None = None):
        super().__init__(code)
        self.anchor_failure_kind = anchor_failure_kind


def require(ok: bool, code: str) -> None:
    if not ok:
        raise AnchorRejected(code)


def line_catalog(source: str) -> dict[str, int]:
    require(type(source) is str and 1 <= len(source.encode()) <= 20000,
            "revision_size")
    lines = source.splitlines(keepends=True)
    require(len(lines) >= 2 and len(lines) <= 350, "revision_anchor_source")
    require(lines[0].startswith("def ") and lines[0].rstrip().endswith(":"),
            "revision_anchor_source")
    require(all(not line.strip() or line.startswith("    ") for line in lines[1:]),
            "revision_anchor_source")
    return {f"L{number}": number - 1 for number, line in enumerate(lines, 1)
            if number > 1 and line.strip()}


def anchored_schema(source: str) -> dict:
    anchors = line_catalog(source)
    require(bool(anchors), "revision_anchor_source")
    lines = source.splitlines(keepends=True)
    headers = [anchor for anchor, index in anchors.items()
               if lines[index].rstrip().endswith(":")]
    ordinary = [anchor for anchor in anchors if anchor not in headers]
    guardable = [anchor for anchor, index in anchors.items()
                 if lines[index].lstrip().startswith("if ")
                 and lines[index].rstrip().endswith(":")]
    variants = []
    for permitted, pattern in ((headers, HEADER_NEW_PATTERN), (ordinary, NEW_PATTERN)):
        if permitted:
            variants.append({
                "type": "object",
                "properties": {
                    "anchor": {"type": "string", "enum": permitted},
                    "new": {"type": "string", "maxLength": 6000, "pattern": pattern},
                },
                "required": ["anchor", "new"], "additionalProperties": False,
            })
    if guardable:
        variants.append({
            "type": "object",
            "properties": {
                "anchor": {"type": "string", "enum": guardable},
                "guard": {"type": "string", "minLength": 1, "maxLength": 2000,
                          "pattern": GUARD_EXPRESSION_PATTERN},
                "return_expression": {"type": "string", "minLength": 1,
                                      "maxLength": 2000,
                                      "pattern": GUARD_EXPRESSION_PATTERN},
            },
            "required": ["anchor", "guard", "return_expression"],
            "additionalProperties": False,
        })
    return {
        "type": "object",
        "properties": {"edits": {
            "type": "array", "minItems": 1, "maxItems": 6,
            "items": {"oneOf": variants} if len(variants) > 1 else variants[0],
        }},
        "required": ["edits"], "additionalProperties": False,
    }



def inference_schema(source: str) -> dict:
    """Compact output grammar for local generation; assembly remains authoritative.

    Ollama's nested oneOf schema has three disjoint edit variants and caused
    revision generation to remain unfinished until the governed time limit.
    The small envelope constrains anchors, sizes and character patterns.
    assemble_anchors independently enforces exact edit shapes, header syntax,
    guardable lines, allowed AST, signature and the fixed acceptance contract.
    """
    anchors = line_catalog(source)
    require(bool(anchors), "revision_anchor_source")
    return {
        "type": "object",
        "properties": {"edits": {
            "type": "array", "minItems": 1, "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "anchor": {"type": "string", "enum": list(anchors)},
                    "new": {"type": "string", "maxLength": 6000,
                            "pattern": NEW_PATTERN},
                    "guard": {"type": "string", "minLength": 1,
                              "maxLength": 2000, "pattern": GUARD_EXPRESSION_PATTERN},
                    "return_expression": {"type": "string", "minLength": 1,
                                          "maxLength": 2000,
                                          "pattern": GUARD_EXPRESSION_PATTERN},
                },
                "required": ["anchor"],
                "additionalProperties": False,
            },
        }},
        "required": ["edits"], "additionalProperties": False,
    }



def lightweight_anchor_schema(source: str) -> dict:
    """Bind line IDs in generation; leave source validation to the strict assembler.

    Regexes and string-length restrictions in the previous generation schema
    imposed excessive decoding cost on CPU-only 7B inference. The assembled
    response is still checked against the complete immutable contract.
    """
    schema = inference_schema(source)
    props = schema["properties"]["edits"]["items"]["properties"]
    for name in ("new", "guard", "return_expression"):
        props[name] = {"type": "string"}
    return schema



def replacement_only_schema(source: str) -> dict:
    """Generate only exact line replacements; reject typed guards at the model boundary.

    Keep the independently validated guard-edit form available to the assembler
    for compatibility, but do not offer it to the model in this repair phase.
    """
    schema = lightweight_anchor_schema(source)
    edits = schema["properties"]["edits"]["items"]
    edits["properties"] = {name: edits["properties"][name] for name in ("anchor", "new")}
    edits["required"] = ["anchor", "new"]
    return schema


def guard_return_literals(source: str) -> list[str]:
    """Finite, source-bound literal returns for typed guards, never a fallback.

    The model may choose only an already-existing constant return expression.
    AST validation is repeated at the trusted assembly boundary.
    """
    line_catalog(source)
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        raise AnchorRejected("revision_anchor_source") from None
    require(len(tree.body) == 1 and type(tree.body[0]) is ast.FunctionDef,
            "revision_anchor_source")
    returns = sorted((node for node in ast.walk(tree.body[0])
                      if type(node) is ast.Return),
                     key=lambda node: (node.lineno, node.col_offset))
    literals: list[str] = []
    for node in returns:
        if type(node.value) is ast.Constant:
            value = node.value.value
            if value is None or type(value) in (str, bool, int, float):
                literal = ast.unparse(node.value)
                if len(literal.encode()) <= 128 and literal not in literals:
                    literals.append(literal)
    return literals[:16]


def model_split_schema(source: str) -> dict:
    """Constrained revision envelope: existing if-header IDs only for typed guards."""
    anchors = line_catalog(source)
    require(bool(anchors), "revision_anchor_source")
    lines = source.splitlines(keepends=True)
    guardable = [anchor for anchor, index in anchors.items()
                 if lines[index].lstrip().startswith("if ")
                 and lines[index].rstrip().endswith(":")]
    return_literals = guard_return_literals(source)
    return {
        "type": "object",
        "properties": {
            "edits": {"type": "array", "minItems": 0, "maxItems": 6,
                      "items": {"type": "object",
                                "properties": {
                                    "anchor": {"type": "string", "enum": list(anchors)},
                                    "new": {"type": "string"}},
                                "required": ["anchor", "new"],
                                "additionalProperties": False}},
            "guards": {"type": "array", "minItems": 0,
                       "maxItems": min(6, len(guardable)) if return_literals else 0,
                       "items": {"type": "object",
                                 "properties": {
                                     "anchor": ({"type": "string", "enum": guardable}
                                                if guardable else {"type": "string"}),
                                     "guard": {"type": "string"},
                                     "return_expression": (
                                         {"type": "string", "enum": return_literals}
                                         if return_literals else {"type": "string"})},
                                 "required": ["anchor", "guard", "return_expression"],
                                 "additionalProperties": False}},
        },
        "required": ["edits", "guards"],
        "additionalProperties": False,
    }


def assemble_anchors(reply: str, source: str, *, model_split: bool = False) -> tuple[str, list[dict]]:
    anchors = line_catalog(source)
    require(type(reply) is str and len(reply.encode()) <= 16384, "revision_size")
    try:
        value = json.loads(reply)
    except ValueError:
        raise AnchorRejected("revision_json") from None
    require(type(model_split) is bool and type(value) is dict, "revision_schema")
    if model_split:
        require(set(value) == {"edits", "guards"}
                and type(value["edits"]) is list and type(value["guards"]) is list
                and 1 <= len(value["edits"]) + len(value["guards"]) <= 6,
                "revision_schema")
        for edit in value["edits"]:
            require(type(edit) is dict and set(edit) == {"anchor", "new"},
                    "revision_edit_schema")
        for guard in value["guards"]:
            require(type(guard) is dict and
                    set(guard) == {"anchor", "guard", "return_expression"},
                    "revision_edit_schema")
        edits = value["edits"] + value["guards"]
    else:
        require(set(value) == {"edits"} and type(value["edits"]) is list
                and 1 <= len(value["edits"]) <= 6, "revision_schema")
        edits = value["edits"]
    lines = source.splitlines(keepends=True)
    replacements = {}
    for edit in edits:
        require(type(edit) is dict, "revision_edit_schema")
        # A typed early-return guard cannot remove an existing block header.
        # Expressions are supplied by the model; the assembler supplies syntax only.
        if set(edit) == {"anchor", "guard", "return_expression"}:
            anchor = edit["anchor"]
            condition, value = edit["guard"], edit["return_expression"]
            require(type(anchor) is str and type(condition) is str
                    and type(value) is str and 1 <= len(condition) <= 2000
                    and 1 <= len(value) <= 2000,
                    "revision_edit_schema")
            if anchor not in anchors:
                raise AnchorRejected("revision_anchor_invalid",
                                     anchor_failure_kind="unknown_anchor")
            index = anchors[anchor]
            require(index not in replacements, "revision_anchor_duplicate")
            original = lines[index]
            if not (original.lstrip().startswith("if ")
                    and original.rstrip().endswith(":")):
                raise AnchorRejected("revision_anchor_invalid",
                                     anchor_failure_kind="guard_not_if_header")
            require(all(char not in condition and char not in value
                        for char in "\r\n\t\0[]"), "revision_indentation")
            if model_split:
                # "return ''" is a statement, not an expression. Source-bound
                # constants and AST parsing keep Python keywords out of values.
                # Never silently remap an invalid model reply or invent a fallback.
                try:
                    guard_ast = ast.parse(condition, mode="eval")
                    return_ast = ast.parse(value, mode="eval")
                except (SyntaxError, ValueError, RecursionError):
                    raise AnchorRejected("revision_edit_schema") from None
                literal = ast.unparse(return_ast.body)
                require(type(return_ast.body) is ast.Constant
                        and literal in guard_return_literals(source),
                        "revision_edit_schema")
                condition = ast.unparse(guard_ast.body)
                value = literal
            indent = original[:len(original) - len(original.lstrip(" "))]
            replacements[index] = (indent + "if " + condition + ":\n"
                                   + indent + "    return " + value + "\n"
                                   + original)
            continue
        require(set(edit) == {"anchor", "new"}
                and type(edit["anchor"]) is str and type(edit["new"]) is str
                and len(edit["new"]) <= 6000, "revision_edit_schema")
        anchor, new = edit["anchor"], edit["new"]
        if anchor not in anchors:
            raise AnchorRejected("revision_anchor_invalid",
                                 anchor_failure_kind="unknown_anchor")
        index = anchors[anchor]
        require(index not in replacements, "revision_anchor_duplicate")
        require("\r" not in new and "\t" not in new and "\0" not in new,
                "revision_indentation")
        added = new.splitlines()
        require(not added or (added[0] and not added[0][0].isspace()),
                "revision_indentation")
        # Do not orphan the existing indented suite by removing its block opener.
        original = lines[index]
        if original.rstrip().endswith(":"):
            require(bool(added) and added[-1].rstrip().endswith(":"),
                    "revision_block_header")
        indent = original[:len(original) - len(original.lstrip(" "))]
        replacement = "".join(indent + line + "\n" for line in added)
        # A no-op entry is harmless; only a wholly unchanged candidate is rejected.
        replacements[index] = replacement
    for index, replacement in replacements.items():
        lines[index] = replacement
    assembled = "".join(lines)
    require(assembled != source, "candidate_repeated")
    require(len(assembled.encode()) <= 20000, "revision_size")
    return assembled, edits


def apply_anchored_edits(reply: str, before: str, proposal: dict, *, observer=None,
                         model_strict: bool = False,
                         model_split: bool = False) -> tuple[str, list[dict]]:
    from app.structured_code_worker import (
        proposal_spec, candidate_fingerprint, extracted, render, need, NODES, StructuredBlocked,
    )
    trace = RevisionTrace(reply, observer)
    try:
        task = proposal_spec(proposal)
        need("feedback" in proposal, "revision_feedback_missing")
        need(candidate_fingerprint(before, task) == proposal["feedback"]["candidate_sha256"],
             "feedback_candidate_mismatch")
        source = extracted(before, task)
        assembled, edits = assemble_anchors(reply, source, model_split=model_split)
        need(type(model_strict) is bool and type(model_split) is bool,
             "revision_edit_schema")
        if model_split:
            need(model_strict, "revision_edit_schema")
        if model_strict:
            # Model-generated replacements cannot create or orphan suites:
            # one original line becomes one complete line. New conditional
            # guards use the already supported typed edit instead, whose
            # indentation/body are assembled deterministically.
            catalog = line_catalog(source)
            lines = source.splitlines()
            for edit in edits:
                if set(edit) == {"anchor", "new"}:
                    old = lines[catalog[edit["anchor"]]]
                    new = edit["new"]
                    require(len(new.splitlines()) == 1 and bool(new.strip())
                            and (new.rstrip().endswith(":")
                                 == old.rstrip().endswith(":")),
                            "revision_block_header")
        trace.before_guard(assembled, NODES)
        candidate = render(before, task, json.dumps({"function": assembled}))
        need(candidate_fingerprint(candidate, task) != proposal["feedback"]["candidate_sha256"],
             "candidate_repeated")
    except AnchorRejected as error:
        trace.rejected(str(error), anchor_failure_kind=error.anchor_failure_kind)
        raise StructuredBlocked(str(error)) from None
    except StructuredBlocked as error:
        trace.rejected(str(error))
        raise
    trace.accepted()
    return json.dumps({"function": extracted(candidate, task)}, ensure_ascii=False), edits
