"""Native Qwen transport for the Noteri #42 source defect.

All responses here come from httpx.MockTransport; no model call, CAS, host
or READY_FOR_PR is claimed. The real E2E requires an installed Qwen model.
"""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from app import noteri42_native_qwen as m
from app.local_code_worker import WorkerBlocked
from app.structured_code_worker import StructuredBlocked


PIN = "a" * 64
SOURCE = (
    "def task_status():\n    return {}\n"
    "def task_headless_ready(task):\n    return True\n"
    "def attest_installed_release(metadata: dict[str, Any], expected_sha: str, source: Path) -> None:\n"
    "    raise LauncherError('WATCHDOG_INSTALLED_TASK_UNVERIFIED')\n"
)
FUNC = (
    "def attest_installed_release(metadata: dict[str, Any], expected_sha: str, source: Path) -> None:\n"
    "    raise LauncherError('WATCHDOG_INSTALLED_TASK_ACTION_MISMATCH')\n"
)


def _response(body):
    return httpx.Response(
        200, headers={"Content-Type": "application/json"}, json=body
    )


def provider(monkeypatch, *, changes=None):
    changes = changes or {}
    calls = []
    reply = json.dumps({"function": FUNC})

    def handler(req):
        calls.append((req.method, req.url.path))
        assert req.url.host == "127.0.0.1"
        assert req.url.port == 11434
        assert req.method in ("GET", "POST")
        path = req.url.path
        if path == "/api/tags":
            value = {"models": [{
                "name": m.MODEL, "digest": PIN, "size": 100,
                "details": {"format": "gguf"},
            }]}
        elif path == "/api/show":
            value = {"details": {"format": "gguf"}}
        elif path == "/api/chat":
            request = json.loads(req.content)
            assert request["model"] == "qwen2.5-coder:7b"
            assert request["stream"] is False
            assert request["messages"][0]["role"] == "system"
            assert "Noteri #42" in request["messages"][1]["content"]
            assert m.SOURCE_SHA in request["messages"][1]["content"]
            assert request["messages"][1]["content"].endswith(SOURCE)
            value = {"done": True, "model": m.MODEL, "eval_count": 28,
                     "message": {"content": reply}}
        elif path == "/api/ps":
            value = {"models": [{"name": m.MODEL, "digest": PIN, "size": 100}]}
        else:
            raise AssertionError(path)
        if path in changes:
            value = changes[path]
        return _response(value)

    client = httpx.Client(
        base_url=m.OLLAMA_URL,
        transport=httpx.MockTransport(handler),
        trust_env=False, follow_redirects=False,
    )
    monkeypatch.setenv("NATIVE_OLLAMA_DEV", "1")
    monkeypatch.setenv("NATIVE_MODEL_DIGEST_PREFIX", PIN[:12])
    monkeypatch.setattr(m.httpx, "Client", lambda **_: client)
    return calls


def test_model_reply_is_proposal_only_and_one_shot(monkeypatch):
    calls = provider(monkeypatch)
    model = m.Noteri42NativeQwen()
    result = model.propose(source_sha=m.SOURCE_SHA, source_text=SOURCE)
    assert result == FUNC
    assert model.request_count == 1
    assert model.process_verified is True
    assert model.model_digest == PIN
    assert len(model.last_model_reply_sha256) == 64
    assert calls == [
        ("GET", "/api/tags"), ("POST", "/api/show"),
        ("POST", "/api/chat"), ("GET", "/api/ps"),
    ]
    with pytest.raises(StructuredBlocked, match="noteri42_model_retry_forbidden"):
        model.propose(source_sha=m.SOURCE_SHA, source_text=SOURCE)
    assert len(calls) == 4


