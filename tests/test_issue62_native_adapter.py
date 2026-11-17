"""Gateway receipt contract tests; no Qwen or physical host is invoked."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.issue62_native_adapter import Issue62NativeAdapter, _module_scope
from app.issue62_typed_builder import ISSUE62_DESCRIPTOR
from app.native_one_shot import (
    AdapterContext, NativeExecutorBlocked, TaskManifest,
)

TASK = "cwp-" + "3" * 24
BASE = "a" * 40
PRODUCED = "b" * 40
DIGEST = "c" * 64
OLD = b"""from __future__ import annotations

OTHER = "unchanged"

def decide_next_action(task, checkpoint):
    return "blocked"
"""
NEW = OLD.replace(b'return "blocked"', b'return "builder"')


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def manifest(workspace_sha: str, source_sha: str) -> TaskManifest:
    return TaskManifest.parse({
        "schema_version": 1, "task_id": TASK,
        "repository": "ericson-j-santos/engineering-worker-pool",
        "base_sha": BASE, "workspace_sha": workspace_sha,
        "adapter_id": ISSUE62_DESCRIPTOR.adapter_id,
        "adapter_contract_sha256": ISSUE62_DESCRIPTOR.contract_sha256,
        "before_sha256": {"app/next_action_bridge.py": source_sha},
    })


def model_receipt(after=NEW) -> dict:
    return {
        "task_id": TASK, "base_sha": BASE, "before_sha256": digest(OLD),
        "after_sha256": digest(after), "model": "qwen2.5-coder:7b",
        "model_digest": DIGEST, "model_reply_sha256": "d" * 64,
        "model_calls": 1, "process_verified": True, "run_id": "attempt-001",
    }


def validation_receipt(after=NEW) -> dict:
    return {
        "task_id": TASK, "produced_sha": PRODUCED, "source_sha256": digest(after),
        "result": "VALIDATED", "passed": 193, "failed": 0, "skipped": 0,
    }


@pytest.fixture
def rig(tmp_path):
    source = tmp_path / "app" / "next_action_bridge.py"
    source.parent.mkdir()
    source.write_bytes(OLD)
    model = model_receipt()
    validation = validation_receipt()
    adapter = Issue62NativeAdapter(
        task_id=TASK, original=OLD, model_receipt_reader=lambda: model,
        validation_receipt_reader=lambda: validation,
        trusted_model_digest=DIGEST,
    )
    events = []

    def build():
        events.append("build")
        source.write_bytes(NEW)
        return {"result": "GOVERNED_COMMAND_VERIFIED", "head": BASE}

    def stage():
        events.append("stage")
        return {"result": "GOVERNED_COMMAND_VERIFIED", "head": BASE}

    def commit():
        events.append("commit")
        return {"result": "GOVERNED_COMMAND_VERIFIED", "head": PRODUCED}

    def validate():
        events.append("validate")
        return {"result": "GOVERNED_COMMAND_VERIFIED", "head": PRODUCED}

    command_map = {
        "issue62.build": build, "issue62.stage": stage,
        "issue62.commit": commit, "issue62.validate": validate,
    }

    def context(role: str) -> AdapterContext:
        is_validator = role == "validator"
        return AdapterContext(
            tmp_path,
            manifest(PRODUCED if is_validator else BASE,
                     digest(NEW if is_validator else OLD)),
            ISSUE62_DESCRIPTOR, command_map, role,
        )

    return {
        "adapter": adapter, "model": model, "validation": validation,
        "events": events, "source": source, "context": context,
    }


def test_real_adapter_contract_build_with_simulated_gateway(rig):
    result = rig["adapter"].build(rig["context"]("builder"))
    assert result.produced_sha == PRODUCED
    assert result.changed_paths == ("app/next_action_bridge.py",)
    assert result.invoked_commands == (
        "issue62.build", "issue62.stage", "issue62.commit",
    )
    assert len(result.evidence_sha256) == 64
    assert rig["events"] == ["build", "stage", "commit"]
    assert rig["adapter"].validated_model_receipt() == rig["model"]


def test_real_adapter_validator_contract_with_simulated_gateway(rig):
    rig["adapter"].build(rig["context"]("builder"))
    result = rig["adapter"].validate(rig["context"]("validator"), PRODUCED)
    assert result.produced_sha == PRODUCED
    assert result.invoked_commands == ("issue62.validate",)
    assert rig["events"] == ["build", "stage", "commit", "validate"]


@pytest.mark.parametrize("changed,value", [
    ("model_digest", "0" * 64),
    ("model", "other-model"),
    ("model_calls", 0),
    ("model_calls", True),
    ("process_verified", False),
    ("before_sha256", "0" * 64),
    ("after_sha256", "0" * 64),
    ("model_reply_sha256", "invalid"),
    ("run_id", "bad/run/id"),
    ("task_id", "cwp-" + "4" * 24),
])
def test_bad_model_receipt_prevents_stage_and_commit(rig, changed, value):
    rig["model"][changed] = value
    with pytest.raises(NativeExecutorBlocked, match="issue62_model_receipt_unverified"):
        rig["adapter"].build(rig["context"]("builder"))
    assert rig["events"] == ["build"]


def test_gated_adapter_rejects_unexpected_code_change(rig):
    original = rig["source"]
    def unsafe():
        rig["events"].append("build")
        original.write_bytes(NEW.replace(b'OTHER = "unchanged"', b'OTHER = "changed"'))
        return {"result": "GOVERNED_COMMAND_VERIFIED", "head": BASE}
    commands = {"issue62.build": unsafe,
                "issue62.stage": lambda: None,
                "issue62.commit": lambda: None,
                "issue62.validate": lambda: None}
    ctx = AdapterContext(original.parent.parent, manifest(BASE, digest(OLD)),
                         ISSUE62_DESCRIPTOR, commands, "builder")
    with pytest.raises(NativeExecutorBlocked, match="issue62_other_code_changed"):
        rig["adapter"].build(ctx)
    assert rig["events"] == ["build"]


def test_unverified_gateway_command_blocks_before_model_readback(rig):
    def unverified():
        rig["events"].append("build")
        return {"result": "HTTP_200", "head": BASE}
    commands = {"issue62.build": unverified,
                "issue62.stage": lambda: None,
                "issue62.commit": lambda: None,
                "issue62.validate": lambda: None}
    ctx = AdapterContext(rig["source"].parent.parent,
                         manifest(BASE, digest(OLD)), ISSUE62_DESCRIPTOR,
                         commands, "builder")
    with pytest.raises(NativeExecutorBlocked, match="issue62_command_receipt_unverified"):
        rig["adapter"].build(ctx)
    assert rig["events"] == ["build"]


@pytest.mark.parametrize("changed,value", [
    ("failed", 1),
    ("skipped", 1),
    ("passed", 192),
    ("produced_sha", "0" * 40),
    ("source_sha256", "0" * 64),
])
def test_validator_incomplete_or_foreign_report_blocks(rig, changed, value):
    rig["adapter"].build(rig["context"]("builder"))
    rig["validation"][changed] = value
    with pytest.raises(NativeExecutorBlocked, match="issue62_validator_evidence_unverified"):
        rig["adapter"].validate(rig["context"]("validator"), PRODUCED)


@pytest.mark.parametrize("modification,reason", [
    (OLD, "issue62_source_unchanged_or_oversized"),
    (NEW.replace(b"OTHER", b"EVIL"), "issue62_other_code_changed"),
    (NEW.replace(b"decide_next_action", b"different_function"),
     "issue62_function_identity_changed"),
    (NEW.replace(b"task, checkpoint", b"task, checkpoint, extra"),
     "issue62_signature_changed"),
])
def test_function_scope_refuses_other_changes(modification, reason):
    with pytest.raises(NativeExecutorBlocked, match=reason):
        _module_scope(OLD, modification)


def test_no_model_receipt_before_build_is_accepted(rig):
    with pytest.raises(NativeExecutorBlocked, match="issue62_model_receipt_not_available"):
        rig["adapter"].validated_model_receipt()


@pytest.mark.parametrize("replacement,reason", [
    (b'    import os\n    return "builder"', "issue62_unsafe_generated_syntax"),
    (b'    while True:\n        return "builder"', "issue62_unsafe_generated_syntax"),
    (b'    return eval("1")', "issue62_unapproved_call"),
    (b'    return task.__class__', "issue62_dunder_forbidden"),
    (b'    def nested():\n        return "builder"\n    return nested()',
     "issue62_unsafe_generated_syntax"),
])
def test_generated_code_cannot_escape_audited_function_allowlist(replacement, reason):
    candidate = OLD.replace(b'    return "blocked"', replacement)
    with pytest.raises(NativeExecutorBlocked, match=reason):
        _module_scope(OLD, candidate)


def test_real_bridge_source_enforces_existing_call_allowlist():
    source = Path("app/next_action_bridge.py").read_bytes()
    previous = b'    if state in {"blocked", "failed"}:'
    assert source.count(previous) == 1
    modified = source.replace(previous, b'    if state in {"blocked", "failed", "queued"}:')
    _module_scope(source, modified)
    # This is ONLY a scope test; it is not an acceptable behavior change.
    unapproved = modified.replace(
        b'    if state in {"blocked", "failed", "queued"}:',
        b'    if eval("1") in {"blocked", "failed", "queued"}:',
    )
    with pytest.raises(NativeExecutorBlocked, match="issue62_unapproved_call"):
        _module_scope(source, unapproved)
