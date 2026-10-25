"""CAS -> native issue Builder and Validator handoff (simulated GitHub ports).

This file tests the durable handoff seam with independently constructed API
clients. It is NOT evidence of a real Qwen call or cross-host GitHub write.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.github_cas_transport import GitHubObjectCAS
from app.issue_draft_pr import HandoffBlocked, PATH, PROFILE, REPO, sha256
from app.issue_native_adapter import DESCRIPTOR
from app.issue_rag_durable_adapter import adapter_from_durable_issue_rag
from app.native_one_shot import DurableArtifact, TaskManifest
from app.native_structured_ollama import NativeStructuredOllama
from test_issue_rag_receipt_cas import (
    CONTENT, GitHubState, Port, load, publish, receive_context, setup,
)

SOURCE = (Path(__file__).resolve().parents[1] / PATH).read_bytes()
TASK = "cwp-" + "a" * 24


def base_manifest():
    return TaskManifest.parse({
        "schema_version": 1, "task_id": TASK,
        "repository": REPO, "base_sha": "a" * 40,
        "workspace_sha": "a" * 40,
        "adapter_id": PROFILE,
        "adapter_contract_sha256": DESCRIPTOR.contract_sha256,
        "before_sha256": {PATH: sha256(SOURCE)},
    })


def independent_store(cas):
    """Construct distinct clients, sharing only the simulated remote backend."""
    state = cas.writer.port.state
    binding = cas.binding
    from app.issue_rag_receipt_cas import IssueRAGReceiptCAS
    return IssueRAGReceiptCAS(
        writer=GitHubObjectCAS(Port(state), binding),
        independent=GitHubObjectCAS(Port(state, readonly=True), binding),
        binding=binding,
    )


def factory(cas, artifact, tmp_path, *, source=SOURCE, m=None,
            generation=1, attempt="attempt-1", effect="effect-rag-v1",
            model=None, source_reader=receive_context):
    return adapter_from_durable_issue_rag(
        cas=cas, artifact=artifact, binding=cas.binding,
        base_manifest=m or base_manifest(),
        lease_generation=generation, attempt_id=attempt,
        effect_id=effect, original_source=source,
        state_directory=tmp_path / "state",
        model=model or NativeStructuredOllama(),
        regression_runner=lambda _: {
            "exit_code": 0, "category": "passed",
            "output_within_limit": True, "passed_count": 52,
            "failed_count": 0, "error_count": 0,
        },
        independent_source_read=source_reader,
    )


def test_materialized_rag_receipt_creates_native_adapter_without_model_calls(tmp_path, monkeypatch):
    cas, state = setup()
    cas.preflight()
    artifact = publish(cas, m=base_manifest())
    model = NativeStructuredOllama()
    calls = []

    def should_not_infer(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("materializing CAS must not invoke Qwen")

    monkeypatch.setattr(NativeStructuredOllama, "__call__", should_not_infer)
    resumed = independent_store(cas)
    adapter = factory(resumed, artifact, tmp_path, model=model)
    assert adapter.rag_receipt["schema_version"] == "worker.rag.issue-context-v1"
    assert adapter.rag_receipt["issue_number"] == cas.binding.number
    assert adapter.rag_receipt["base_sha"] == cas.binding.base_sha
    assert adapter.task_id == TASK
    assert adapter.propose is model
    assert adapter.before == SOURCE
    assert calls == []
    assert state.write_count == 2 and state.branch_count == 1


def test_idempotent_cross_client_materialization_never_republishes(tmp_path):
    cas, state = setup()
    cas.preflight()
    artifact = publish(cas, m=base_manifest())
    before = state.write_count
    first = factory(independent_store(cas), artifact, tmp_path)
    second = factory(independent_store(cas), artifact, tmp_path)
    assert first.rag_receipt == second.rag_receipt
    assert first.task_id == second.task_id == TASK
    assert state.write_count == before == 2
    assert state.branch_count == 1


@pytest.mark.parametrize("bad", [
    "wrong_original_bytes", "wrong_task", "wrong_repository",
    "wrong_adapter", "produced_manifest", "untrusted_model", "wrong_binding",
])
def test_invalid_adapter_inputs_block_before_cas_read(tmp_path, bad):
    cas, state = setup()
    cas.preflight()
    artifact = publish(cas, m=base_manifest())
    other = independent_store(cas)
    checks_before = state.reads
    m = base_manifest()
    values = dict(
        cas=other, artifact=artifact, binding=cas.binding,
        base_manifest=m, lease_generation=1, attempt_id="attempt-1",
        effect_id="effect-rag-v1", original_source=SOURCE,
        state_directory=tmp_path, model=NativeStructuredOllama(),
        regression_runner=lambda _: {}, independent_source_read=receive_context,
    )
    if bad == "wrong_original_bytes":
        values["original_source"] = SOURCE + b"\nchanged"
    elif bad == "untrusted_model":
        values["model"] = lambda *_: "fake reply"
    elif bad == "wrong_binding":
        values["binding"] = replace(cas.binding, number=9)
    else:
        raw = {
            "schema_version": m.schema_version, "task_id": m.task_id,
            "repository": m.repository, "base_sha": m.base_sha,
            "workspace_sha": m.workspace_sha, "adapter_id": m.adapter_id,
            "adapter_contract_sha256": m.adapter_contract_sha256,
            "before_sha256": dict(m.before_sha256),
        }
        if bad == "wrong_task":
            raw["task_id"] = "cwp-" + "b" * 24
            # The signed envelope would prevent a later materialization.
            # The Adapter factory does not yet know the correct task ID,
            # so it is checked by the durable CAS on the next read.
            values["base_manifest"] = TaskManifest.parse(raw)
            with pytest.raises(HandoffBlocked, match="rag_cas_envelope_identity_invalid"):
                adapter_from_durable_issue_rag(**values)
            assert state.reads > checks_before
            return
        elif bad == "wrong_repository":
            raw["repository"] = "other/other"
        elif bad == "wrong_adapter":
            raw["adapter_id"] = "other.adapter.v1"
        else:
            raw["workspace_sha"] = "c" * 40
        values["base_manifest"] = TaskManifest.parse(raw)
    with pytest.raises(HandoffBlocked, match="^rag_durable_adapter_(identity_invalid|base_drift|handlers_invalid)$"):
        adapter_from_durable_issue_rag(**values)
    assert state.reads == checks_before


@pytest.mark.parametrize("tamper", [
    "lease", "attempt", "effect", "artifact_sha", "source_change", "revoke_owner",
])
def test_cross_task_tamper_and_revocation_block_before_adapter(tmp_path, tamper):
    cas, state = setup()
    cas.preflight()
    artifact = publish(cas, m=base_manifest())
    other = independent_store(cas)
    gen, attempt, effect, source_reader = 1, "attempt-1", "effect-rag-v1", receive_context
    if tamper == "lease":
        gen = 2
    elif tamper == "attempt":
        attempt = "new-attempt"
    elif tamper == "effect":
        effect = "new-effect"
    elif tamper == "artifact_sha":
        artifact = DurableArtifact(
            artifact.artifact_kind, artifact.artifact_ref[:-1] + "0",
            artifact.artifact_sha256,
        )
    elif tamper == "source_change":
        source_reader = lambda *_: CONTENT + b"forged"
    else:
        state.reject_owner = True

    with pytest.raises(HandoffBlocked):
        factory(other, artifact, tmp_path, generation=gen, attempt=attempt,
                effect=effect, source_reader=source_reader)
    assert state.write_count == 2


def test_independent_source_read_error_is_sanitized_without_adapter(tmp_path):
    cas, state = setup()
    cas.preflight()
    artifact = publish(cas, m=base_manifest())
    with pytest.raises(HandoffBlocked) as blocked:
        factory(independent_store(cas), artifact, tmp_path,
                source_reader=lambda *_: (_ for _ in ()).throw(
                    RuntimeError("sensitive session secret")))
    assert "sensitive session secret" not in str(blocked.value)
    assert state.write_count == 2
