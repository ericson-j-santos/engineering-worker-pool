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
    "reqsys_notion_page_type_unverified",
    "reqsys_notion_page_id_unverified",
    "reqsys_notion_archived_unverified",
    "reqsys_notion_trash_unverified",
    "reqsys_notion_parent_unverified",
    "reqsys_notion_todo_unverified",
    "reqsys_notion_readback_invalid",
})


def _registered_pointer() -> str:
    """Read ONLY the path reference from the current user's Windows profile.

    Environment updates do not reach an already-running remote agent.
    The token itself remains protected and is read only by the existing
    NotionReqSysTodoReader secure-file implementation.
    """
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ
        ) as key:
            value, kind = winreg.QueryValueEx(key, "PORTFOLIO_NOTION_TOKEN_FILE")
        if not isinstance(value, str) or kind not in (
            winreg.REG_SZ, winreg.REG_EXPAND_SZ
        ):
            return ""
        return os.path.expandvars(value) if kind == winreg.REG_EXPAND_SZ else value
    except (OSError, ImportError):
        return ""


def run() -> dict[str, object]:
    pointer = os.environ.get("PORTFOLIO_NOTION_TOKEN_FILE", "") or _registered_pointer()
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
