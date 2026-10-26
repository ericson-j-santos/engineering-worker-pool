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

MAIN = "be69b27455f99606a03ce5cba6ca809e1082f2b4"
ISSUE = 8
COMMENT_ID = 6427072933
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

    # Inspect actual RAG input line numbers; never log excerpts or model reply.
    candidate_trace: list[dict[str, object]] = []
    observed = research_issue(
        approved, github_token=token, diagnostic_sink=candidate_trace,
    )
    need(observed.get("schema_version") == "worker.rag.issue-context-v1"
         and observed.get("state") in {"supported", "insufficient_evidence"}
         and observed.get("issue_number") == ISSUE
         and observed.get("request_id") == approved.request_id
         and observed.get("base_sha") == MAIN
         and observed.get("head_sha") == MAIN
         and type(observed.get("model_calls")) is int
         and 1 <= observed["model_calls"] <= 4
         and observed.get("model_process_verified") is True
         and observed.get("issue_head_readback_stable") is True
         and observed.get("execution_authorized") is False
         and observed.get("state_changed") is False,
         "issue8_real_qwen_identity_unproved")

    # This trace is deliberately outside the strict signed receipt schema.
    # Validate original bytes via a separate official GitHub GET client.
    independent = official_issue_reader(token)
    source_receipts = observed.get("sources")
    need(type(source_receipts) is list and len(source_receipts) == 1
         and type(candidate_trace) is list and len(candidate_trace) == 1
         and type(observed.get("receipt_sha256")) is str
         and digest(canonical({
             k: value for k, value in observed.items()
             if k != "receipt_sha256"
         })) == observed["receipt_sha256"],
         "issue8_candidate_receipt_unverified")
    trace = candidate_trace[0]
    expected_source = source_receipts[0]
    need(type(trace) is dict
         and set(trace) == {"path", "candidate_lines", "decision"}
         and trace.get("path") == DOC
         and type(trace.get("candidate_lines")) is list
         and len(trace["candidate_lines"]) <= 5
         and all(type(line) is int and 1 <= line <= 4096
                 for line in trace["candidate_lines"])
         and len(set(trace["candidate_lines"])) == len(trace["candidate_lines"])
         and trace.get("decision") in {
             "no_candidates", "insufficient", "supported",
         }
         and (len(trace["candidate_lines"]) == 0) ==
             (trace["decision"] == "no_candidates")
         and (trace["decision"] == "supported") ==
             (observed["state"] == "supported")
         and type(expected_source) is dict
         and expected_source.get("path") == DOC
         and expected_source.get("ref_sha") == MAIN,
         "issue8_candidate_trace_unverified")
    text, blob, source_digest, source_bytes = _file_data(
        independent.get_text_file(REPO, DOC, MAIN), DOC,
    )
    need(expected_source.get("blob_sha") == blob
         and expected_source.get("sha256") == source_digest
         and expected_source.get("bytes") == source_bytes,
         "issue8_candidate_source_drift")
    source_lines = text.splitlines()
    need(all(line <= len(source_lines) for line in trace["candidate_lines"]),
         "issue8_candidate_line_out_of_bounds")
    acceptance_candidate_seen = any(
        "Builder" in source_lines[line - 1]
        and "42/42" in source_lines[line - 1]
        for line in trace["candidate_lines"]
    )
    # Only enumerated paths, integer line numbers and booleans are emitted.
    print(json.dumps({
        "result": "ISSUE8_RAG_CANDIDATE_LINES_ATTESTED",
        "worker_sha": MAIN,
        "run_id": run_id,
        "source_path": DOC,
        "source_sha256": source_digest,
        "candidate_lines": trace["candidate_lines"],
        "selection_decision": trace["decision"],
        "source_contains_acceptable_line": acceptance_candidate_seen,
        "observed_state": observed["state"],
        "model_calls": observed["model_calls"],
        "model_process_verified": True,
        "independent_source_readback": True,
        "raw_source_or_model_text_logged": False,
        "code_modified": False, "cas_write": False,
        "draft_pr_created": False,
    }, sort_keys=True))
    need(observed.get("state") == "supported"
         and observed.get("model_calls") == 2,
         "issue8_real_qwen_evidence_unproved")

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
