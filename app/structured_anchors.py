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



def statement_anchors(source: str) -> list[str]:
    """Enumerate AST statement starts, never guessed text or continuation lines."""
    anchors = line_catalog(source)
    lines = source.splitlines()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        raise AnchorRejected("revision_anchor_source") from None
    require(len(tree.body) == 1 and type(tree.body[0]) is ast.FunctionDef,
            "revision_anchor_source")
    locations = set()
    for node in ast.walk(tree.body[0]):
        if type(node) not in (ast.If, ast.For, ast.Assign, ast.Return, ast.Expr):
            continue
        number = node.lineno
        anchor = f"L{number}"
        if anchor in anchors:
            line = lines[number - 1]
            # AST treats elif as If, but a new line before it breaks the clause.
            if (node.col_offset == len(line) - len(line.lstrip(" "))
                    and not line.lstrip().startswith(("elif ", "elif("))):
                locations.add(anchor)
    return [anchor for anchor in anchors if anchor in locations]


def validated_type_guard(node: ast.expr) -> str:
    """Restrict model-split guards to bounded, side-effect-free isinstance checks."""
    def subject(value: ast.expr) -> bool:
        if type(value) is ast.Name:
            return not value.id.startswith("__")
        if type(value) is not ast.Call:
            return False
        call = value.func
        return (type(call) is ast.Attribute and call.attr == "get"
                and type(call.value) is ast.Name
                and not call.value.id.startswith("__")
                and len(value.args) == 1 and not value.keywords
                and type(value.args[0]) is ast.Constant
                and type(value.args[0].value) is str
                and len(value.args[0].value) <= 128)

    def negative_isinstance(value: ast.expr) -> ast.expr | None:
        if type(value) is not ast.UnaryOp or type(value.op) is not ast.Not:
            return None
        call = value.operand
        if (type(call) is not ast.Call or type(call.func) is not ast.Name
                or call.func.id != "isinstance" or len(call.args) != 2
                or call.keywords or type(call.args[1]) is not ast.Name
                or call.args[1].id not in {"str", "dict", "list", "bool", "int", "float"}
                or not subject(call.args[0])):
            return None
        return call.args[0]

    if type(node) is ast.BoolOp and type(node.op) is ast.And and len(node.values) == 2:
        check, guard = node.values
        checked = negative_isinstance(guard)
        require(type(check) is ast.Compare and len(check.ops) == 1
                and type(check.ops[0]) is ast.IsNot
                and len(check.comparators) == 1
                and type(check.comparators[0]) is ast.Constant
                and check.comparators[0].value is None
                and checked is not None and subject(check.left)
                and ast.dump(check.left, include_attributes=False)
                    == ast.dump(checked, include_attributes=False),
                "revision_guard_expression")
    else:
        require(negative_isinstance(node) is not None, "revision_guard_expression")
    return ast.unparse(node)


def model_split_schema(source: str) -> dict:
    """Constrained model revision: guards bind to exact AST statement anchors."""
    anchors = line_catalog(source)
    require(bool(anchors), "revision_anchor_source")
    guardable = statement_anchors(source)
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
    guards = {}
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
            # Only model-split may pair one guard with one replacement.
            # Two of either kind remain ambiguous and are rejected.
            require(index not in guards and (model_split or index not in replacements),
                    "revision_anchor_duplicate")
            original = lines[index]
            old_if_header = (original.lstrip().startswith("if ")
                             and original.rstrip().endswith(":"))
            if not old_if_header and not (model_split and anchor in statement_anchors(source)):
                raise AnchorRejected("revision_anchor_invalid",
                                     anchor_failure_kind=("guard_not_statement" if model_split
                                                          else "guard_not_if_header"))
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
                condition = validated_type_guard(guard_ast.body)
                value = literal
            indent = original[:len(original) - len(original.lstrip(" "))]
            guards[index] = (indent + "if " + condition + ":\n"
                             + indent + "    return " + value + "\n")
            continue
        require(set(edit) == {"anchor", "new"}
                and type(edit["anchor"]) is str and type(edit["new"]) is str
                and len(edit["new"]) <= 6000, "revision_edit_schema")
        anchor, new = edit["anchor"], edit["new"]
        if anchor not in anchors:
            raise AnchorRejected("revision_anchor_invalid",
                                 anchor_failure_kind="unknown_anchor")
        index = anchors[anchor]
        require(index not in replacements and (model_split or index not in guards),
                "revision_anchor_duplicate")
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
    for index, prefix in guards.items():
        lines[index] = prefix + lines[index]
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