def test_source_sha_or_invalid_source_never_contacts_model(monkeypatch):
    calls = provider(monkeypatch)
    for sha, body, reason in (
        ("f" * 40, SOURCE, "noteri42_source_sha_invalid"),
        (m.SOURCE_SHA, "", "noteri42_source_invalid"),
        (m.SOURCE_SHA, "def task_status():\n pass\n", "noteri42_source_invalid"),
    ):
        with pytest.raises(StructuredBlocked, match=reason):
            m.Noteri42NativeQwen().propose(source_sha=sha, source_text=body)
    assert calls == []


def test_cloud_optin_or_digest_missing_never_contacts_model(monkeypatch):
    calls = provider(monkeypatch)
    monkeypatch.delenv("NATIVE_OLLAMA_DEV")
    with pytest.raises(StructuredBlocked, match="native_dev_opt_in_required"):
        m.Noteri42NativeQwen().propose(source_sha=m.SOURCE_SHA, source_text=SOURCE)
    assert calls == []


@pytest.mark.parametrize("change,reason", [
    ({"/api/tags": {"models": []}}, "native_model_ambiguous_or_missing"),
    ({"/api/tags": {"models": [{
        "name": "qwen2.5-coder:7b", "digest": "f" * 64, "size": 100,
        "details": {"format": "gguf"},
    }]}}, "native_model_digest_mismatch"),
    ({"/api/show": {"remote_model": "foreign", "details": {"format": "gguf"}}},
     "noteri42_model_weights_unverified"),
    ({"/api/chat": {"done": True, "model": "other", "eval_count": 22,
                   "message": {"content": json.dumps({"function": FUNC})}}},
     "noteri42_model_inference_unverified"),
    ({"/api/chat": {"done": False, "model": "qwen2.5-coder:7b",
                   "eval_count": 0, "message": {"content": "{}"}}},
     "noteri42_model_inference_unverified"),
    ({"/api/ps": {"models": []}}, "noteri42_model_process_unverified"),
    ({"/api/chat": {"done": True, "model": "qwen2.5-coder:7b",
                   "eval_count": 5, "message": {"content": "{}"}}},
     "noteri42_model_reply_shape"),
])
def test_bad_provenance_or_reply_blocks(monkeypatch, change, reason):
    provider(monkeypatch, changes=change)
    model = m.Noteri42NativeQwen()
    with pytest.raises((WorkerBlocked, StructuredBlocked), match=reason):
        model.propose(source_sha=m.SOURCE_SHA, source_text=SOURCE)
    assert model.process_verified is False
    assert model.model_digest == ""


@pytest.mark.parametrize("candidate", [
    "pass\n" + FUNC,
    FUNC + "\nimport os\n",
    FUNC + "\nopen('would-run.py', 'w').write('bad')\n",
    FUNC + "\ndef another():\n    pass\n",
    "@decorator\n" + FUNC,
    "def wrap():\n    " + FUNC.replace("\n", "\n    "),
])
def test_qwen_proposal_rejects_extra_top_level_code(candidate):
    with pytest.raises(StructuredBlocked, match="noteri42_model_reply_shape"):
        m._json_reply(json.dumps({"function": candidate}))


def test_qwen_proposal_rejects_syntax_error():
    invalid = FUNC + "\nif (\n"
    with pytest.raises(ValueError, match="noteri42_model_function_syntax"):
        m._json_reply(json.dumps({"function": invalid}))


def test_qwen_proposal_accepts_exactly_one_function():
    assert m._json_reply(json.dumps({"function": FUNC})) == FUNC

def test_bogus_json_and_duplicate_json_fields_fail_closed():
    with pytest.raises((ValueError, StructuredBlocked)):
        m._json_reply("not json")
    with pytest.raises(StructuredBlocked, match="noteri42_model_duplicate_json_key"):
        m._json_reply('{"function": "x", "function": "y"}')
    with pytest.raises(StructuredBlocked, match="noteri42_model_reply_shape"):
        m._json_reply('{"function": "def unrelated(): pass"}')
    with pytest.raises(StructuredBlocked, match="noteri42_model_reply_size"):
        m._json_reply("")
