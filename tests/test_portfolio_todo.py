"""Contrato de admissão TODO Global -> Worker Pool, sem dependência externa."""
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.portfolio_todo import AdmissionRejected, prepare_portfolio_task


NOW = datetime(2026, 10, 8, 19, 0, tzinfo=timezone.utc)
REPO = "ericson-j-santos/mcmv-rural-painel"
ISSUE = 18
SHA = "a" * 40


def todo() -> dict:
    return {
        "Fonte": "GitHub",
        "Status": "PENDENTE",
        "Tipo": "Validação",
        "Prioridade": "P2",
        "Origem URL": f"https://github.com/{REPO}",
        "Identificador externo": f"github:{REPO}#{ISSUE}",
        "Chave de idempotência": "portfolio-github-mcmv-rural-painel-e2e",
        "Correlation ID": "github-portfolio-20261008-v1",
    }


def github() -> dict:
    return {
        "repository": REPO,
        "issue_number": ISSUE,
        "state": "open",
        "is_pull_request": False,
        "issue_url": f"https://github.com/{REPO}/issues/{ISSUE}",
        "default_branch": "main",
        "verified_ref": "main",
        "verified_head_sha": SHA,
        "base_sha": SHA,
        "observed_at": (NOW - timedelta(seconds=30)).isoformat(),
    }


def test_positive_payload_and_stable_replay_identity() -> None:
    a = prepare_portfolio_task(todo(), github(), now=NOW)
    b = prepare_portfolio_task(todo(), github(), now=NOW + timedelta(seconds=1))
    assert a == b
    assert a["repository"] == REPO
    assert a["issue_number"] == ISSUE
    assert a["base_sha"] == SHA
    assert a["priority"] == 20
    assert a["request_id"].startswith("todo-")
    assert a["correlation_id"] == "github-portfolio-20261008-v1"
    assert "lease_token" not in a
    assert "token" not in a


@pytest.mark.parametrize(
    ("source", "field", "value", "reason"),
    [
        ("todo", "Status", "EM ANDAMENTO", "todo_not_pending"),
        ("todo", "Status", "CONCLUÍDO", "todo_not_pending"),
        ("todo", "Tipo", "Acompanhamento", "todo_type_not_executable"),
        ("todo", "Fonte", "ChatGPT", "source_not_github"),
        ("todo", "Prioridade", "Urgente", "todo_priority_invalid"),
        ("todo", "Identificador externo", "github:outro/repo#18", "todo_external_issue_id_mismatch"),
        ("todo", "Origem URL", "https://github.com/outro/repo", "todo_origin_repository_mismatch"),
        ("todo", "Chave de idempotência", "", "todo_idempotency_key_invalid"),
        ("todo", "Correlation ID", "", "todo_correlation_invalid"),
        ("github", "state", "closed", "github_issue_not_open"),
        ("github", "is_pull_request", True, "github_pull_request_not_issue"),
        ("github", "issue_number", True, "github_issue_number_invalid"),
        ("github", "issue_url", "https://github.com/outro/repo/issues/18", "github_issue_url_mismatch"),
        ("github", "verified_ref", "develop", "github_default_branch_not_verified"),
        ("github", "verified_head_sha", "b" * 40, "github_head_mismatch"),
        ("github", "base_sha", "invalid", "github_sha_invalid"),
        ("github", "observed_at", "2026-10-08T18:50:00+00:00", "github_observation_stale_or_future"),
        ("github", "observed_at", "2026-10-08T19:01:00+00:00", "github_observation_stale_or_future"),
        ("github", "observed_at", "2026-10-08T18:59:30", "github_observation_timezone_missing"),
    ],
)
def test_negatives_fail_closed(source: str, field: str, value, reason: str) -> None:
    item, evidence = todo(), github()
    (item if source == "todo" else evidence)[field] = value
    with pytest.raises(AdmissionRejected, match=reason):
        prepare_portfolio_task(item, evidence, now=NOW)


def test_todo_without_github_issue_binding_is_not_dispatched() -> None:
    item = todo()
    item["Identificador externo"] = "github:ericson-j-santos/mcmv-rural-painel:e2e"
    with pytest.raises(AdmissionRejected, match="todo_external_issue_id_mismatch"):
        prepare_portfolio_task(item, github(), now=NOW)


def test_missing_timezone_fails_closed() -> None:
    with pytest.raises(AdmissionRejected, match="now_timezone_missing"):
        prepare_portfolio_task(todo(), github(), now=NOW.replace(tzinfo=None))


def test_synthetic_bridge_http_e2e_with_independent_readback(tmp_path: Path, monkeypatch) -> None:
    """E2E de contrato em DEV isolado; NÃO comprova integração viva Notion/GitHub."""
    token_file = tmp_path / "pool-token"
    token_file.write_text("test-local-token\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_WORKER_POOL_DB", str(tmp_path / "pool.sqlite"))
    monkeypatch.setenv("CODEX_WORKER_POOL_API_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CODEX_WORKER_POOL_EXPECTED_RULES_SHA", "c" * 40)
    monkeypatch.setenv("CODEX_WORKER_POOL_RECONCILE_INTERVAL_SECONDS", "5")
    service_root = str(Path(__file__).resolve().parents[1])
    if service_root not in sys.path:
        sys.path.insert(0, service_root)
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    client = TestClient(module.app)
    headers = {"Authorization": "Bearer test-local-token"}

    first_payload = prepare_portfolio_task(todo(), github(), now=NOW)
    first = client.post("/v1/tasks", json=first_payload, headers=headers)
    replay = client.post("/v1/tasks", json=first_payload, headers=headers)
    assert first.status_code == 201
    assert replay.status_code == 200
    assert first.json()["created"] is True
    assert replay.json()["created"] is False
    task_id = first.json()["task"]["task_id"]
    assert replay.json()["task"]["task_id"] == task_id

    readback = client.get(f"/v1/tasks/{task_id}", headers=headers)
    assert readback.status_code == 200
    assert readback.json()["task_id"] == task_id
    assert readback.json()["repository"] == REPO
    assert readback.json()["base_sha"] == SHA
    assert "lease_token" not in readback.json()
    assert client.post("/v1/tasks", json=first_payload).status_code == 401

    bad = todo()
    bad["Status"] = "BLOQUEADO"
    with pytest.raises(AdmissionRejected):
        prepare_portfolio_task(bad, github(), now=NOW)
    observed = client.get("/v1/snapshot", headers=headers)
    assert observed.status_code == 200
    assert len(observed.json()["tasks"]) == 1
