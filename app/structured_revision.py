"""Apply exact, non-overlapping edits chosen by the model to one guarded function.

The trusted task, signature, tests and execution grammar are not changed.
This is patch assembly, not a candidate implementation or a fallback answer.
"""
from __future__ import annotations

import json

from app.structured_code_worker import (
    candidate_fingerprint, extracted, need, proposal_spec, render, StructuredBlocked,
)

REVISION_SCHEMA = {
    "type": "object",
    "properties": {"edits": {
        "type": "array", "minItems": 1, "maxItems": 6,
        "items": {"type": "object",
                  "properties": {"old": {"type": "string", "minLength": 1, "maxLength": 3000},
                                 "new": {"type": "string", "maxLength": 6000}},
                  "required": ["old", "new"], "additionalProperties": False},
    }},
    "required": ["edits"], "additionalProperties": False,
}


def apply_local_edits(reply: str, before: str, proposal: dict) -> tuple[str, list[dict]]:
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
    candidate = render(before, task, json.dumps({"function": source}))
    need(candidate_fingerprint(candidate, task) != proposal["feedback"]["candidate_sha256"],
         "candidate_repeated")
    return json.dumps({"function": extracted(candidate, task)}, ensure_ascii=False), value["edits"]
