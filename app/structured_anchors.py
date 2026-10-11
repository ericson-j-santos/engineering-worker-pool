"""Candidate-bound line anchors for model revisions; no source matching by guessed text."""
from __future__ import annotations

import ast
import json

from app.structured_diagnostics import RevisionTrace

NEW_PATTERN = r"^[^\[\]]*$"


class AnchorRejected(ValueError):
    """Closed reason codes only."""


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
    return {
        "type": "object",
        "properties": {"edits": {
            "type": "array", "minItems": 1, "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "anchor": {"type": "string", "enum": list(anchors)},
                    "new": {"type": "string", "maxLength": 6000, "pattern": NEW_PATTERN},
                },
                "required": ["anchor", "new"], "additionalProperties": False,
            },
        }},
        "required": ["edits"], "additionalProperties": False,
    }


def assemble_anchors(reply: str, source: str) -> tuple[str, list[dict]]:
    anchors = line_catalog(source)
    require(type(reply) is str and len(reply.encode()) <= 16384, "revision_size")
    try:
        value = json.loads(reply)
    except ValueError:
        raise AnchorRejected("revision_json") from None
    require(type(value) is dict and set(value) == {"edits"}
            and type(value["edits"]) is list and 1 <= len(value["edits"]) <= 6,
            "revision_schema")
    edits = value["edits"]
    lines = source.splitlines(keepends=True)
    replacements = {}
    for edit in edits:
        require(type(edit) is dict and set(edit) == {"anchor", "new"}
                and type(edit["anchor"]) is str and type(edit["new"]) is str
                and len(edit["new"]) <= 6000, "revision_edit_schema")
        anchor, new = edit["anchor"], edit["new"]
        require(anchor in anchors, "revision_anchor_invalid")
        index = anchors[anchor]
        require(index not in replacements, "revision_anchor_duplicate")
        require("\r" not in new and "\t" not in new and "\0" not in new,
                "revision_indentation")
        added = new.splitlines()
        require(not added or (added[0] and not added[0][0].isspace()),
                "revision_indentation")
        # Only indentation from the immutable selected line is supplied by the assembler.
        original = lines[index]
        indent = original[:len(original) - len(original.lstrip(" "))]
        replacement = "".join(indent + line + "\n" for line in added)
        require(replacement != original, "candidate_repeated")
        replacements[index] = replacement
    for index, replacement in replacements.items():
        lines[index] = replacement
    assembled = "".join(lines)
    require(len(assembled.encode()) <= 20000, "revision_size")
    return assembled, edits


def apply_anchored_edits(reply: str, before: str, proposal: dict, *, observer=None) -> tuple[str, list[dict]]:
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
        assembled, edits = assemble_anchors(reply, source)
        trace.before_guard(assembled, NODES)
        candidate = render(before, task, json.dumps({"function": assembled}))
        need(candidate_fingerprint(candidate, task) != proposal["feedback"]["candidate_sha256"],
             "candidate_repeated")
    except AnchorRejected as error:
        trace.rejected(str(error))
        raise StructuredBlocked(str(error)) from None
    except StructuredBlocked as error:
        trace.rejected(str(error))
        raise
    trace.accepted()
    return json.dumps({"function": extracted(candidate, task)}, ensure_ascii=False), edits
