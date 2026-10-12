"""Bounded metadata for rejected model edits. Never executes or stores source.

The optional observer is trusted and supplied by the caller. The provider has
no default file output. An observer failure is fatal rather than losing proof.
"""
from __future__ import annotations

import ast
import copy
import hashlib
from typing import Callable

REASONS = frozenset({
    "revision_feedback_missing", "feedback_candidate_mismatch", "revision_size",
    "revision_json", "revision_schema", "revision_edit_schema",
    "revision_edit_not_unique", "revision_signature_edit", "revision_edits_overlap",
    "revision_anchor_source", "revision_anchor_invalid", "revision_anchor_duplicate",
    "revision_indentation", "revision_block_header",
    "candidate_repeated", "candidate_grammar", "candidate_function_only",
    "candidate_syntax", "candidate_loops", "candidate_depth", "candidate_name",
    "candidate_call", "candidate_attribute", "nested_function", "reserved_assignment",
    "assignment_target", "loop_target", "generator_target", "call_expansion",
    "dictionary_expansion", "function_identity", "function_signature",
    "function_decorator", "reply_size", "reply_json", "reply_schema",
    "module_size", "module_syntax", "multiline_signature_not_supported",
    "json_depth", "json_object", "json_array", "json_type", "json_string",
    "json_number",
})



# Fixed categories only. SyntaxError.msg can contain user text; never persist it.
SYNTAX_KINDS = (
    ("expected an indented block", "missing_block"),
    ("unexpected indent", "unexpected_indent"),
    ("unindent does not match", "bad_unindent"),
    ("inconsistent use of tabs and spaces", "tabs_spaces"),
    ("expected ':'", "missing_colon"),
    ("invalid syntax", "invalid_syntax"),
    ("'return' outside function", "return_outside"),
    ("'break' outside loop", "break_outside"),
    ("unterminated string", "unterminated_string"),
    ("'(' was never closed", "open_parenthesis"),
    ("'[' was never closed", "open_bracket"),
    ("'{' was never closed", "open_brace"),
)


def syntax_kind(error: SyntaxError) -> str:
    message = error.msg
    if not isinstance(message, str):
        return "other"
    for prefix, category in SYNTAX_KINDS:
        if message.startswith(prefix):
            return category
    return "other"


# Sanitized enum for source-bound rejects; never model text or raw anchor IDs.
ANCHOR_FAILURE_KINDS = frozenset({"unknown_anchor", "guard_not_if_header"})


class DiagnosticWriteError(RuntimeError):
    """Fixed message only; observer exceptions are never propagated as text."""


class RevisionTrace:
    def __init__(self, reply: object, observer: Callable[[dict], None] | None = None):
        self.observer = observer
        self.record = {"schema_version": 1, "stage": "received"}
        if type(reply) is str:
            self.record["response_characters"] = len(reply)
            if len(reply) <= 16384:
                self.record["response_sha256"] = hashlib.sha256(reply.encode()).hexdigest()
        self.emit()

    def emit(self) -> None:
        if self.observer is not None:
            try:
                self.observer(copy.deepcopy(self.record))
            except Exception:
                raise DiagnosticWriteError("revision_diagnostic_write_failed") from None

    def before_guard(self, source: str, allowed_nodes: set[type]) -> None:
        self.record["stage"] = "guard"
        # The assembler is already bounded; keep diagnostics independently bounded too.
        self.record["assembly_characters"] = len(source)
        if len(source) > 65536:
            self.record["ast_parse"] = "size_blocked"
            self.emit()
            return
        self.record["assembly_sha256"] = hashlib.sha256(source.encode()).hexdigest()
        try:
            tree = ast.parse(source)
        except SyntaxError as error:
            self.record["ast_parse"] = "invalid"
            # Only bounded integers, enum and SHA-256. Never error.text or error.msg.
            lines = source.splitlines()
            line = error.lineno
            offset = error.offset
            self.record["syntax_kind"] = syntax_kind(error)
            if type(line) is int and 1 <= line <= len(lines) and line <= 400:
                content = lines[line - 1]
                self.record["syntax_line"] = line
                self.record["syntax_line_sha256"] = hashlib.sha256(content.encode()).hexdigest()
                self.record["syntax_line_indent"] = len(content) - len(content.lstrip(" "))
                self.record["syntax_line_characters"] = len(content)
                if type(offset) is int and 1 <= offset <= len(content) + 1:
                    self.record["syntax_column"] = offset
                if line > 1:
                    previous = lines[line - 2]
                    self.record["syntax_prior_line_colon"] = previous.rstrip().endswith(":")
                    self.record["syntax_prior_line_indent"] = (
                        len(previous) - len(previous.lstrip(" "))
                    )
            self.emit()
            return
        except (ValueError, RecursionError):
            self.record["ast_parse"] = "invalid"
            self.emit()
            return
        self.record["ast_parse"] = "valid"
        count = 0
        forbidden = []
        for node in ast.walk(tree):
            count += 1
            if type(node) not in allowed_nodes and len(forbidden) < 16:
                forbidden.append({
                    "type": type(node).__name__,
                    "line": getattr(node, "lineno", 0),
                    "column": getattr(node, "col_offset", 0),
                })
            if count >= 4096:
                break
        self.record["ast_node_count"] = count
        self.record["ast_scan_truncated"] = count >= 4096
        self.record["rejected_ast_nodes"] = forbidden
        self.record["rejected_ast_types"] = sorted({item["type"] for item in forbidden})
        self.emit()

    def rejected(self, reason: str, *, anchor_failure_kind: str | None = None) -> None:
        if anchor_failure_kind is not None:
            if (reason != "revision_anchor_invalid"
                    or type(anchor_failure_kind) is not str
                    or anchor_failure_kind not in ANCHOR_FAILURE_KINDS):
                raise DiagnosticWriteError("revision_diagnostic_kind_invalid")
            self.record["anchor_failure_kind"] = anchor_failure_kind
        self.record["failed_stage"] = self.record["stage"]
        self.record["stage"] = "rejected"
        self.record["reason_code"] = reason if reason in REASONS else "structured_rejection"
        self.emit()

    def accepted(self) -> None:
        self.record["stage"] = "accepted"
        self.emit()
