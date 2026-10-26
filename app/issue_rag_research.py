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

# The versioned issue question is English, while its approved evidence document
# is in PT-BR. A literal English-only token intersection hid the real 42/42 and
# 52/52 acceptance paragraphs. These aliases affect candidate RANKING only;
# neither they nor a keyword hit can turn Qwen's refusal into "supported".
_ISSUE_SEARCH_ALIASES = {
    "test": ("teste", "testes", "casos"),
    "tests": ("teste", "testes", "casos"),
    "cases": ("casos",),
    "regression": ("regressão", "regressões"),
    "regressions": ("regressão", "regressões"),
    "evidence": ("evidência", "evidências", "provas"),
    "required": ("exige", "exigidos", "obrigatório"),
    "verification": ("verificação", "verificado"),
    "validation": ("validação", "validador"),
}


def _ranking_words(query: str) -> tuple[set[str], set[str]]:
    """Constrained bilingual vocabulary for the fixed issue acceptance question."""
    from agent_core.pr_context import _words

    direct = _words(query)
    # A model-selected query cannot erase the operator-defined topic, but the
    # actual query still supplies the direct-match terms.
    fixed_topic = _words(QUESTION)
    local_terms = set()
    for term in sorted(direct | fixed_topic):
        local_terms.update(_ISSUE_SEARCH_ALIASES.get(term, ()))
    return direct, local_terms


class IssueReader(Protocol):
    def get_issue(self, number: int) -> dict[str, Any]: ...
    def get_comment(self, comment_id: int) -> dict[str, Any]: ...
    def get_branch(self, name: str) -> dict[str, Any]: ...
    def get_text_file(self, repository: str, path: str, sha: str) -> dict[str, Any]: ...


class IssueRAGResearch:
    """Separate read-only capability; not a worker or GitHub writer."""

    def __init__(self, *, reader: IssueReader, independent_reader: IssueReader,
                 planner: Any, approved_paths: tuple[str, ...],
                 diagnostic_sink: list[dict[str, Any]] | None = None) -> None:
        require(type(approved_paths) is tuple and 1 <= len(approved_paths) <= MAX_SOURCES
                and len(set(approved_paths)) == len(approved_paths)
                and all(type(p) is str and p in ALLOWED_SOURCES
                        for p in approved_paths),
                "rag_source_allowlist_invalid")
        require(diagnostic_sink is None or
                (type(diagnostic_sink) is list and not diagnostic_sink),
                "rag_diagnostic_sink_invalid")
        self._diagnostic_sink = diagnostic_sink
        self.reader = reader
        self.independent_reader = independent_reader
        self.planner = planner
        self.approved_paths = approved_paths

    def _record_candidates(self, path: str, lines: list[int],
                           decision: str) -> None:
        """Record only bounded source metadata, never model/source text."""
        if self._diagnostic_sink is None:
            return
        require(type(path) is str and path in self.approved_paths
                and type(lines) is list and len(lines) <= MAX_LINES
                and all(type(line) is int and 1 <= line <= 4096
                        for line in lines)
                and len(set(lines)) == len(lines)
                and decision in {"no_candidates", "insufficient", "supported"}
                and ((not lines) == (decision == "no_candidates"))
                and len(self._diagnostic_sink) < MAX_SOURCES,
                "rag_diagnostic_metadata_invalid")
        self._diagnostic_sink.append({
            "path": path,
            "candidate_lines": list(lines),
            "decision": decision,
        })

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
            direct, localized = _ranking_words(query)
            for line, value in enumerate(text.splitlines(), start=1):
                excerpt = value.strip()[:MAX_LINE]
                source_words = _words(value)
                # Prefer clauses about tests/regressions over a bare profile
                # identifier; keep the existing 24-point citation contract.
                score = min(24, len(direct & source_words) +
                            4 * len(localized & source_words))
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
                self._record_candidates(chosen, [], "no_candidates")
                continue
            selected = self.planner.select(QUESTION, [
                {"citation_index": i, "path": s["path"], "line": s["line"],
                 "excerpt": s["excerpt"]}
                for i, s in enumerate(snippets)
            ])
            candidate = AgentCoreResearchBridge._check_selection(selected, snippets)
            self._record_candidates(
                chosen, [s["line"] for s in snippets],
                "insufficient" if candidate is None else "supported",
            )
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


def research_issue(binding: IssueBinding, *, github_token: str | None = None,
                   diagnostic_sink: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Explicit DEV opt-in. Caller first obtains IssueBinding from official API."""
    from app.agent_rag_consumer import NativeQwenResearchPlanner
    from app.native_structured_ollama import NativeStructuredOllama

    NativeStructuredOllama.no_cloud()
    return IssueRAGResearch(
        reader=official_issue_reader(github_token),
        independent_reader=official_issue_reader(github_token),
        planner=NativeQwenResearchPlanner(),
        approved_paths=("docs/ISSUE_QWEN7B_DRAFT_HANDOFF.md",),
        diagnostic_sink=diagnostic_sink,
    ).research(binding)
