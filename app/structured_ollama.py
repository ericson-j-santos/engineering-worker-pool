"""Local-only proposal transport for the structured-function contract."""
from __future__ import annotations

import json

import httpx

from app.local_code_worker import LocalOllama, OLLAMA_URL, DIGEST
from app.structured_code_worker import proposal_spec, candidate_fingerprint, extracted, need
from app.structured_anchors import apply_anchored_edits, line_catalog


MODEL = "qwen2.5-coder:7b"
FUNCTION_PATTERN = r"^def [^\[\]+]+$"


def proposal_messages(proposal: dict, before: str) -> list[dict]:
    task = proposal_spec(proposal)
    # The first runs copied faulty source. Generate from the trusted behavioral
    # contract instead; the executor still binds the patch to the exact source hash.
    prompt = (
        "Write a NEW implementation from the behavioral contract, not a transcription.\n"
        + "Required definition header: def " + task["function"] + "("
        + ", ".join(task["parameters"]) + "):\n"
        + "The JSON function string must include def, the full signature and its body.\n"
        + "Trusted input/output examples:\n"
        + json.dumps(task["cases"], ensure_ascii=False, separators=(",", ":"))
        + "\nExecution constraints: no imports, helpers, annotations, decorators, try/except, "
          "while, break, continue, comprehensions, indexing with brackets or recursion. "
          "Use only local assignments, if/elif/else, return and at most one for loop. "
          "Allowed builtins: isinstance, str, dict, list, len, bool, int, float. "
          "Allowed methods: get, strip, join, append, with positional arguments only. "
          "Use get instead of indexing. Create empty lists with list(), never brackets. "
          "Append strings to the list, then join it. No addition or augmented assignments. "
          "Concatenate text with an EMPTY separator, never add spaces between fragments. "
          "Do not coerce data with str. "
          "A malformed rich-text fragment must immediately return an empty string, "
          "not be skipped and not produce a partial result. "
          "Check the top-level properties object before calling its get method. "
          "Check select.name is a string. An empty plain_text is valid and overrides text.content. "
          "If a fragment has non-null non-string plain_text, return empty for the WHOLE field. "
          "If fallback text/content is absent or not a string, return empty for the WHOLE field. "
          "Do not output tests or Markdown. Return only JSON with key function.\n"
        + "AUTHORITATIVE BEHAVIORAL CONTRACT:\n" + task["instruction"]
    )
    messages = [
        {"role": "system", "content":
         "You are repairing a real defect, not transcribing code. Output exactly one JSON object. "
         "The function value MUST begin with def and contain the complete CORRECTED function."},
        {"role": "user", "content": prompt},
    ]
    if "feedback" in proposal:
        feedback = proposal["feedback"]
        # All failure observations and the candidate fingerprint are validated
        # before inference; original task/cases remain immutable.
        need(candidate_fingerprint(before, task) == feedback["candidate_sha256"],
             "feedback_candidate_mismatch")
        source = extracted(before, task)
        anchors = line_catalog(source)  # Reject unanchorable candidates BEFORE another model call.
        lines = source.splitlines()
        catalog = "\n".join(f"{anchor}: {lines[index]}" for anchor, index in anchors.items())
        # Focus only source-bound exception locations. All value mismatches
        # remain mandatory and the trusted validator decides acceptance.
        affected = {}
        for failure in feedback["failures"]:
            observation = failure["observed"]
            number = observation.get("candidate_line")
            anchor = f"L{number}"
            if (observation["kind"] == "exception" and type(number) is int
                    and anchor in anchors):
                affected.setdefault(anchor, []).append(failure["case_index"])
        focus = [{"anchor": anchor, "failed_case_indices": affected[anchor]}
                 for anchor in sorted(affected, key=lambda item: int(item[1:]))]
        messages[0]["content"] = (
            "Repair ONLY the failing lines of the previous Python function. "
            "Return exactly one JSON object with key edits: an array of 1 to 6 "
            "objects using EXACTLY ONE of: {anchor,new} to replace one line, "
            "or {anchor,guard,return_expression} to add a typed early-return guard "
            "before an existing if header. "
            "Never output a full function, a replacement implementation, Markdown or tests.")
        messages[1]["content"] = (
            task["instruction"] + "\n"
            "Preserve the exact signature and all unmentioned lines. Each anchor L<number> "
            "identifies one existing, immutable source line; use only the listed IDs, "
            "at most once each. For {anchor,new}, new MUST contain ONE complete "
            "Python line, with NO newline or leading indentation; the assembler "
            "keeps the existing indentation. Preserve a trailing colon exactly "
            "when the original line ends with a colon, and NEVER introduce a new "
            "block header on a non-header line. To add an early-return guard before "
            "an existing if header, use {anchor,guard,return_expression}; give only "
            "the condition and return expression, not if/return syntax, indentation "
            "or a colon. The assembler adds the complete two-line guard while "
            "preserving the existing if and its body. "
            "No imports, subscripts, list literals, try, while, break, comprehensions "
            "or untrusted calls. Use dict.get, list(), append/join, isinstance. "
            "VALIDATION REQUIREMENTS: Call dict.get only after verifying dict, and str.strip "
            "only after verifying str. Reject invalid fragments instead of ignoring them "
            "or returning partial strings. Preserve valid empty plain_text; join all "
            "fragments before the final strip, not one strip per fragment. "
            "Every failure must be corrected; preserve all previously passing cases. "
            "When an exception observation has candidate_line, that is a one-based "
            "line in this rejected candidate, not an approved solution. Prioritize "
            "those anchors or their necessary preceding type guards. Fix every value "
            "mismatch too. A new if/elif/else needs a nonempty suite with four spaces "
            "of relative indentation; do not orphan the existing header's suite. "
            "Do not emit cosmetic changes or an unchanged AST.\n"
            "IMMUTABLE_SOURCE_ANCHORS_V1\n" + catalog)
        # The failed candidate is still available as source-bound line data above.
        # Do not demonstrate it as a prior assistant answer: that reinforced copying
        # the incorrect implementation in real model attempts.
        messages[1]["content"] += (
            "\nREJECTED_SOURCE_IS_NOT_AN_ANSWER_V1\n"
            "The anchored lines above represent a rejected candidate, never an approved output. "
            "Use them only to locate minimal edits; all nonedited lines stay unchanged.\n"
            "PATCH THE PREVIOUS CODE AT THE EXACT LISTED LINES ONLY. "
            "Return ONLY JSON edits in the exact anchor/new or "
            "anchor/guard/return_expression shapes, at most six. "
            "Do not emit multiline new values or incomplete if blocks. "
            "Make guards reject invalid JSON types before any .get or .strip. "
            "A single invalid fragment must return empty for the whole field. "
            "Join raw fragments with an empty separator then strip the result once. "
            "Retain source branches for valid inputs. "
            "Do not return the original function unchanged.\n"
            "FOCUSED_EXCEPTION_ANCHORS_V1\n"
            + json.dumps(focus, ensure_ascii=False, separators=(",", ":"))
            + "\nVALIDATOR_OBSERVATIONS_V1\n"
            + json.dumps(feedback, ensure_ascii=False, separators=(",", ":")))

    return messages


