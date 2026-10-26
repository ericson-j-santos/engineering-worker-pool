"""Hosted-only real Qwen7B issue #8 -> GitHub evidence citation gate.

Unlike synthetic issue tests, this calls the actual approved issue/comment/main
and the real local Ollama process in CI. All GitHub operations are GET-only.
It cannot claim the issue, write CAS, patch code or publish any branch/PR.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timezone

from agent_core.pr_context import _file_data
from app.agent_rag_consumer import NativeQwenResearchPlanner
from app.issue_draft_pr import HandoffBlocked, admit, REPO
from app.issue_rag_handoff import QUESTION, bind_issue_rag, digest, canonical
from app.issue_rag_research import (
    IssueRAGResearch, official_issue_reader, research_issue,
)

MAIN = "047c77b6b867ff5d489bd74f6fd80989719ab85e"
ISSUE = 8
COMMENT_ID = 6423646539
DOC = "docs/ISSUE_QWEN7B_DRAFT_HANDOFF.md"


def need(ok: bool, code: str) -> None:
    if not ok:
        raise RuntimeError(code)


def main() -> None:
    need(os.environ.get("GITHUB_ACTIONS") == "true",
         "issue8_rag_ci_environment_required")
    need(os.environ.get("GITHUB_REPOSITORY") == REPO,
         "issue8_rag_ci_repository_invalid")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    need(run_id.isdigit(), "issue8_rag_run_identity_missing")
    token = os.environ.get("GITHUB_TOKEN") or None

    reader = official_issue_reader(token)
    now = datetime.now(timezone.utc)
    issue = reader.get_issue(ISSUE)
    comment = reader.get_comment(COMMENT_ID)
    branch = reader.get_branch("main")
    approved = admit(
        issue, comment, branch, base_sha=MAIN,
        observed_at=now.isoformat(), now=now,
    )

    # Independently demonstrate stale SHAs are rejected BEFORE Qwen is called.
    planner = NativeQwenResearchPlanner()
    stale = replace(approved, base_sha="f" * 40)
    try:
        IssueRAGResearch(
            reader=reader, independent_reader=official_issue_reader(token),
            planner=planner, approved_paths=(DOC,),
        ).research(stale)
    except HandoffBlocked as exc:
        need(str(exc) == "main_head_drift", "issue8_stale_sha_wrong_gate")
    else:
        raise RuntimeError("issue8_stale_sha_unexpectedly_admitted")
    need(planner.calls == 0, "issue8_stale_head_spent_model_calls")

    observed = research_issue(approved, github_token=token)
    need(observed.get("schema_version") == "worker.rag.issue-context-v1"
         and observed.get("state") == "supported"
         and observed.get("issue_number") == ISSUE
         and observed.get("request_id") == approved.request_id
         and observed.get("base_sha") == MAIN
         and observed.get("head_sha") == MAIN
         and observed.get("model_calls") == 2
         and observed.get("model_process_verified") is True
         and observed.get("issue_head_readback_stable") is True
         and observed.get("execution_authorized") is False
         and observed.get("state_changed") is False,
         "issue8_real_qwen_evidence_unproved")

    independent = official_issue_reader(token)

    def source_reader(path: str, sha: str) -> bytes:
        need((path, sha) == (DOC, MAIN),
             "issue8_source_identity_invalid")
        value, _, _, _ = _file_data(
            independent.get_text_file(REPO, path, sha), path
        )
        return value.encode("utf-8")

    cited = bind_issue_rag(approved, observed, source_reader)
    # Require a literal single-source answer, not keyword success,
    # regression evidence from a different line, or model-fabricated facts.
    need(cited.source_path == DOC
         and "Builder" in cited.excerpt
         and "42/42" in cited.excerpt
         and cited.base_sha == MAIN,
         "issue8_builder_claim_not_supported")
    # The regression obligation is verified separately by the Validator.
    need("regressões" not in QUESTION.lower()
         and "casos" in QUESTION.lower(),
         "issue8_question_not_single_claim")
    current = independent.get_branch("main")
    need(current.get("commit", {}).get("sha") == MAIN,
         "issue8_main_drift_after_inference")
    need(digest(canonical({
        k: value for k, value in observed.items()
        if k != "receipt_sha256"
    })) == cited.receipt_sha256,
         "issue8_receipt_integrity_not_independent")

    print(json.dumps({
        "result": "ISSUE8_REAL_QWEN_CITATION_VERIFIED",
        "correlation_id": "issue8-rag-ci-" + run_id,
        "issue_number": ISSUE,
        "worker_sha": MAIN,
        "source_path": DOC,
        "citation_line": cited.line,
        "source_sha256": cited.source_sha256,
        "model_digest_prefix": cited.model_digest[:12],
        "model_calls": observed["model_calls"],
        "independent_source_readback": True,
        "stale_sha_blocked_before_model": True,
        "code_modified": False,
        "cas_write": False,
        "draft_pr_created": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
