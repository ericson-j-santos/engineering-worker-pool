"""Hosted-only real Qwen7B issue #8 -> GitHub evidence citation gate.

Unlike synthetic issue tests, this calls the actual approved issue/comment/main
and the real local Ollama process in CI. All GitHub operations are GET-only.
It cannot claim the issue, write CAS, patch code or publish any branch/PR.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone

from app.agent_rag_consumer import NativeQwenResearchPlanner
from app.issue_draft_pr import HandoffBlocked, admit, REPO, OWNER, PROFILE, SHA, require
from app.issue_rag_handoff import QUESTION, bind_issue_rag, digest, canonical
from app.issue_rag_research import (
    IssueRAGResearch, official_issue_reader, research_issue,
)

ISSUE = 8
DOC = "docs/ISSUE_QWEN7B_DRAFT_HANDOFF.md"
MAX_COMMENT_PAGES = 10
MAX_COMMENT_BYTES = 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the read-only GitHub token to a redirected host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def list_issue_comments(token: str | None) -> list[dict]:
    """Enumerate the fixed issue's complete owner-approval history via GET only."""
    require(token is None or (type(token) is str and 0 < len(token) <= 2048),
            "issue8_token_configuration_invalid")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "worker-pool-issue8-ci/1",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    opener = urllib.request.build_opener(_NoRedirect)
    records: list[dict] = []
    for number in range(1, MAX_COMMENT_PAGES + 1):
        url = (f"https://api.github.com/repos/{REPO}/issues/{ISSUE}/comments"
               f"?per_page=100&page={number}")
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with opener.open(request, timeout=12) as response:
                require(response.status == 200, "issue8_comment_lookup_unavailable")
                payload = response.read(MAX_COMMENT_BYTES + 1)
            require(len(payload) <= MAX_COMMENT_BYTES, "issue8_comment_page_oversized")
            page = json.loads(payload)
        except HandoffBlocked:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                OSError, ValueError, UnicodeError):
            raise HandoffBlocked("issue8_comment_lookup_unavailable") from None
        require(type(page) is list and len(page) <= 100,
                "issue8_comment_list_invalid")
        records.extend(page)
        if len(page) < 100:
            return records
    raise HandoffBlocked("issue8_comment_history_truncated")


def select_owner_authorization(comments: list[dict], base_sha: str) -> int:
    """Select only a literal OWNER authorization bound to the current main SHA."""
    require(type(base_sha) is str and SHA.fullmatch(base_sha) is not None,
            "issue8_current_main_sha_invalid")
    require(type(comments) is list, "issue8_comment_list_invalid")
    expected = f"/local-agent authorize {PROFILE} sha:{base_sha}"
    issue_url = f"https://api.github.com/repos/{REPO}/issues/{ISSUE}"
    seen: set[int] = set()
    approved: list[int] = []
    for item in comments:
        require(type(item) is dict, "issue8_comment_list_invalid")
        comment_id = item.get("id")
        require(type(comment_id) is int and comment_id > 0
                and comment_id not in seen, "issue8_comment_list_invalid")
        seen.add(comment_id)
        user = item.get("user")
        if (type(user) is dict and user.get("login") == OWNER
                and user.get("type") == "User"
                and item.get("author_association") == "OWNER"
                and item.get("issue_url") == issue_url
                and item.get("body") == expected):
            approved.append(comment_id)
    require(bool(approved), "issue8_current_owner_authorization_missing")
    return max(approved)


def need(ok: bool, code: str) -> None:
    if not ok:
        raise RuntimeError(code)


def main() -> None:
    # The hosted continuity workflow validates imports without Agent Core installed.
    from agent_core.pr_context import _file_data
    need(os.environ.get("GITHUB_ACTIONS") == "true",
         "issue8_rag_ci_environment_required")
    need(os.environ.get("GITHUB_REPOSITORY") == REPO,
         "issue8_rag_ci_repository_invalid")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    need(run_id.isdigit(), "issue8_rag_run_identity_missing")
    token = os.environ.get("GITHUB_TOKEN") or None

    reader = official_issue_reader(token)
    issue = reader.get_issue(ISSUE)
    branch = reader.get_branch("main")
    current = branch.get("commit") if type(branch) is dict else None
    main_sha = current.get("sha") if type(current) is dict else None
    require(type(main_sha) is str and SHA.fullmatch(main_sha) is not None,
            "issue8_current_main_sha_invalid")
    comment_id = select_owner_authorization(list_issue_comments(token), main_sha)
    comment = reader.get_comment(comment_id)
    # Fresh branch readback after pagination, before spending any model calls.
    branch = reader.get_branch("main")
    now = datetime.now(timezone.utc)
    approved = admit(
        issue, comment, branch, base_sha=main_sha,
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
         and observed.get("base_sha") == main_sha
         and observed.get("head_sha") == main_sha
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
         and expected_source.get("ref_sha") == main_sha,
         "issue8_candidate_trace_unverified")
    text, blob, source_digest, source_bytes = _file_data(
        independent.get_text_file(REPO, DOC, main_sha), DOC,
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
        "worker_sha": main_sha,
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
        need((path, sha) == (DOC, main_sha),
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
         and cited.base_sha == main_sha,
         "issue8_builder_claim_not_supported")
    # The regression obligation is verified separately by the Validator.
    need("regressões" not in QUESTION.lower()
         and "casos" in QUESTION.lower(),
         "issue8_question_not_single_claim")
    current = independent.get_branch("main")
    need(current.get("commit", {}).get("sha") == main_sha,
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
        "worker_sha": main_sha,
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
