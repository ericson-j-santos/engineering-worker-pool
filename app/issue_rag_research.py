"""Issue-scoped, read-only Agentic RAG producer for an actually admitted task.

Unlike PR-context research, this flow reads the exact issue-authorized main
commit; it never fabricates a PR to reuse a different task's citation.
No model command, issue text, branch name, or path can authorize execution.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Protocol

from app.issue_draft_pr import (
    HandoffBlocked, IssueBinding, MODEL_DIGEST_PREFIX,
    REPO, admit, require,
)
from app.issue_rag_handoff import (
    ALLOWED_SOURCES, MAX_LINE, MAX_MODEL_CALLS, MAX_SOURCES,
    QUESTION, bind_issue_rag, canonical, digest,
)

MAX_SOURCE_BYTES = 65_536
MAX_LINES = 5


class IssueReader(Protocol):
    def get_issue(self, number: int) -> dict[str, Any]: ...
    def get_comment(self, comment_id: int) -> dict[str, Any]: ...
    def get_branch(self, name: str) -> dict[str, Any]: ...
    def get_text_file(self, repository: str, path: str, sha: str) -> dict[str, Any]: ...


class IssueRAGResearch:
    """Separate read-only capability; not a worker or GitHub writer."""

    def __init__(self, *, reader: IssueReader, independent_reader: IssueReader,
                 planner: Any, approved_paths: tuple[str, ...]) -> None:
        require(type(approved_paths) is tuple and 1 <= len(approved_paths) <= MAX_SOURCES
                and len(set(approved_paths)) == len(approved_paths)
                and all(type(p) is str and p in ALLOWED_SOURCES
                        for p in approved_paths),
                "rag_source_allowlist_invalid")
        self.reader = reader
        self.independent_reader = independent_reader
        self.planner = planner
        self.approved_paths = approved_paths

    @staticmethod
    def _read_authorized(reader: IssueReader, binding: IssueBinding) -> None:
        observed_at = datetime.now(timezone.utc)
        checked = admit(
            reader.get_issue(binding.number),
            reader.get_comment(binding.comment_id),
            reader.get_branch("main"),
            base_sha=binding.base_sha,
            observed_at=observed_at.isoformat(),
            now=observed_at,
        )
        require(checked == binding, "rag_owner_or_issue_drift")

    @staticmethod
    def _source(reader: IssueReader, path: str, base_sha: str) -> tuple[str, str, str, int]:
        # Reuse canonical Agent Core Git blob integrity and bounded text rules.
        from agent_core.pr_context import _file_data
        return _file_data(reader.get_text_file(REPO, path, base_sha), path)

    def research(self, binding: IssueBinding) -> dict[str, Any]:
        # The reader must independently authenticate the owner/issue/main SHA
        # before the model is permitted to choose a source path.
        self._read_authorized(self.reader, binding)
        starting = getattr(self.planner, "calls", None)
        require(type(starting) is int and starting >= 0, "rag_model_counter_invalid")
        from agent_core.pr_context import _words
        from app.agent_rag_consumer import AgentCoreResearchBridge

        remaining = list(self.approved_paths)
        receipts: list[dict[str, Any]] = []
        searches: list[str] = []
        citation: dict[str, Any] | None = None
        for _ in range(min(MAX_SOURCES, len(remaining))):
            plan = self.planner.plan(QUESTION, tuple(remaining))
            chosen, query = AgentCoreResearchBridge._validate_plan(
                plan, QUESTION, tuple(remaining),
            )
            remaining.remove(chosen)
            searches.append(digest(query.encode("utf-8")))
            text, blob_sha, sha256, nbytes = self._source(
                self.reader, chosen, binding.base_sha,
            )
            require(nbytes <= MAX_SOURCE_BYTES, "rag_source_oversized")
            source = {
                "path": chosen, "ref_sha": binding.base_sha,
                "blob_sha": blob_sha, "sha256": sha256, "bytes": nbytes,
            }
            receipts.append(source)
            snippets: list[dict[str, Any]] = []
            words = _words(query)
            for line, value in enumerate(text.splitlines(), start=1):
                excerpt = value.strip()[:MAX_LINE]
                score = len(words & _words(value))
                if excerpt and score:
                    snippets.append({
                        "path": chosen, "line": line,
                        "score": score, "excerpt": excerpt,
                        "url": (
                            f"https://github.com/{REPO}/blob/"
                            f"{binding.base_sha}/{chosen}#L{line}"
                        ),
                        "untrusted_source_text": True,
                    })
            snippets.sort(key=lambda x: (-x["score"], x["line"]))
            snippets = snippets[:MAX_LINES]
            if not snippets:
                continue
            selected = self.planner.select(QUESTION, [
                {"citation_index": i, "path": s["path"], "line": s["line"],
                 "excerpt": s["excerpt"]}
                for i, s in enumerate(snippets)
            ])
            candidate = AgentCoreResearchBridge._check_selection(selected, snippets)
            if candidate is None:
                continue
            citation = {**candidate, "source_sha256": sha256}
            break

        self._read_authorized(self.reader, binding)
        completed = getattr(self.planner, "calls", None)
        model_digest = getattr(self.planner, "model_digest", None)
        require(type(completed) is int and
                1 <= completed - starting <= MAX_MODEL_CALLS and
                type(model_digest) is str and len(model_digest) == 64
                and model_digest.startswith(MODEL_DIGEST_PREFIX)
                and getattr(self.planner, "process_verified", False) is True,
                "rag_model_attestation_invalid")
        observed = {
            "schema_version": "worker.rag.issue-context-v1",
            "repository": REPO,
            "issue_number": binding.number,
            "request_id": binding.request_id,
            "correlation_id": binding.correlation_id,
            "head_sha": binding.base_sha,
            "base_sha": binding.base_sha,
            "question_sha256": digest(QUESTION.encode("utf-8")),
            "search_query_sha256": searches,
            "sources": receipts,
            "selected_reference": citation,
            "state": "supported" if citation else "insufficient_evidence",
            "model_calls": completed - starting,
            "model_digest": model_digest,
            "model_process_verified": True,
            "issue_head_readback_stable": True,
            "execution_authorized": False,
            "state_changed": False,
        }
        observed["receipt_sha256"] = digest(canonical(observed))
        if citation is not None:
            # A separate reader retrieves original bytes AFTER inference.
            def fetch_source(path: str, sha: str) -> bytes:
                value, _, _, _ = self._source(self.independent_reader, path, sha)
                return value.encode("utf-8")
            bind_issue_rag(binding, observed, fetch_source)
            # Last independent GitHub admission after the source readback.
            self._read_authorized(self.independent_reader, binding)
        return observed


def official_issue_reader(token: str | None = None) -> IssueReader:
    """Use the reviewed Agent Core official GET-only HTTPS reader."""
    from agent_core.pr_context import ContextGitHubHTTPS

    class Reader(ContextGitHubHTTPS):
        def get_issue(self, number: int) -> dict[str, Any]:
            require(type(number) is int and number > 0, "rag_issue_invalid")
            return self._get(f"/repos/{REPO}/issues/{number}")

        def get_comment(self, comment_id: int) -> dict[str, Any]:
            require(type(comment_id) is int and comment_id > 0,
                    "rag_comment_invalid")
            return self._get(f"/repos/{REPO}/issues/comments/{comment_id}")

        def get_branch(self, name: str) -> dict[str, Any]:
            require(name == "main", "rag_branch_not_allowlisted")
            return self._get(f"/repos/{REPO}/branches/main")

    return Reader(token)


def research_issue(binding: IssueBinding, *, github_token: str | None = None) -> dict[str, Any]:
    """Explicit DEV opt-in. Caller first obtains IssueBinding from official API."""
    from app.agent_rag_consumer import NativeQwenResearchPlanner
    from app.native_structured_ollama import NativeStructuredOllama

    NativeStructuredOllama.no_cloud()
    return IssueRAGResearch(
        reader=official_issue_reader(github_token),
        independent_reader=official_issue_reader(github_token),
        planner=NativeQwenResearchPlanner(),
        approved_paths=("docs/ISSUE_QWEN7B_DRAFT_HANDOFF.md", "README.md"),
    ).research(binding)
