"""Local-only proposal transport for the structured-function contract."""
from __future__ import annotations

import json

import httpx

from app.local_code_worker import LocalOllama, OLLAMA_URL, DIGEST
from app.structured_code_worker import task_spec, need


MODEL = "qwen2.5-coder:7b"
FUNCTION_PATTERN = r"^def [^\[\]+]+$"


class StructuredOllama(LocalOllama):
    def __call__(self, task: dict, before: str) -> str:
        task_spec(task)
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
            response = self.request(client, "POST", "/api/chat", json={
                "model": MODEL,
                "messages": [{"role": "system", "content":
                              "You are repairing a real defect, not transcribing code. Output exactly one JSON object. "
                              "The function value MUST begin with def and contain the complete CORRECTED function."},
                             {"role": "user", "content": prompt}],
                "stream": False, "keep_alive": "5m",
                "format": {"type": "object", "properties": {"function": {"type": "string",
                                                                    "pattern": FUNCTION_PATTERN}},
                           "required": ["function"], "additionalProperties": False},
                "options": {"temperature": 0, "seed": 7, "num_predict": 1400, "num_ctx": 8192},
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
            return message["content"]
