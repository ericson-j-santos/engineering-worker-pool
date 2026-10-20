"""Capability controls for result readback; doubles are not E2E evidence."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app import native_remote_readback as rb
from app.native_one_shot import NativeExecutorBlocked
from app.native_remote_runtime import HttpsWorkerApi


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", None])
def test_readback_api_rejects_every_non_get_before_transport(method):
    api = object.__new__(rb._ReadbackApi)
    with patch.object(HttpsWorkerApi, "_request") as request:
        with pytest.raises(NativeExecutorBlocked, match="^readback_effect_forbidden$"):
            api._request(method, "/v1/claims/fenced/exact", {})
    request.assert_not_called()


def test_readback_api_only_delegates_get_without_payload():
    api = object.__new__(rb._ReadbackApi)
    with patch.object(HttpsWorkerApi, "_request", return_value={"state": "completed"}) as request:
        assert api._request("GET", "/v1/tasks/cwp-" + "1" * 24) == {"state": "completed"}
        request.assert_called_once()
        with pytest.raises(NativeExecutorBlocked, match="^readback_effect_forbidden$"):
            api._request("GET", "/v1/tasks/cwp-" + "1" * 24, {})
        assert request.call_count == 1


@pytest.mark.parametrize("method", ["preflight", "publish", "materialize"])
def test_readback_artifact_capability_cannot_write(method):
    concrete = Mock()
    capability = rb._VerifyOnlyArtifacts(concrete)
    with pytest.raises(NativeExecutorBlocked, match="^readback_effect_forbidden$"):
        getattr(capability, method)(task_id="not-a-real-task")
    assert concrete.mock_calls == []


def test_readback_artifact_capability_only_delegates_verification():
    concrete = Mock()
    capability = rb._VerifyOnlyArtifacts(concrete)
    capability.verify(task_id="fixture")
    concrete.verify.assert_called_once_with(task_id="fixture")
    assert len(concrete.mock_calls) == 1


def test_borrowed_lock_cannot_release_outer_owner():
    lock = Mock()
    path = Path("/not-opened")
    held = rb._LockedReceipt(lock, path, b"fixed", "task", "builder")
    with patch.object(rb, "_receipt_bytes", return_value=b"fixed"):
        borrowed, returned = held.acquire("task", "builder")
        assert returned == path
        borrowed.unlink(missing_ok=True)
        lock.unlink.assert_not_called()
        with pytest.raises(NativeExecutorBlocked, match="^readback_receipt_identity$"):
            held.acquire("task", "builder")
    with pytest.raises(NativeExecutorBlocked, match="^readback_effect_forbidden$"):
        held.write(path, {})


@pytest.mark.parametrize("task,role", [("other", "builder"), ("task", "validator")])
def test_locked_receipt_does_not_allow_another_identity(task, role):
    held = rb._LockedReceipt(Mock(), Path("/not-opened"), b"fixed", "task", "builder")
    with patch.object(rb, "_receipt_bytes") as read:
        with pytest.raises(NativeExecutorBlocked, match="^readback_receipt_identity$"):
            held.acquire(task, role)
    read.assert_not_called()


def test_receipt_change_between_attestation_and_executor_is_blocked():
    held = rb._LockedReceipt(Mock(), Path("/not-opened"), b"fixed", "task", "builder")
    with patch.object(rb, "_receipt_bytes", return_value=b"changed"):
        with pytest.raises(NativeExecutorBlocked, match="^readback_receipt_changed$"):
            held.acquire("task", "builder")


def test_unknown_runtime_is_not_a_deployment_authority():
    with pytest.raises(NativeExecutorBlocked, match="^readback_runtime_invalid$"):
        rb.readback_remote_result(
            SimpleNamespace(), {}, role="builder", worker_id="fixture-builder",
        )


def test_real_receipt_file_controls(tmp_path):
    path = tmp_path / "receipt.json"
    data = b'{"fixture":true}\n'
    path.write_bytes(data)
    path.chmod(0o600)
    assert rb._receipt_bytes(path) == data
    path.chmod(0o644)
    with pytest.raises(NativeExecutorBlocked, match="^readback_receipt_invalid$"):
        rb._receipt_bytes(path)
    path.chmod(0o600)
    assert rb._receipt_bytes(path) == data
    linked = tmp_path / "linked.json"
    linked.symlink_to(path)
    with pytest.raises(NativeExecutorBlocked, match="^readback_receipt_invalid$"):
        rb._receipt_bytes(linked)
    with pytest.raises(NativeExecutorBlocked, match="^readback_receipt_unavailable$"):
        rb._receipt_bytes(tmp_path / "missing.json")


def test_receipt_size_is_bounded(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_bytes(b"x" * (rb.MAX_RECEIPT_BYTES + 1))
    path.chmod(0o600)
    with pytest.raises(NativeExecutorBlocked, match="^readback_receipt_invalid$"):
        rb._receipt_bytes(path)


def test_unqualified_platform_fails_before_open():
    path = Path("/not-opened")
    with patch.object(rb.os, "name", "nt"):
        with patch.object(rb.os, "open") as opened:
            with pytest.raises(NativeExecutorBlocked, match="^readback_platform_unqualified$"):
                rb._receipt_bytes(path)
    opened.assert_not_called()


def test_hardlinked_receipt_is_not_accepted(tmp_path):
    path = tmp_path / "receipt.json"
    path.write_bytes(b"{}")
    path.chmod(0o600)
    other = tmp_path / "other.json"
    rb.os.link(path, other)
    with pytest.raises(NativeExecutorBlocked, match="^readback_receipt_invalid$"):
        rb._receipt_bytes(path)
    other.unlink()
    assert rb._receipt_bytes(path) == b"{}"


def test_composition_e2e_is_required_in_existing_ci():
    import yaml
    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    )
    steps = workflow["jobs"]["test"]["steps"]
    matches = [(index, step) for index, step in enumerate(steps)
               if step.get("name") == "Verify remote composition and read-only result replay"]
    assert len(matches) == 1
    index, step = matches[0]
    assert step["run"] == (
        "python scripts/remote_native_composition_e2e.py --rules-scripts _rules_native/scripts"
    )
    assert step["if"] == (
        "github.event_name == 'pull_request' && "
        "github.event.pull_request.head.repo.full_name == github.repository"
    )
    assert step["timeout-minutes"] == 2
    assert "continue-on-error" not in step
    assert workflow["permissions"] == {"contents": "read"}
    assert any(s.get("with", {}).get("path") == "_rules_native"
               and s["with"].get("ref") == "ddd09dc616181c0aaf46d33a194e9b00a6989332"
               for s in steps[:index])
    assert (Path(__file__).resolve().parents[1] / "scripts/remote_native_composition_e2e.py").is_file()
