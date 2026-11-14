"""Read-only, fail-closed admission for one Desktop Ollama HTTP repair.

This profile does not run code, invoke an LLM, schedule jobs or publish Git.
It inspects candidate Python syntax before any separately governed execution.
Do not extend the generic Worker Pool structured-code grammar to support it.
"""
from __future__ import annotations

import ast
import hashlib

PROFILE = "desktop.ollama.http-transport.v1"
REPOSITORY = "ericson-j-santos/desktop-pc24x7-runtime"
PATH = "scripts/ollama_cpu_pilot.py"
FUNCTION = "http_json"
BASE_SHA = "b038a770117e0d3d4d1297a3f4bec112ff68a744"
MAX_SOURCE_BYTES = 262_144
EXCEPTIONS = (
    "urllib.error.HTTPError", "urllib.error.URLError", "TimeoutError",
    "ConnectionError", "OSError",
)
CODES = {
    "ollama_http_error", "ollama_timeout", "ollama_connection_error", "ollama_os_error",
}
ALLOWED_BODY_NODES = {
    ast.If, ast.Raise, ast.Call, ast.Compare, ast.BoolOp, ast.Name,
    ast.Attribute, ast.Constant, ast.JoinedStr, ast.FormattedValue,
    ast.And, ast.Or, ast.Is, ast.IsNot, ast.Lt, ast.LtE, ast.Gt,
    ast.GtE, ast.Eq, ast.NotEq, ast.Load,
}
ALLOWED_NAMES = {"exc", "PilotBlocked", "isinstance", "type", "int", "TimeoutError"}
ALLOWED_ATTRIBUTES = {"exc.code", "exc.reason"}
ALLOWED_CALLS = {"PilotBlocked", "type", "isinstance"}


class ProfileBlocked(ValueError):
    """Fixed reason codes; source code, prompts, secrets and tracebacks are omitted."""


def need(condition: bool, code: str) -> None:
    if not condition:
        raise ProfileBlocked(code)


def digest(source: bytes) -> str:
    return hashlib.sha256(source).hexdigest()


def parse(source: bytes) -> tuple[str, ast.Module]:
    need(type(source) is bytes and 0 < len(source) <= MAX_SOURCE_BYTES,
         "source_bytes_invalid")
    try:
        text = source.decode("utf-8")
        tree = ast.parse(text)
    except (UnicodeError, SyntaxError, ValueError, RecursionError):
        raise ProfileBlocked("source_syntax_invalid") from None
    need(type(tree) is ast.Module, "source_tree_invalid")
    return text, tree


def function(tree: ast.Module) -> ast.FunctionDef:
    funcs = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name == FUNCTION]
    need(len(funcs) == 1 and not funcs[0].decorator_list,
         "function_identity_invalid")
    return funcs[0]


def outer_transport_try(fn: ast.FunctionDef) -> ast.Try:
    matches = [node for node in fn.body if isinstance(node, ast.Try)
               and any(isinstance(child, ast.With) for child in node.body)]
    need(len(matches) == 1, "transport_try_invalid")
    return matches[0]


def typed_exceptions(node: ast.ExceptHandler) -> str:
    return ast.unparse(node.type) if node.type is not None else ""


def normalized_without_transport_handlers(tree: ast.Module) -> str:
    # Compare all statements, imports, endpoint allowlists, prompt handling,
    # function signatures, response parsing and return logic, ignoring only the
    # explicitly scoped exception handlers inside the one transport try block.
    import copy
    copied = copy.deepcopy(tree)
    outer_transport_try(function(copied)).handlers = []
    return ast.dump(copied, include_attributes=False)


