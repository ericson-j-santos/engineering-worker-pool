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
    "revision_indentation",
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
        except (SyntaxError, ValueError, RecursionError):
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

    def rejected(self, reason: str) -> None:
        self.record["failed_stage"] = self.record["stage"]
        self.record["stage"] = "rejected"
        self.record["reason_code"] = reason if reason in REASONS else "structured_rejection"
        self.emit()

    def accepted(self) -> None:
        self.record["stage"] = "accepted"
        self.emit()
