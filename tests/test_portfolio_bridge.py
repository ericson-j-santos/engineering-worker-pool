"""E2E sintético Notion/GitHub -> adapter -> API real do Worker Pool -> SQLite."""
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from app.portfolio_bridge import BridgeRejected, run_page
from app.portfolio_todo import AdmissionRejected

NOW = datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc)
PAGE = "3f34c12b-ae7c-8155-9d28-f4d51040deda"
SOURCE = "663a8805-d9a6-48dc-92a0-308d7a9e87ac"
REPO = "ericson-j-santos/engineering-worker-pool"
ISSUE_NUMBER = 13
SHA = "a" * 40


def props(status: str = "PENDENTE") -> dict:
    def rich(value: str):
        return {"type": "rich_text", "rich_text": [{"plain_text": value}]}
    def selected(value: str):
        return {"type": "select", "select": {"name": value}}
    return {
        "Fonte": selected("GitHub"),
        "Status": selected(status),
        "Tipo": selected("Validação"),
        "Prioridade": selected("P1"),
        "Origem URL": {"type": "url", "url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}"},
        "Identificador externo": rich(f"github:{REPO}#{ISSUE_NUMBER}"),
        "Chave de idempotência": rich("portfolio-github-engineering-worker-pool-issue-13"),
        "Correlation ID": rich("portfolio-worker-pool-20261008-13"),
    }


class FakeUpstream:
    """Mock apenas das APIs EXTERNAS; Worker Pool é HTTP real em TestClient."""

    def __init__(self, client: TestClient | None = None):
        self.client = client
        self.calls = []
        self.page = {"object": "page", "parent": {"type": "data_source_id", "data_source_id": SOURCE},
                     "properties": props()}
        self.issue = {"number": ISSUE_NUMBER, "state": "open",
                      "html_url": f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}"}
        self.repo = {"full_name": REPO, "default_branch": "main"}
        self.ref = {"object": {"type": "commit", "sha": SHA}}
        self.latest_ref = self.ref
        self.todo_change_on_reread = False
        self.notion_calls = 0
        self.github_ref_calls = 0

    def request(self, service, method, url, token, data=None):
        self.calls.append((service, method, url))
        if service == "notion":
            assert token == "notion-test"
            self.notion_calls += 1
            assert method == "GET" and url.endswith("/v1/pages/" + PAGE)
            if self.todo_change_on_reread and self.notion_calls > 1:
                changed = dict(self.page)
                changed["properties"] = props(status="CONCLUÍDO")
                return 200, changed
            return 200, self.page
        if service == "github":
            assert token == "github-test"
            assert method == "GET"
            if url.endswith("/issues/13"):
                return 200, self.issue
            if url.endswith("/git/ref/heads/main"):
                self.github_ref_calls += 1
                return 200, (self.ref if self.github_ref_calls == 1 else self.latest_ref)
            assert url.endswith("/repos/" + REPO)
            return 200, self.repo
        assert service == "worker"
        assert self.client is not None
        assert token == "worker-test"
        path = urlparse(url).path
        response = self.client.request(method, path,
                                       headers={"Authorization": "Bearer worker-test"}, json=data)
        return response.status_code, response.json()


def run(upstream, *, execute=False, environment="dev", clock=NOW):
    return run_page(
        page_id=PAGE, data_source_id=SOURCE, transport=upstream,
        notion_token="notion-test", github_token="github-test",
        worker_token="worker-test" if execute else "",
        worker_url="http://127.0.0.1:9000" if execute else "",
        execute=execute, environment=environment, now=clock,
    )


