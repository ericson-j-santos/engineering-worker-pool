"""Fail-closed progress receipts for the real governed structured-function E2E.

All transport calls in these tests are deterministic fixtures; no Ollama, host
or live TODO Global is used. Real-model E2E remains required for merge.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from structured_worker_e2e import (  # noqa: E402
    DiagnosticStructuredOllama, record_model_progress, StructuredBlocked,
)
from app.structured_ollama import StructuredOllama  # noqa: E402


SHA = "5f1cbc18417c6e9fae778246bde87f8bcbad3fbe"


def payload():
    return {
        "sha": SHA, "correlation": "structured-local-code-test",
        "phase": "revision", "step": "generation", "event": "started",
        "elapsed_ms": 280001,
    }


def test_progress_receipt_is_atomic_and_replay_idempotent(tmp_path):
    target = tmp_path / "model-progress.json"
    record_model_progress(target, **payload())
    first = target.read_bytes()
    record_model_progress(target, **payload())
    assert target.read_bytes() == first
    content = json.loads(first)
    assert content["expected_sha"] == SHA
    assert content["phase"] == "revision"
    assert content["elapsed_ms"] == 280001
    assert len(list(tmp_path.iterdir())) == 1


def test_new_stage_overwrites_old_without_duplicate_history(tmp_path):
    target = tmp_path / "model-progress.json"
    record_model_progress(target, **payload())
    later = payload()
    later.update(step="process_readback", event="completed", elapsed_ms=280110)
    record_model_progress(target, **later)
    result = json.loads(target.read_text())
    assert result["step"] == "process_readback"
    assert result["event"] == "completed"
    assert "generation" not in target.read_text()
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize(("field", "value"), [
    ("phase", "private"),
    ("step", "/etc/passwd"),
    ("event", "PRIVATE_TOKEN"),
    ("elapsed_ms", True),
    ("elapsed_ms", -1),
    ("elapsed_ms", 3_600_001),
])
def test_invalid_or_unbounded_progress_fails_closed(tmp_path, field, value):
    target = tmp_path / "model-progress.json"
    data = payload()
    data[field] = value
    with pytest.raises(StructuredBlocked, match="^model_progress_invalid$"):
        record_model_progress(target, **data)
    assert not target.exists()


def test_local_transport_start_complete_and_failure_are_sanitized(tmp_path, monkeypatch):
    def fake_request(_client, method, path, **_kwargs):
        if (method, path) == ("POST", "/api/show"):
            raise ValueError("PRIVATE_TOKEN")
        return {"done": True}
    monkeypatch.setattr(StructuredOllama, "request", staticmethod(fake_request))
    target = tmp_path / "progress.json"
    calls = []
    provider = DiagnosticStructuredOllama()

    def observer(step, event):
        calls.append((step, event))
        record_model_progress(
            target, sha=SHA, correlation="test", phase="revision",
            step=step, event=event, elapsed_ms=len(calls),
        )
    provider.progress_observer = observer
    assert provider.request(None, "POST", "/api/chat") == {"done": True}
    assert calls == [("generation", "started"), ("generation", "completed")]
    with pytest.raises(ValueError, match="PRIVATE_TOKEN"):
        provider.request(None, "POST", "/api/show")
    assert calls[-2:] == [("weights", "started"), ("weights", "failed")]
    assert "PRIVATE_TOKEN" not in target.read_text()


def test_unlisted_transport_is_rejected_before_network(monkeypatch):
    calls = []
    monkeypatch.setattr(StructuredOllama, "request", staticmethod(
        lambda *_args, **_kwargs: calls.append("NETWORK")))
    provider = DiagnosticStructuredOllama()
    with pytest.raises(StructuredBlocked, match="^model_transport_unrecognized$"):
        provider.request(None, "POST", "/admin/reconfigure")
    assert calls == []


def test_timeout_checkpoint_is_in_outer_artifact_copy_contract():
    script = (ROOT / "scripts/structured_worker_e2e.py").read_text(encoding="utf-8")
    assert '"model-progress.json"' in script.split("for name in (", 1)[-1]
    assert "timeout=435" in script and '"--timeout", "420"' in script

def test_budget_covers_two_bounded_model_calls_and_ci_time_limits():
    """Fixed ceilings allow two 180s calls plus 60s for guards and validation."""
    worker = (ROOT / "app/structured_code_worker.py").read_text(encoding="utf-8")
    provider = (ROOT / "app/structured_ollama.py").read_text(encoding="utf-8")
    outer = (ROOT / "scripts/structured_worker_e2e.py").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "for attempt in (1, 2):" in worker
    assert "timeout=180" in provider
    model_calls, model_call_timeout = 2, 180
    gateway_timeout, wrapper_timeout, step_timeout, job_timeout = 420, 435, 8 * 60, 14 * 60
    assert model_call_timeout < 300  # Material first-proposal evidence resets the watchdog.
    assert gateway_timeout >= model_calls * model_call_timeout + 60
    assert gateway_timeout < wrapper_timeout < step_timeout < job_timeout
    assert '"--timeout", "420"' in outer and "timeout=435" in outer
    assert '    timeout-minutes: 14' in workflow
    assert workflow.count("      - name: Repair real structured function in governed copy") == 1
    assert '        timeout-minutes: 8' in workflow.split(
        "      - name: Repair real structured function in governed copy", 1)[1].split(
        "      - name: Upload structured-function evidence and patch", 1)[0]
    assert not (gateway_timeout - 1 >= model_calls * model_call_timeout + 60)
