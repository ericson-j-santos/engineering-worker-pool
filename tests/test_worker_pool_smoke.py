from __future__ import annotations

from pathlib import Path

import pytest

from scripts.worker_pool_smoke import (
    SMOKE_REPOSITORY,
    SmokeError,
    run_smoke,
    validate_pool_url,
)

SHA = "a" * 40

class FakePool:
    def __init__(self) -> None:
        self.task_id = "cwp-smoke-task"
        self.created = False

    def request(self, method, url, token, payload):
        assert token == "test-token"
        if url.endswith("/health"):
            return 200, {"status": "healthy"}
        if url.endswith("/v1/repositories") and method == "POST":
            assert payload["repository"] == SMOKE_REPOSITORY
            assert payload["enabled"] is False
            return 200, {
                "repository": SMOKE_REPOSITORY,
                "enabled": False,
                "max_in_flight": 1,
            }
        if url.endswith("/v1/tasks") and method == "POST":
            if not self.created:
                self.created = True
                return 201, {"created": True, "task": {"task_id": self.task_id}}
            return 200, {"created": False, "task": {"task_id": self.task_id}}
        if url.endswith(f"/v1/tasks/{self.task_id}") and method == "GET":
            return 200, {
                "task_id": self.task_id,
                "state": "queued",
                "leased_by": None,
            }
        if url.endswith("/v1/snapshot") and method == "GET":
            return 200, {
                "repositories": [
                    {"repository": SMOKE_REPOSITORY, "enabled": False}
                ],
                "tasks": [
                    {
                        "task_id": self.task_id,
                        "state": "queued",
                        "leased_by": None,
                    }
                ],
            }
        raise AssertionError((method, url, payload))

def test_validate_pool_url_is_loopback_only() -> None:
    assert validate_pool_url("http://127.0.0.1:8097") == "http://127.0.0.1:8097"
    with pytest.raises(SmokeError, match="worker_pool_url_not_loopback"):
        validate_pool_url("https://example.com")

def test_smoke_proves_disabled_lane_replay_and_readback(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("test-token\n", encoding="utf-8")
    pool = FakePool()

    result = run_smoke(
        expected_sha=SHA,
        correlation_id="smoke-test",
        pool_url="http://127.0.0.1:8097",
        token_file=token_file,
        request_fn=pool.request,
        head_fn=lambda: SHA,
    )

    assert result["result"] == "WORKER_POOL_SMOKE_PASSED"
    assert result["lane_enabled"] is False
    assert result["task_state"] == "queued"
    assert result["leased_by"] is None
    assert result["first_created"] is True
    assert result["replay_created"] is False
    assert result["independent_readback"] is True
    assert result["secrets_exposed"] is False
    assert result["production_touched"] is False
    assert result["deploy_executed"] is False

def test_smoke_is_replay_safe_across_runs(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("test-token", encoding="utf-8")
    pool = FakePool()

    first = run_smoke(
        expected_sha=SHA,
        correlation_id="smoke-1",
        pool_url="http://localhost:8097",
        token_file=token_file,
        request_fn=pool.request,
        head_fn=lambda: SHA,
    )
    second = run_smoke(
        expected_sha=SHA,
        correlation_id="smoke-2",
        pool_url="http://localhost:8097",
        token_file=token_file,
        request_fn=pool.request,
        head_fn=lambda: SHA,
    )

    assert first["first_created"] is True
    assert second["first_created"] is False
    assert first["task_id"] == second["task_id"]

def test_smoke_fails_closed_on_sha_mismatch(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("test-token", encoding="utf-8")

    with pytest.raises(SmokeError, match="checkout_sha_mismatch"):
        run_smoke(
            expected_sha=SHA,
            correlation_id="smoke-negative",
            pool_url="http://127.0.0.1:8097",
            token_file=token_file,
            request_fn=FakePool().request,
            head_fn=lambda: "b" * 40,
        )