def worker(tmp_path: Path, monkeypatch) -> TestClient:
    token_file = tmp_path / "worker-secret"
    token_file.write_text("worker-test\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_WORKER_POOL_DB", str(tmp_path / "isolated.db"))
    monkeypatch.setenv("CODEX_WORKER_POOL_API_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("CODEX_WORKER_POOL_EXPECTED_RULES_SHA", "c" * 40)
    sys.modules.pop("app.main", None)
    api = importlib.import_module("app.main")
    client = TestClient(api.app)
    result = client.post("/v1/repositories",
                         headers={"Authorization": "Bearer worker-test"},
                         json={"repository": REPO, "enabled": True, "max_in_flight": 1,
                               "correlation_id": "test-preflight"})
    assert result.status_code == 200
    return client


def test_dry_run_reads_real_shape_and_does_not_enqueue() -> None:
    fake = FakeUpstream()
    out = run(fake)
    assert out["state"] == "ready_for_dispatch"
    assert out["dispatched"] is False
    assert out["base_sha"] == SHA
    assert fake.github_ref_calls == 1
    assert [s for s, _, _ in fake.calls if s == "worker"] == []


def test_execute_e2e_real_worker_api_persists_once_with_independent_readback(
    tmp_path: Path, monkeypatch,
) -> None:
    client = worker(tmp_path, monkeypatch)
    fake = FakeUpstream(client)
    first = run(fake, execute=True)
    assert first["state"] == "enqueued_verified"
    assert first["created"] is True
    fake2 = FakeUpstream(client)
    replay = run(fake2, execute=True)
    assert replay["task_id"] == first["task_id"]
    assert replay["created"] is False
    observed = client.get("/v1/snapshot", headers={"Authorization": "Bearer worker-test"})
    assert observed.status_code == 200
    assert len(observed.json()["tasks"]) == 1
    persisted = client.get(f"/v1/tasks/{first['task_id']}",
                           headers={"Authorization": "Bearer worker-test"}).json()
    assert persisted["repository"] == REPO
    assert persisted["base_sha"] == SHA
    assert "lease_token" not in persisted


def test_not_pending_blocks_before_github_or_worker() -> None:
    fake = FakeUpstream()
    fake.page["properties"] = props("EM ANDAMENTO")
    with pytest.raises(BridgeRejected, match="todo_not_pending"):
        run(fake, execute=True)
    assert [s for s, _, _ in fake.calls] == ["notion"]


def test_foreign_data_source_rejected_before_github() -> None:
    fake = FakeUpstream()
    fake.page["parent"]["data_source_id"] = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(BridgeRejected, match="notion_source_mismatch"):
        run(fake)
    assert len(fake.calls) == 1


def test_closed_or_pull_request_rejected() -> None:
    for change in ({"state": "closed"}, {"pull_request": {"url": "some-url"}}):
        fake = FakeUpstream()
        fake.issue.update(change)
        with pytest.raises(BridgeRejected, match="github_issue_not_open_or_mismatch"):
            run(fake)
        assert not any(s == "worker" for s, _, _ in fake.calls)


def test_github_head_moves_between_check_and_dispatch(tmp_path, monkeypatch) -> None:
    client = worker(tmp_path, monkeypatch)
    fake = FakeUpstream(client)
    fake.latest_ref = {"object": {"type": "commit", "sha": "b" * 40}}
    with pytest.raises(BridgeRejected, match="github_head_changed_before_dispatch"):
        run(fake, execute=True)
    assert len(client.get("/v1/snapshot", headers={"Authorization":"Bearer worker-test"}).json()["tasks"]) == 0


def test_todo_change_during_preflight_rejected(tmp_path, monkeypatch) -> None:
    client = worker(tmp_path, monkeypatch)
    fake = FakeUpstream(client)
    fake.todo_change_on_reread = True
    with pytest.raises(BridgeRejected, match="todo_changed_before_dispatch"):
        run(fake, execute=True)
    assert len(client.get("/v1/snapshot", headers={"Authorization":"Bearer worker-test"}).json()["tasks"]) == 0


def test_production_dispatch_rejected_before_worker() -> None:
    fake = FakeUpstream()
    with pytest.raises(BridgeRejected, match="execute_dev_only"):
        run(fake, execute=True, environment="prod")
    assert not any(s == "worker" for s, _, _ in fake.calls)


def test_missing_or_invalid_issue_and_token_fail_closed() -> None:
    fake = FakeUpstream()
    fake.page["properties"]["Identificador externo"] = {"type":"rich_text", "rich_text":[{"plain_text":"github:not-a-valid-binding"}]}
    with pytest.raises(BridgeRejected, match="github_external_id_invalid"):
        run(fake)
    with pytest.raises(BridgeRejected, match="source_tokens_missing"):
        run_page(page_id=PAGE, data_source_id=SOURCE, transport=fake, notion_token="",
                 github_token="github-test")


def test_worker_url_must_be_loopback(tmp_path, monkeypatch) -> None:
    client = worker(tmp_path, monkeypatch)
    fake = FakeUpstream(client)
    with pytest.raises(BridgeRejected, match="worker_must_be_loopback"):
        run_page(page_id=PAGE, data_source_id=SOURCE, transport=fake,
                 notion_token="notion-test", github_token="github-test",
                 worker_token="worker-test", worker_url="http://remote.example:9000",
                 execute=True, environment="dev", now=NOW)
    assert not any(m == "POST" for _, m, _ in fake.calls)
