"""Contract tests for native localhost-only Qwen7B without Docker."""
from __future__ import annotations

import json

import httpx
import pytest

from app import local_code_worker as legacy
from app import native_structured_ollama as native
from app import structured_ollama
from app.structured_code_worker import StructuredBlocked
from test_structured_code_worker import BEFORE, FIXED, task


PIN = "dae161e27b0e"
DIGEST = PIN + "a" * (64 - len(PIN))


@pytest.fixture(autouse=True)
def native_opt_in(monkeypatch):
    monkeypatch.setenv("NATIVE_OLLAMA_DEV", "1")
    monkeypatch.setenv("NATIVE_MODEL_DIGEST_PREFIX", PIN)


def inventory():
    return {"models": [
        {"name": "other:small", "digest": "b" * 64, "size": 200,
         "details": {"format": "gguf"}},
        {"name": structured_ollama.MODEL, "digest": DIGEST, "size": 4683087561,
         "details": {"format": "gguf"}},
    ]}


def test_native_opt_in_required_before_network(monkeypatch):
    monkeypatch.delenv("NATIVE_OLLAMA_DEV")
    with pytest.raises(StructuredBlocked, match="native_dev_opt_in_required"):
        native.NativeStructuredOllama.no_cloud()
    monkeypatch.setenv("NATIVE_OLLAMA_DEV", "1")
    monkeypatch.delenv("NATIVE_MODEL_DIGEST_PREFIX")
    with pytest.raises(StructuredBlocked, match="native_digest_pin_required"):
        native.NativeStructuredOllama.no_cloud()


@pytest.mark.parametrize("model_state,reason", [
    ({"models": []}, "native_model_inventory"),
    ({"models": [{"name": "other"}]}, "native_model_ambiguous_or_missing"),
    ({"models": [inventory()["models"][1]] * 2}, "native_model_ambiguous_or_missing"),
    ({"models": [dict(inventory()["models"][1], digest="c" * 64)]},
     "native_model_digest_mismatch"),
    ({"models": [dict(inventory()["models"][1], size=0)]}, "native_gguf_unverified"),
    ({"models": [dict(inventory()["models"][1], remote_host="remote.example")]},
     "native_remote_model_forbidden"),
])
def test_model_admission_blocks_untrusted_inventory(model_state, reason):
    with pytest.raises(StructuredBlocked, match=f"^{reason}$"):
        native.NativeStructuredOllama.select_model(model_state["models"])


def test_legacy_hermetic_single_model_contract_is_unchanged():
    with pytest.raises(StructuredBlocked, match="^model_inventory$"):
        structured_ollama.StructuredOllama.select_model(inventory()["models"])


def test_native_model_uses_bounded_options_without_changing_legacy():
    provider = native.NativeStructuredOllama()
    assert provider.generation_options(False)["num_predict"] == 768
    assert provider.generation_options(True)["num_predict"] == 384
    assert structured_ollama.StructuredOllama().generation_options(False)["num_predict"] == 1400


def test_real_api_contract_without_docker_or_cloud(monkeypatch):
    calls = []
    original_client = httpx.Client

    def blocked_docker(*_args, **_kwargs):
        raise AssertionError("Docker must not be called by native runtime")

    monkeypatch.setattr(legacy, "process", blocked_docker)

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        assert request.url.scheme == "http" and request.url.host == "127.0.0.1"
        if request.url.path == "/api/tags":
            return httpx.Response(200, json=inventory())
        if request.url.path == "/api/show":
            assert json.loads(request.content) == {"model": structured_ollama.MODEL}
            return httpx.Response(200, json={"details": {"format": "gguf"}})
        if request.url.path == "/api/chat":
            payload = json.loads(request.content)
            assert payload["model"] == structured_ollama.MODEL
            assert payload["options"]["num_predict"] == 768
            assert payload["format"]["additionalProperties"] is False
            assert payload["stream"] is False
            return httpx.Response(200, json={"model": structured_ollama.MODEL,
                                              "done": True, "eval_count": 220,
                                              "message": {"content": json.dumps({"function": FIXED})}})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [
                {"name": structured_ollama.MODEL, "digest": DIGEST, "size": 4683087561}
            ]})
        raise AssertionError("unapproved Ollama endpoint")

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        assert kwargs["base_url"] == "http://127.0.0.1:11434"
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(structured_ollama.httpx, "Client", client)
    provider = native.NativeStructuredOllama()
    reply = provider(task(), BEFORE)
    assert reply == json.dumps({"function": FIXED})
    assert provider.process_verified is True and provider.model_digest == DIGEST
    assert calls == ["/api/tags", "/api/show", "/api/chat", "/api/ps"]


def test_client_never_allows_non_loopback(monkeypatch):
    monkeypatch.setattr(native, "OLLAMA_URL", "https://some-remote-host.invalid")
    with pytest.raises(StructuredBlocked, match="native_loopback_required"):
        native.NativeStructuredOllama.no_cloud()
