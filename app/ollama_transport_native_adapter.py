"""Opt-in, issue-bound Ollama HTTP repair adapter for NativeOneShotExecutor.

This module never dispatches tasks, authorizes a host, merges or publishes Git.
The installed executor supplies Session/Gateway, fenced lease, CAS and Validator.
The model must produce new handler code; the historical assisted patch is NOT
attributed to it. A green unit test does not attest real model execution.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from app.native_one_shot import AdapterContext, AdapterDescriptor, AdapterResult, TaskManifest
from app.ollama_transport_repair_profile import (
    BASE_SHA, FUNCTION, PATH, PROFILE, REPOSITORY, classify_candidate, digest,
)
from app.structured_code_worker import clean_git_configuration, git

MODEL = "qwen2.5-coder:7b"
MODEL_PIN = "dae161e27b0e"
ENDPOINT = "http://127.0.0.1:11434"
MAX_RESPONSE = 65536
OLD_HANDLER = (
    "    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):\n"
    "        raise PilotBlocked(\"ollama_unreachable\") from None"
)
DESCRIPTOR = AdapterDescriptor(
    adapter_id=PROFILE,
    allowed_paths=(PATH,),
    builder_commands=("ollama.transport.repair",),
    validator_commands=("ollama.transport.validate",),
)


class TransportBlocked(RuntimeError):
    """Fixed error code only. Never include prompts, IP responses or exceptions."""


def need(ok: bool, code: str) -> None:
    if not ok:
        raise TransportBlocked(code)


class LocalQwenHttpProposer:
    """Strict localhost, one-shot model request; never downloads or falls back."""

    def _request(self, path: str, *, data: bytes | None = None, timeout: int = 10) -> dict:
        need(path in ("/api/tags", "/api/generate"), "endpoint_denied")
        need(type(timeout) is int and 1 <= timeout <= 120, "timeout_denied")
        headers = {"Accept": "application/json"}
        if data is not None:
            need(type(data) is bytes and len(data) <= 16384, "request_size_denied")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(ENDPOINT + path, data=data, headers=headers,
                                     method="GET" if data is None else "POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            raise TransportBlocked("local_model_transport_failed") from None
        need(len(raw) <= MAX_RESPONSE, "model_response_too_large")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError):
            raise TransportBlocked("model_json_invalid") from None
        need(type(result) is dict, "model_schema_invalid")
        return result

    def propose(self, original: bytes) -> dict[str, Any]:
        need(os.environ.get("NATIVE_OLLAMA_DEV") == "1"
             and os.environ.get("OLLAMA_NO_CLOUD") == "1",
             "local_only_not_opted_in")
        need(type(original) is bytes and OLD_HANDLER.encode() in original,
             "baseline_not_supported")
        inventory = self._request("/api/tags")
        models = inventory.get("models")
        need(type(models) is list and 1 <= len(models) <= 64,
             "local_inventory_invalid")
        candidates = [m for m in models if type(m) is dict and m.get("name") == MODEL]
        need(len(candidates) == 1, "model_ambiguous_or_missing")
        model = candidates[0]
        model_digest = model.get("digest")
        need(type(model_digest) is str
             and bool(re.fullmatch(r"[0-9a-f]{64}", model_digest))
             and model_digest.startswith(MODEL_PIN)
             and type(model.get("size")) is int and model["size"] > 0
             and type(model.get("details")) is dict
             and model["details"].get("format") == "gguf"
             and not model.get("remote_model") and not model.get("remote_host"),
             "local_model_identity_invalid")
        prompt = (
            "You are repairing exactly the urllib transport exception handlers in a "
            "public local-only Python HTTP function. Return JSON with one property "
            "replacement: a Python source string containing exactly five except "
            "clauses for HTTPError, URLError, TimeoutError, ConnectionError, "
            "OSError in that order, all indented four spaces. Classify HTTP "
            "status codes 400..599, timeout, connection, OS errors with fixed "
            "PilotBlocked reason codes. Never return error bodies, secrets, URLs, "
            "downloads, subprocesses, imports or unrelated edits. No Markdown. "
            "Do not copy a pre-existing solution. The prior handler is:\n"
            + OLD_HANDLER
        )
        payload = json.dumps({
            "model": MODEL, "prompt": prompt, "stream": False,
            "format": {"type": "object", "properties": {"replacement": {"type": "string"}},
                       "required": ["replacement"], "additionalProperties": False},
            "keep_alive": "1m",
            "options": {"temperature": 0, "seed": 42, "num_ctx": 2048,
                        "num_predict": 512},
        }, separators=(",", ":")).encode("utf-8")
        generated = self._request("/api/generate", data=payload, timeout=120)
        need(generated.get("model") == MODEL and generated.get("done") is True,
             "inference_not_proved")
        need(type(generated.get("eval_count")) is int
             and generated["eval_count"] > 0
             and type(generated.get("eval_duration")) is int
             and generated["eval_duration"] > 0, "inference_metrics_missing")
        raw = generated.get("response")
        need(type(raw) is str and len(raw) <= 6000, "model_output_invalid")
        try:
            obj = json.loads(raw)
        except ValueError:
            raise TransportBlocked("model_output_invalid") from None
        need(type(obj) is dict and set(obj) == {"replacement"}
             and type(obj["replacement"]) is str
             and 100 <= len(obj["replacement"].encode("utf-8")) <= 3000,
             "model_patch_invalid")
        return {"replacement": obj["replacement"],
                "model": MODEL, "model_digest": model_digest,
                "model_calls": 1, "eval_count": generated["eval_count"]}


class OllamaTransportNativeAdapter:
    """Fixed AdapterDescriptor; no mutable path, command or repository inputs."""

    descriptor = DESCRIPTOR

    def __init__(self, *, original: bytes, task_id: str,
                 proposer: LocalQwenHttpProposer,
                 regression_runner: Callable[[Path], Mapping[str, Any]]):
        need(type(original) is bytes and len(original) <= 262144
             and type(task_id) is str and bool(re.fullmatch(r"cwp-[0-9a-f]{24}", task_id))
             and type(proposer) is LocalQwenHttpProposer
             and callable(regression_runner), "adapter_construction_denied")
        self.original, self.task_id = original, task_id
        self.proposer, self.regression_runner = proposer, regression_runner

    def _file(self, root: Path) -> bytes:
        source = root / PATH
        need(root.is_dir() and not root.is_symlink()
             and source.is_file() and not source.is_symlink()
             and source.stat().st_nlink == 1
             and source.resolve().is_relative_to(root.resolve()),
             "workspace_source_denied")
        data = source.read_bytes()
        need(0 < len(data) <= 262144, "workspace_source_denied")
        return data

    def _manifest(self, ctx: AdapterContext) -> TaskManifest:
        m = ctx.manifest
        need(m.repository == REPOSITORY and m.base_sha == BASE_SHA
             and m.task_id == self.task_id and m.adapter_id == PROFILE
             and m.adapter_contract_sha256 == DESCRIPTOR.contract_sha256
             and set(m.before_sha256) == {PATH},
             "manifest_identity_denied")
        return m

    def commands(self, root: Path) -> dict[str, Callable[[], dict[str, Any]]]:
        def repair() -> dict[str, Any]:
            need(self._file(root) == self.original, "builder_source_drift")
            clean_git_configuration(root)
            need(git(root, "remote") == ""
                 and git(root, "branch", "--show-current").startswith("worker/")
                 and git(root, "rev-parse", "HEAD") == BASE_SHA
                 and git(root, "status", "--porcelain", "--untracked-files=all") == "",
                 "builder_checkout_denied")
            receipt = self.proposer.propose(self.original)
            need(type(receipt) is dict
                 and set(receipt) == {"replacement", "model", "model_digest", "model_calls", "eval_count"}
                 and receipt["model"] == MODEL
                 and type(receipt["model_digest"]) is str
                 and bool(re.fullmatch(r"[0-9a-f]{64}", receipt["model_digest"]))
                 and receipt["model_digest"].startswith(MODEL_PIN)
                 and receipt["model_calls"] == 1 and type(receipt["eval_count"]) is int
                 and receipt["eval_count"] > 0,
                 "builder_model_receipt_denied")
            replacement = receipt["replacement"]
            need(type(replacement) is str and replacement.startswith("    except ")
                 and OLD_HANDLER in self.original.decode("utf-8")
                 and self.original.decode("utf-8").count(OLD_HANDLER) == 1,
                 "builder_patch_denied")
            after = self.original.decode("utf-8").replace(OLD_HANDLER, replacement, 1).encode("utf-8")
            classify_candidate(repository=REPOSITORY, base_sha=BASE_SHA, path=PATH,
                               before=self.original, after=after)
            source = root / PATH
            source.write_bytes(after)
            git(root, "add", "--", PATH)
            need(git(root, "diff", "--cached", "--name-only") == PATH,
                 "builder_scope_denied")
            git(root, "-c", "user.name=GovernedBuilder",
                "-c", "user.email=governed-builder@example.invalid",
                "commit", "-m", "repair(ollama): classify local HTTP transport errors")
            produced = git(root, "rev-parse", "HEAD")
            need(bool(re.fullmatch(r"[0-9a-f]{40}", produced))
                 and produced != BASE_SHA
                 and git(root, "diff", "--name-only", BASE_SHA, produced) == PATH
                 and git(root, "status", "--porcelain", "--untracked-files=all") == ""
                 and self._file(root) == after, "builder_commit_denied")
            return {"produced_sha": produced, "after_sha256": digest(after),
                    "model_digest": receipt["model_digest"],
                    "model_calls": 1, "eval_count": receipt["eval_count"]}

        def validate() -> dict[str, Any]:
            after = self._file(root)
            classify_candidate(repository=REPOSITORY, base_sha=BASE_SHA, path=PATH,
                               before=self.original, after=after)
            result = self.regression_runner(root)
            need(type(result) is dict and result.get("exit_code") == 0
                 and result.get("category") == "passed"
                 and result.get("output_within_limit") is True
                 and type(result.get("passed_count")) is int
                 and result["passed_count"] >= 24
                 and result.get("failed_count") == 0
                 and result.get("error_count") == 0
                 and result.get("skipped_count") == 0
                 and result.get("source_sha256") == digest(after),
                 "validator_regressions_denied")
            return {"source_sha256": digest(after),
                    "passed_count": result["passed_count"]}

        return {"ollama.transport.repair": repair,
                "ollama.transport.validate": validate}

    def build(self, ctx: AdapterContext) -> AdapterResult:
        m = self._manifest(ctx)
        need(m.workspace_sha == BASE_SHA
             and m.before_sha256[PATH] == digest(self.original)
             and self._file(ctx.root) == self.original,
             "builder_manifest_drift")
        observed = ctx.invoke("ollama.transport.repair")
        need(type(observed) is dict
             and type(observed.get("produced_sha")) is str
             and type(observed.get("after_sha256")) is str
             and type(observed.get("model_digest")) is str
             and observed.get("model_calls") == 1,
             "builder_evidence_denied")
        attestation = hashlib.sha256(json.dumps(observed, sort_keys=True).encode()).hexdigest()
        return AdapterResult(produced_sha=observed["produced_sha"],
                             changed_paths=(PATH,), invoked_commands=tuple(ctx.invoked),
                             evidence_sha256=attestation)

    def validate(self, ctx: AdapterContext, produced_sha: str) -> AdapterResult:
        m = self._manifest(ctx)
        need(type(produced_sha) is str
             and bool(re.fullmatch(r"[0-9a-f]{40}", produced_sha))
             and produced_sha != BASE_SHA and m.workspace_sha == produced_sha
             and m.before_sha256[PATH] == digest(self._file(ctx.root))
             and git(ctx.root, "rev-parse", "HEAD") == produced_sha,
             "validator_manifest_drift")
        evidence = ctx.invoke("ollama.transport.validate")
        need(type(evidence) is dict
             and evidence.get("source_sha256") == digest(self._file(ctx.root)),
             "validator_receipt_denied")
        attestation = hashlib.sha256(json.dumps({"sha": produced_sha, "evidence": evidence},
                                                sort_keys=True).encode()).hexdigest()
        return AdapterResult(produced_sha=produced_sha, changed_paths=(PATH,),
                             invoked_commands=tuple(ctx.invoked), evidence_sha256=attestation)