class StructuredOllama(LocalOllama):
    revision_observer = None  # Opt-in trusted callback; never a default file output.

    def __call__(self, task: dict, before: str) -> str:
        messages = proposal_messages(task, before)
        self.last_revision = None
        self.last_model_reply_sha256 = ""
        is_revision = "feedback" in task
        self.no_cloud()
        with httpx.Client(base_url=OLLAMA_URL, timeout=180, trust_env=False,
                          follow_redirects=False) as client:
            models = self.request(client, "GET", "/api/tags").get("models")
            need(type(models) is list and len(models) == 1 and type(models[0]) is dict,
                 "model_inventory")
            model = models[0]
            digest = model.get("digest")
            need(model.get("name") == MODEL and not model.get("remote_host")
                 and not model.get("remote_model"), "remote_model")
            need(type(digest) is str and DIGEST.fullmatch(digest) is not None, "model_digest")
            metadata = self.request(client, "POST", "/api/show", json={"model": MODEL})
            need(not metadata.get("remote_host") and not metadata.get("remote_model")
                 and type(metadata.get("details")) is dict
                 and metadata["details"].get("format") == "gguf", "model_weights")
            response = self.request(client, "POST", "/api/chat", json={
                "model": MODEL,
                "messages": messages,
                "stream": False, "keep_alive": "5m",
                "format": "json" if is_revision else {"type": "object", "properties": {"function": {"type": "string",
                                                                    "pattern": FUNCTION_PATTERN}},
                           "required": ["function"], "additionalProperties": False},
                "options": {"temperature": 0, "seed": 7, "num_predict": 1024 if is_revision else 1400,
                            "num_ctx": 4096 if is_revision else 8192},
            })
            need(response.get("done") is True and response.get("model") == MODEL
                 and type(response.get("eval_count")) is int and response["eval_count"] > 0,
                 "inference_unproved")
            running = self.request(client, "GET", "/api/ps").get("models")
            need(type(running) is list and any(
                type(item) is dict and item.get("name") == MODEL and item.get("digest") == digest
                and type(item.get("size")) is int and item["size"] > 0
                and not item.get("remote_host") and not item.get("remote_model")
                for item in running), "local_process_unproved")
            self.no_cloud()
            self.model_digest, self.process_verified = digest, True
            message = response.get("message")
            need(type(message) is dict and type(message.get("content")) is str,
                 "provider_content")
            reply = message["content"]
            # Initial reply is a whole function. Revision reply contains model-selected
            # immutable line edits; the trusted assembler enforces AST and signature.
            import hashlib
            self.last_model_reply_sha256 = hashlib.sha256(reply.encode()).hexdigest()
            if is_revision:
                verified, _edits = apply_anchored_edits(
                    reply, before, task, observer=self.revision_observer,
                    model_strict=True)
                self.last_revision = "anchored_edits"
                return verified
            return reply
