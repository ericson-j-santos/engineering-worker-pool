"""Sonda DEV de leitura exclusiva da página Notion TODO-74.

Executar somente em sessão validada pelo Session Launcher e Command Gateway.
Não emite autorização, não cria tarefa e não acessa outra página.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.pre_pr_cas_gate import PrePRBlocked
from app.reqsys_readonly_authority import (
    NotionReqSysTodoReader,
    REPOSITORY,
    TODO_ID,
)

_SAFE_REASONS = frozenset({
    "reqsys_notion_pointer_missing",
    "reqsys_notion_private_path_invalid",
    "reqsys_notion_credential_unavailable",
    "reqsys_notion_network_unavailable",
    "reqsys_notion_response_unverified",
    "reqsys_notion_response_invalid",
    "reqsys_notion_page_unverified",
    "reqsys_notion_parent_unverified",
    "reqsys_notion_todo_unverified",
    "reqsys_notion_readback_invalid",
})


def run() -> dict[str, object]:
    pointer = os.environ.get("PORTFOLIO_NOTION_TOKEN_FILE", "")
    if not pointer:
        raise PrePRBlocked("reqsys_notion_pointer_missing")
    token_file = Path(pointer)
    if not token_file.is_absolute():
        raise PrePRBlocked("reqsys_notion_private_path_invalid")
    with NotionReqSysTodoReader.from_private_token_file(
        token_file=token_file,
    ) as reader:
        result = reader.todo(TODO_ID)
    if result != {
        "id": TODO_ID,
        "state": "BLOCKED",
        "repository": REPOSITORY,
        "issue_number": 715,
    }:
        raise PrePRBlocked("reqsys_notion_readback_invalid")
    return {
        "result": "TODO74_GET_VERIFIED",
        "todo_id": TODO_ID,
        "state": "BLOCKED",
        "authorized": False,
        "dispatched": False,
    }


def main() -> int:
    try:
        outcome = run()
    except PrePRBlocked as exc:
        reason = str(exc)
        if reason not in _SAFE_REASONS:
            reason = "reqsys_notion_probe_failed"
        print(json.dumps({"result": "BLOCKED", "reason": reason}, sort_keys=True))
        return 2
    except Exception:
        # Never echo provider exceptions, headers, paths or secret material.
        print(json.dumps({"result": "BLOCKED", "reason": "reqsys_notion_probe_failed"}, sort_keys=True))
        return 2
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
