"""Read-only Issue #8 Qwen RAG with the live owner-approved main SHA.

This fixture does not mutate Issue #8, CAS, Git refs, tasks or PRs.
It preserves the historical CAS-bound RAG probe and its original tests.
"""
from __future__ import annotations

import os

from scripts.issue_rag_real_qwen_e2e import run_authorized_issue

CURRENT_MAIN = "e22aabb14e1a646e274c331e83cc06c470607a38"
CURRENT_OWNER_COMMENT = 6458684416
REPO = "ericson-j-santos/engineering-worker-pool"
BRANCH = "feat/task-routing-pinned-or-pool-20261105"


def main() -> None:
    if not (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_REPOSITORY") == REPO
        and os.environ.get("GITHUB_REF") == "refs/heads/" + BRANCH
        and os.environ.get("GITHUB_EVENT_NAME") == "push"
    ):
        raise RuntimeError("current_issue_rag_ci_identity_rejected")
    # The reusable strict gate fetches the CURRENT issue, owner comment, and
    # branch from GitHub and applies the existing exact SHA/OWNER/MAIN checks.
    # If main moves or the comment becomes invalid it stops before inference.
    run_authorized_issue(CURRENT_MAIN, CURRENT_OWNER_COMMENT)


if __name__ == "__main__":
    main()
