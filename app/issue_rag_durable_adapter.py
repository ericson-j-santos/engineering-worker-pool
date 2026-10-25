"""Read a durable issue-bound RAG receipt before constructing the Qwen Builder.

Read-only materialization bridge. The actual Worker Pool lease, Gateway session,
model execution, patch CAS, independent Validator and draft PR are governed by
their existing separate contracts; importing this module authorizes none.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from app.issue_draft_pr import (
    HandoffBlocked, IssueBinding, PATH, PROFILE, REPO, require, sha256,
)
from app.issue_native_adapter import DESCRIPTOR, IssueQwenNativeAdapter
from app.issue_rag_receipt_cas import IssueRAGReceiptCAS, KIND
from app.native_one_shot import DurableArtifact, TaskManifest
from app.native_structured_ollama import NativeStructuredOllama


def adapter_from_durable_issue_rag(
    *, cas: IssueRAGReceiptCAS, artifact: DurableArtifact,
    binding: IssueBinding, base_manifest: TaskManifest,
    lease_generation: int, attempt_id: str, effect_id: str,
    original_source: bytes, state_directory: Path,
    model: NativeStructuredOllama,
    regression_runner: Callable[[Path], Mapping[str, Any]],
    independent_source_read: Callable[[str, str], bytes],
) -> IssueQwenNativeAdapter:
    """Reconstitute the same verified context on Builder or Validator host.

    The caller must supply the independently verified *BASE* TaskManifest
    even on a Validator host whose checkout HEAD is the produced SHA; the
    separate AdapterContext for Validator remains pinned to produced_sha.
    Never pass model-supplied manifests, CAS URLs or arbitrary source paths.
    """
    require(type(cas) is IssueRAGReceiptCAS
            and type(binding) is IssueBinding
            and cas.binding == binding
            and type(base_manifest) is TaskManifest
            and type(artifact) is DurableArtifact
            and artifact.artifact_kind == KIND,
            "rag_durable_adapter_identity_invalid")
    require(base_manifest.repository == REPO
            and base_manifest.adapter_id == PROFILE
            and base_manifest.adapter_contract_sha256 == DESCRIPTOR.contract_sha256
            and base_manifest.base_sha == binding.base_sha
            and base_manifest.workspace_sha == binding.base_sha
            and set(base_manifest.before_sha256) == {PATH}
            and type(original_source) is bytes
            and 0 < len(original_source) <= 262_144
            and base_manifest.before_sha256[PATH] == sha256(original_source),
            "rag_durable_adapter_base_drift")
    require(type(model) is NativeStructuredOllama
            and callable(regression_runner)
            and callable(independent_source_read),
            "rag_durable_adapter_handlers_invalid")

    # CAS.read checks owner-approved issue/main, SHA, fenced task identity,
    # immutable envelope bytes and a fresh independently fetched source.
    # A receipt from another lease/issue/worker cannot be silently rebound.
    receipt, citation = cas.materialize(
        artifact=artifact, manifest=base_manifest,
        lease_generation=lease_generation, attempt_id=attempt_id,
        effect_id=effect_id,
        independent_source_read=independent_source_read,
    )
    require(citation.issue == binding.number
            and citation.base_sha == binding.base_sha
            and citation.request_id == binding.request_id
            and citation.correlation_id == binding.correlation_id,
            "rag_durable_adapter_context_drift")
    # The existing Builder revalidates the same bytes before inference; the
    # Validator revalidates them again on its own host/checkout.
    return IssueQwenNativeAdapter(
        binding=binding, task_id=base_manifest.task_id,
        before=original_source, state_directory=state_directory,
        propose=model, regression_runner=regression_runner,
        require_native_model=True, rag_receipt=receipt,
        rag_source_reader=independent_source_read,
    )
