"""Apply exact, non-overlapping edits chosen by the model to one guarded function.

The trusted task, signature, tests and execution grammar are not changed.
This is patch assembly, not a candidate implementation or a fallback answer.
"""
from __future__ import annotations

import json

from app.structured_code_worker import (
    candidate_fingerprint, extracted, need, proposal_spec, render, StructuredBlocked, NODES,
)

from app.structured_diagnostics import RevisionTrace

# Generation constraint mirrors the observed Subscript rejection; execution NODES is unchanged.
REVISION_NEW_PATTERN = r"^[^\[\]]*$"

REVISION_SCHEMA = {
    "type": "object",
    "properties": {"edits": {
        "type": "array", "minItems": 1, "maxItems": 6,
        "items": {"type": "object",
                  "properties": {"old": {"type": "string", "minLength": 1, "maxLength": 3000},
                                 "new": {"type": "string", "maxLength": 6000,
                                         "pattern": REVISION_NEW_PATTERN}},
                  "required": ["old", "new"], "additionalProperties": False},
    }},
    "required": ["edits"], "additionalProperties": False,
}


def apply_local_edits(reply: str, before: str, proposal: dict, *, observer=None) -> tuple[str, list[dict]]:
    trace = RevisionTrace(reply, observer)
    try:
        result = _apply_local_edits(reply, before, proposal, trace)
    except StructuredBlocked as error:
        trace.rejected(str(error))
        raise
    trace.accepted()
    return result


def _apply_local_edits(reply: str, before: str, proposal: dict, trace: RevisionTrace) -> tuple[str, list[dict]]:
    task = proposal_spec(proposal)
    need("feedback" in proposal, "revision_feedback_missing")
    need(candidate_fingerprint(before, task) == proposal["feedback"]["candidate_sha256"],
         "feedback_candidate_mismatch")
    need(type(reply) is str and len(reply.encode()) <= 16384, "revision_size")
    try:
        value = json.loads(reply)
    except ValueError:
        raise StructuredBlocked("revision_json") from None
    need(type(value) is dict and set(value) == {"edits"}
         and type(value["edits"]) is list and 1 <= len(value["edits"]) <= 6,
         "revision_schema")
    source = extracted(before, task)
    spans = []
    for edit in value["edits"]:
        need(type(edit) is dict and set(edit) == {"old", "new"}
             and type(edit["old"]) is str and 1 <= len(edit["old"]) <= 3000
             and type(edit["new"]) is str and len(edit["new"]) <= 6000,
             "revision_edit_schema")
        old, new = edit["old"], edit["new"]
        need(old != new, "candidate_repeated")
        need(source.count(old) == 1, "revision_edit_not_unique")
        start = source.index(old)
        need(start >= source.index("\n") + 1, "revision_signature_edit")
        spans.append((start, start + len(old), new))
    spans.sort()
    need(all(left[1] <= right[0] for left, right in zip(spans, spans[1:])),
         "revision_edits_overlap")
    for start, end, new in reversed(spans):
        source = source[:start] + new + source[end:]
    # Re-apply the same full-function boundary/signature/AST validation before assembly.
    trace.before_guard(source, NODES)
    candidate = render(before, task, json.dumps({"function": source}))
    need(candidate_fingerprint(candidate, task) != proposal["feedback"]["candidate_sha256"],
         "candidate_repeated")
    return json.dumps({"function": extracted(candidate, task)}, ensure_ascii=False), value["edits"]

def apply_full_function_revision(reply: str, before: str, proposal: dict, *, observer=None) -> str:
    """Check a complete model revision against the immutable candidate and AST guard."""
    trace = RevisionTrace(reply, observer)
    try:
        task = proposal_spec(proposal)
        need("feedback" in proposal, "revision_feedback_missing")
        need(candidate_fingerprint(before, task) == proposal["feedback"]["candidate_sha256"],
             "feedback_candidate_mismatch")
        need(type(reply) is str and len(reply.encode()) <= 16384, "revision_size")
        try:
            value = json.loads(reply)
        except ValueError:
            raise StructuredBlocked("reply_json") from None
        need(type(value) is dict and set(value) == {"function"}
             and type(value["function"]) is str, "reply_schema")
        trace.before_guard(value["function"], NODES)
        candidate = render(before, task, reply)
        need(candidate_fingerprint(candidate, task) != proposal["feedback"]["candidate_sha256"],
             "candidate_repeated")
    except StructuredBlocked as error:
        trace.rejected(str(error))
        raise
    trace.accepted()
    return reply
