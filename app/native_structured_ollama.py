"""Opt-in native Windows/CPU transport for trusted, bounded structured repairs.

No Docker, cloud API client, model download, arbitrary command execution,
or implicit fallback. Localhost is enforced at the actual HTTP client by
StructuredOllama (trust_env=False, follow_redirects=False).

The opt-in flag constrains THIS client; it cannot prove the Ollama daemon's
global cloud configuration. The physical runner must separately qualify that.
"""
from __future__ import annotations

import os
import re

from app.local_code_worker import OLLAMA_URL
from app.structured_code_worker import need
from app.structured_ollama import MODEL, StructuredOllama

PIN_PATTERN = re.compile(r"[0-9a-f]{12,64}\Z")


class NativeStructuredOllama(StructuredOllama):
    """Use the installed local GGUF model, even when other models are installed."""

    @staticmethod
    def no_cloud() -> None:
        # Preserve the existing legacy Docker CI provider; this one never
        # invokes docker or any external provider.
        need(OLLAMA_URL == "http://127.0.0.1:11434", "native_loopback_required")
        need(os.environ.get("NATIVE_OLLAMA_DEV") == "1", "native_dev_opt_in_required")
        pin = os.environ.get("NATIVE_MODEL_DIGEST_PREFIX", "")
        need(PIN_PATTERN.fullmatch(pin) is not None, "native_digest_pin_required")

    @staticmethod
    def select_model(models: object) -> dict:
        need(type(models) is list and 1 <= len(models) <= 64
             and all(type(x) is dict for x in models), "native_model_inventory")
        matched = [item for item in models if item.get("name") == MODEL]
        need(len(matched) == 1, "native_model_ambiguous_or_missing")
        item = matched[0]
        digest = item.get("digest")
        need(type(digest) is str and re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
             "native_model_digest")
        pin = os.environ.get("NATIVE_MODEL_DIGEST_PREFIX", "")
        need(PIN_PATTERN.fullmatch(pin) is not None and digest.startswith(pin),
             "native_model_digest_mismatch")
        need(type(item.get("size")) is int and item["size"] > 0
             and type(item.get("details")) is dict
             and item["details"].get("format") == "gguf", "native_gguf_unverified")
        need(not item.get("remote_host") and not item.get("remote_model"),
             "native_remote_model_forbidden")
        return item

    def generation_options(self, is_revision: bool) -> dict:
        # At measured CPU-only speed, 1400 generated tokens could exceed the
        # governed single-call timeout. Native-only profile stays bounded.
        return {"temperature": 0, "seed": 7,
                "num_predict": 384 if is_revision else 768,
                "num_ctx": 4096 if is_revision else 8192}
