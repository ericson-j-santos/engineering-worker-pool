"""Scoped issue8 CI gate: relevant path, unrelated PR and fail-closed negatives."""
from __future__ import annotations

import pytest

from scripts.issue8_ci_scope import ScopeBlocked, issue8_applicable


@pytest.mark.parametrize("paths", [
    ["scripts/issue_rag_real_qwen_e2e.py"],
    ["app/issue_draft_pr.py"],
    ["app/agent_rag_consumer.py"],
    ["requirements-rag.txt"],
    ["tests/test_issue8_ci_authorization.py"],
    [".github/workflows/ci.yml"],
    ["docs/ISSUE_QWEN7B_DRAFT_HANDOFF.md"],
])
def test_related_changes_require_live_issue8_e2e(paths: list[str]) -> None:
    assert issue8_applicable(paths) is True


@pytest.mark.parametrize("paths", [
    ["app/continuation_selector.py", "docs/GOVERNED_NEXT_ACTION.md"],
    ["tests/test_continuation_selector.py"],
    ["app/portfolio_bridge.py"],
    ["README.md"],
])
def test_unrelated_changes_are_not_issue8_e2e_proof(paths: list[str]) -> None:
    assert issue8_applicable(paths) is False


@pytest.mark.parametrize("paths", [
    [], ["../escape"], ["README.md", "README.md"],
    ["README.md\nscripts/issue_rag_real_qwen_e2e.py"],
    ["bad\\path"], ["bad\x00path"], ["a/../b"],
    ["a//b"], ["a/./b"],
])
def test_invalid_or_ambiguous_diff_blocks_instead_of_skipping(paths: list[str]) -> None:
    with pytest.raises(ScopeBlocked, match="^issue8_diff_invalid$"):
        issue8_applicable(paths)
