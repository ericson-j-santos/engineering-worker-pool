"""CI-only, read-only GitHub -> real local Qwen7B -> independent readback.

The model server must already exist at loopback in an isolated CI runtime.
Never installs a model, accesses protected hosts, or dispatches workers.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os

from agent_core.pr_context import ContextGitHubHTTPS
from app.agent_rag_consumer import (
    AgentCoreResearchBridge, NativeQwenResearchPlanner,
    ResearchBlocked, ResearchTask, research_pr,
)

REPO = "ericson-j-santos/agent-core"
PR = 9
# Reviewed PR #9 head, not merge commit. The doc exists at this exact SHA.
HEAD = "b083a23af7c55f96ee1c7f8daabbd51167bf4de6"
DOC = "docs/PR_CONTEXT_RETRIEVAL.md"
QUESTION = "Is the GitHub PR context retrieval capability read-only?"


def _require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def main() -> None:
    _require(os.environ.get("GITHUB_ACTIONS") == "true",
             "rag_ci_environment_required")
    _require(os.environ.get("GITHUB_REPOSITORY") ==
             "ericson-j-santos/engineering-worker-pool",
             "rag_ci_repository_invalid")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    _require(run_id.isdigit(), "rag_ci_run_id_missing")

    identity = f"rag-worker-{run_id}-github-pr9"
    task = ResearchTask(
        repository=REPO, pr_number=PR, expected_head=HEAD,
        correlation_id=identity, question=QUESTION,
        source_paths=(DOC,),
    )
    token = os.environ.get("GITHUB_TOKEN") or None

    # Negative control against the real GitHub API; no Ollama call allowed.
    reader = ContextGitHubHTTPS(token)
    planner = NativeQwenResearchPlanner()
    stale_task = ResearchTask(
        repository=REPO, pr_number=PR, expected_head="f" * 40,
        correlation_id=identity, question=QUESTION, source_paths=(DOC,),
    )
    try:
        AgentCoreResearchBridge(reader, planner).research(stale_task)
    except ResearchBlocked as exc:
        _require(str(exc) == "rag_head_stale_before_model",
                 "rag_stale_preflight_unexpected_block")
    else:
        raise RuntimeError("rag_stale_head_was_accepted")
    _require(planner.calls == 0, "rag_negative_spent_model_calls")

    observed = research_pr(task, github_token=token)
    _require(observed["schema_version"] == "worker.rag.evidence-v1",
             "rag_evidence_contract")
    _require(observed["state"] in ("supported", "insufficient_evidence"),
             "rag_retrieval_state_invalid")
    if observed["state"] != "supported":
        code = ("rag_retrieval_no_matching_excerpt"
                if observed["model_calls"] <= 1
                else "rag_model_selection_declined_citation")
        raise RuntimeError(code)
    _require(observed["head_sha"] == HEAD, "rag_head_mismatch")
    _require(observed["model_calls"] >= 2, "rag_model_selection_missing")
    _require(observed["model_process_verified"] is True,
             "rag_real_model_process_unverified")
    _require(observed["pr_readback_stable"] is True,
             "rag_pr_readback_unproved")
    _require(observed["execution_authorized"] is False and
             observed["state_changed"] is False,
             "rag_readonly_boundary_broken")
    citation = observed["selected_reference"]
    _require(type(citation) is dict and
             citation["path"] == DOC and
             citation["untrusted_source_text"] is True and
             citation["url"].startswith(
                 f"https://github.com/{REPO}/blob/{HEAD}/{DOC}#L"),
             "rag_real_citation_unproved")
    _require(observed["model_digest"].startswith(
        os.environ["NATIVE_MODEL_DIGEST_PREFIX"]),
        "rag_model_pin_unproved")

    # Independently fetch the PR and bytes from GitHub *after* model inference.
    independent = ContextGitHubHTTPS(token)
    pr = independent.get_pull_request(REPO, PR)
    _require(pr.get("head", {}).get("sha") == HEAD and
             pr.get("base", {}).get("sha") == observed["base_sha"] and
             pr.get("head", {}).get("repo", {}).get("full_name") == REPO,
             "rag_independent_pr_readback_failed")
    file = independent.get_text_file(REPO, DOC, HEAD)
    raw = base64.b64decode(file["content"].replace("\n", ""), validate=True)
    digest = hashlib.sha256(raw).hexdigest()
    _require(digest == citation["source_sha256"] and
             digest == observed["sources"][0]["sha256"] and
             len(raw) == file["size"],
             "rag_independent_source_readback_failed")
    _require(citation["excerpt"] in raw.decode("utf-8"),
             "rag_citation_not_in_real_source")
    record = dict(observed)
    receipt = record.pop("receipt_sha256")
    canonical = json.dumps(
        record, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    _require(hashlib.sha256(canonical.encode()).hexdigest() == receipt,
             "rag_receipt_integrity_failed")

    print(json.dumps({
        "rag_real_github_and_native_qwen_e2e": True,
        "correlation_id": identity,
        "pr_head_sha": HEAD,
        "model_digest_prefix": observed["model_digest"][:12],
        "model_calls": observed["model_calls"],
        "source_path": DOC,
        "source_sha256": digest,
        "citation_line": citation["line"],
        "independent_pr_and_source_readback": True,
        "stale_head_negative_no_model_calls": True,
        "no_git_writes_or_worker_dispatch": True,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
