"""Admissao estrita de validacao via comentario; nao despacha tarefas de produto."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPOSITORY = "ericson-j-santos/engineering-worker-pool"
OWNER = "ericson-j-santos"
ISSUE_NUMBER = 13
COMMAND = "/worker validate"
SHA40 = re.compile(r"^[0-9a-f]{40}$")
MAX_EVENT_BYTES = 262144


def admitted(event: Any, expected_sha: str, actor: str, repository: str) -> bool:
    """Somente evento autenticado fornecido pelo GitHub; sem HTTP, fila ou shell."""
    if (not isinstance(event, dict) or not isinstance(expected_sha, str)
            or not SHA40.fullmatch(expected_sha)):
        return False
    comment, issue, repo = (event.get(k) for k in ("comment", "issue", "repository"))
    if not all(isinstance(value, dict) for value in (comment, issue, repo)):
        return False
    user = comment.get("user")
    if not isinstance(user, dict):
        return False
    return (
        actor == OWNER
        and repository == REPOSITORY
        and event.get("action") == "created"
        and repo.get("full_name") == REPOSITORY
        and repo.get("private") is False
        and type(issue.get("number")) is int
        and issue["number"] == ISSUE_NUMBER
        and issue.get("state") == "open"
        and "pull_request" not in issue
        and user.get("login") == OWNER
        and user.get("type") == "User"
        and type(comment.get("id")) is int
        and comment["id"] > 0
        and comment.get("body") == COMMAND
    )


def main() -> int:
    try:
        path = Path(os.environ["GITHUB_EVENT_PATH"])
        if path.stat().st_size > MAX_EVENT_BYTES:
            raise ValueError
        event = json.loads(path.read_text(encoding="utf-8"))
        ok = (
            os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("GITHUB_EVENT_NAME") == "issue_comment"
            and admitted(
                event, os.environ.get("EXPECTED_SHA", ""),
                os.environ.get("GITHUB_ACTOR", ""),
                os.environ.get("GITHUB_REPOSITORY", ""),
            )
        )
    except (OSError, ValueError, KeyError):
        ok = False
    # Nao imprimir evento, corpo, headers, tokens ou dados da conta.
    print("NATIVE_CI_ADMITTED" if ok else "NATIVE_CI_BLOCKED")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
