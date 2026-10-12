"""Local-only proposal transport for the structured-function contract."""
from __future__ import annotations

import json

import httpx

from app.local_code_worker import LocalOllama, OLLAMA_URL, DIGEST
from app.structured_code_worker import proposal_spec, candidate_fingerprint, extracted, need
from app.structured_anchors import lightweight_anchor_schema, apply_anchored_edits


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
        need(candidate_fingerprint(before, task) == feedback["candidate_sha256"],
             "feedback_candidate_mismatch")
        # Keep the revision context bounded: the original instruction and ALL tests stay
        # authoritative in the executor, but do not duplicate passing examples in this turn.
        messages[0]["content"] = (
            "Repair the previous function using exact localized replacements. "
            "Return ONLY JSON with key edits, each either {anchor,new} or "
            "{anchor,guard,return_expression}. "
            "Never return the whole function, prose or Markdown.")
        messages[1]["content"] = (
            task["instruction"] + "\n"
            "The function is supplied in the next message. Preserve its signature and "
            "passing behavior. Allowed constructs: local assignments, if/elif/else, return "
            "and at most one for loop; builtins isinstance, str, dict, list, len, bool, int, float; "
            "methods get, strip, join, append. No imports, helpers, indexing, try, while, "
            "break, continue, comprehensions, recursion, addition or augmented assignment.")
        # A real assistant/user revision turn: observations are not hidden in the instruction.
        messages.extend([
            {"role": "assistant", "content": json.dumps(
                {"function": extracted(before, task)}, ensure_ascii=False)},
            {"role": "user", "content":
             "PREVIOUS PROPOSAL FAILED REAL TESTS. Make LOCALIZED EDITS only. "
             "Use observed failures to fix responsible branches or expressions; "
             "preserve passing cases, signature, tests and execution restrictions. "
             "Return JSON with key edits; at most six unique anchors. "
             "Each edit is {anchor,new} or {anchor,guard,return_expression}. "
             "Select from L2 onward; L1 is the signature and cannot change. "
             "Each new replaces one original line; its first line is unindented "
             "and additional lines are relative to that original line. "
             "A block opener ending with : must still end with : after replacement. "
             "Do not orphan its original suite; use a typed guard instead. "
             "Typed guard edits apply only to existing if-headers; guard and "
             "return_expression are single expressions, not if/return statements. "
             "Use dict.get and list(). Never use square brackets in replacement text. "
             "Do not change the whole function; a no-op is rejected.\n"
             "IMMUTABLE_SOURCE_LINE_ANCHORS\n"
             + "\n".join(f"L{number}: {line}" for number, line in
                         enumerate(extracted(before, task).splitlines(), 1) if number > 1)
             + "\nGUARDABLE_IF_ANCHORS_JSON\n"\n             + json.dumps([f"L{number}" for number, line in\n                           enumerate(extracted(before, task).splitlines(), 1)\n                           if number > 1 and line.lstrip().startswith("if ")\n                           and line.rstrip().endswith(":")], separators=(",", ":"))\n             + "\nVALIDATOR_OBSERVATIONS_V1\n" + json.dumps(feedback, ensure_ascii=False,
                                                       separators=(",", ":"))},
        ])
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
                "format": lightweight_anchor_schema(before) if is_revision else {"type": "object", "properties": {"function": {"type": "string",
                                                                    "pattern": FUNCTION_PATTERN}},
                           "required": ["function"], "additionalProperties": False},
                "options": {"temperature": 0, "seed": 7, "num_predict": 384 if is_revision else 1400,
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
            # Retain transport identity for callers; the returned function is assembled from model edits.
            import hashlib
            self.last_model_reply_sha256 = hashlib.sha256(reply.encode()).hexdigest()
            if is_revision:
                assembled, edits = apply_anchored_edits(
                    reply, before, task, observer=self.revision_observer)
                self.last_revision = edits
                return assembled
            return reply
