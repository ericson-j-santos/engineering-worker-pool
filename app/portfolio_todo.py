"""Admissão fail-closed de TODOs canônicos em tarefas do Worker Pool.

Adaptador puro: NÃO consulta GitHub/Notion, não autentica observações externas e
NÃO executa HTTP. O chamador confiável obtém TODO e snapshot GitHub pela API
oficial, valida permissão e fornece a observação atual para esta função.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_PRIORITY = {"P0": 0, "P1": 10, "P2": 20, "P3": 30}
_TYPES = frozenset({"Implementação", "Correção", "Validação", "Automação"})
_MAX_AGE = timedelta(minutes=5)


class AdmissionRejected(ValueError):
    """O TODO não pode ser despachado sem evidência adicional."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise AdmissionRejected(reason)


def _string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def _observed_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdmissionRejected("github_observation_invalid") from exc
    _require(parsed.tzinfo is not None, "github_observation_timezone_missing")
    return parsed.astimezone(timezone.utc)


def prepare_portfolio_task(
    todo: Mapping[str, Any],
    github_issue: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Retorna payload para `POST /v1/tasks` sem efeitos externos.

    Exige confirmação pelo cliente confiável de Issue aberta (não PR), ref da
    branch padrão e SHA do HEAD observado; somente o Worker Pool persiste
    a tarefa e garante replay idempotente no momento do POST.
    """
    _require(now.tzinfo is not None, "now_timezone_missing")
    now_utc = now.astimezone(timezone.utc)
    _require(_string(todo, "Fonte") == "GitHub", "source_not_github")
    _require(_string(todo, "Status") == "PENDENTE", "todo_not_pending")
    _require(_string(todo, "Tipo") in _TYPES, "todo_type_not_executable")
    priority_label = _string(todo, "Prioridade")
    _require(priority_label in _PRIORITY, "todo_priority_invalid")

    repository = _string(github_issue, "repository")
    _require(bool(_REPO.fullmatch(repository)), "github_repository_invalid")
    issue_number = github_issue.get("issue_number")
    _require(type(issue_number) is int and issue_number > 0, "github_issue_number_invalid")
    _require(_string(github_issue, "state") == "open", "github_issue_not_open")
    _require(github_issue.get("is_pull_request") is False, "github_pull_request_not_issue")

    issue_url = f"https://github.com/{repository}/issues/{issue_number}"
    repository_url = f"https://github.com/{repository}"
    _require(_string(github_issue, "issue_url") == issue_url, "github_issue_url_mismatch")
    _require(
        _string(todo, "Origem URL") in {repository_url, issue_url},
        "todo_origin_repository_mismatch",
    )
    _require(
        _string(todo, "Identificador externo") == f"github:{repository}#{issue_number}",
        "todo_external_issue_id_mismatch",
    )

    branch = _string(github_issue, "default_branch")
    verified_ref = _string(github_issue, "verified_ref")
    _require(bool(branch) and verified_ref == branch, "github_default_branch_not_verified")
    main_sha = _string(github_issue, "verified_head_sha")
    base_sha = _string(github_issue, "base_sha")
    _require(
        bool(_SHA.fullmatch(base_sha)) and bool(_SHA.fullmatch(main_sha)),
        "github_sha_invalid",
    )
    _require(base_sha.lower() == main_sha.lower(), "github_head_mismatch")

    observed = _observed_at(_string(github_issue, "observed_at"))
    elapsed = now_utc - observed
    _require(timedelta(0) <= elapsed <= _MAX_AGE, "github_observation_stale_or_future")

    correlation_id = _string(todo, "Correlation ID")
    logical_key = _string(todo, "Chave de idempotência")
    _require(0 < len(correlation_id) <= 128, "todo_correlation_invalid")
    _require(0 < len(logical_key) <= 512, "todo_idempotency_key_invalid")
    request_id = "todo-" + hashlib.sha256(logical_key.encode("utf-8")).hexdigest()
    return {
        "repository": repository,
        "issue_number": issue_number,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "priority": _PRIORITY[priority_label],
        "base_sha": base_sha.lower(),
    }