def validate_handler(handler: ast.ExceptHandler, kind: str) -> None:
    need(handler.name == ("exc" if kind in EXCEPTIONS[:2] else None),
         "handler_binding_invalid")
    need(bool(handler.body) and len(list(ast.walk(ast.Module(body=handler.body,
             type_ignores=[])))) <= 90, "handler_size_invalid")
    need(all(type(node) in ALLOWED_BODY_NODES | {ast.Module}
             for node in ast.walk(ast.Module(body=handler.body, type_ignores=[]))),
         "handler_grammar_invalid")
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Call):
            need(isinstance(node.func, ast.Name) and node.func.id in ALLOWED_CALLS
                 and not node.keywords, "handler_call_forbidden")
            if node.func.id == "PilotBlocked":
                need(len(node.args) == 1, "handler_reason_invalid")
            elif node.func.id == "type":
                need(len(node.args) == 1, "handler_type_invalid")
            else:
                need(len(node.args) == 2, "handler_type_invalid")
        elif isinstance(node, ast.Name):
            need(node.id in ALLOWED_NAMES, "handler_name_forbidden")
        elif isinstance(node, ast.Attribute):
            need(ast.unparse(node) in ALLOWED_ATTRIBUTES,
                 "handler_attribute_forbidden")
        elif isinstance(node, ast.Constant):
            value = node.value
            need(value is None or (type(value) is int and value in (400, 599))
                 or (type(value) is str and (value in CODES
                     or value == "ollama_http_")),
                 "handler_literal_forbidden")
        elif isinstance(node, ast.FormattedValue):
            need(ast.unparse(node.value) == "exc.code" and node.conversion == -1
                 and node.format_spec is None, "handler_format_forbidden")
        elif isinstance(node, ast.Raise):
            need(isinstance(node.exc, ast.Call)
                 and isinstance(node.exc.func, ast.Name)
                 and node.exc.func.id == "PilotBlocked"
                 and isinstance(node.cause, ast.Constant)
                 and node.cause.value is None, "handler_raise_invalid")
    need(isinstance(handler.body[-1], ast.Raise), "handler_fallback_missing")


def classify_candidate(*, repository: str, base_sha: str, path: str,
                       before: bytes, after: bytes) -> dict[str, str]:
    """Static syntax gate; NEVER declares Qwen invocation, CAS or READY_FOR_PR."""
    need(repository == REPOSITORY and path == PATH and base_sha == BASE_SHA,
         "identity_or_base_drift")
    need(type(before) is bytes and type(after) is bytes and before != after,
         "patch_empty_or_invalid")
    old_text, old_tree = parse(before)
    new_text, new_tree = parse(after)
    old_fn, new_fn = function(old_tree), function(new_tree)
    old_try, new_try = outer_transport_try(old_fn), outer_transport_try(new_fn)
    need(len(old_try.handlers) == 1
         and typed_exceptions(old_try.handlers[0]) ==
         "(urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError)"
         and ast.dump(ast.Module(body=old_try.handlers[0].body, type_ignores=[]),
                      include_attributes=False) == ast.dump(ast.parse(
                          'raise PilotBlocked("ollama_unreachable") from None'),
                      include_attributes=False),
         "baseline_not_reproduced")
    need([typed_exceptions(x) for x in new_try.handlers] == list(EXCEPTIONS),
         "handler_classes_invalid")
    need(normalized_without_transport_handlers(old_tree) ==
         normalized_without_transport_handlers(new_tree),
         "candidate_scope_changed")
    # Exact raw text outside the function must also match, including comments.
    old_lines, new_lines = old_text.splitlines(keepends=True), new_text.splitlines(keepends=True)
    need(old_lines[:old_fn.lineno - 1] == new_lines[:new_fn.lineno - 1]
         and old_lines[old_fn.end_lineno:] == new_lines[new_fn.end_lineno:],
         "module_text_changed")
    for handler, kind in zip(new_try.handlers, EXCEPTIONS):
        validate_handler(handler, kind)
    # A structural gate is NOT a behavioral correctness test or model receipt.
    return {"state": "static_profile_admitted_only", "profile": PROFILE,
            "base_sha": BASE_SHA, "path": PATH,
            "before_sha256": digest(before), "after_sha256": digest(after)}
