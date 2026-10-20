"""Unit contracts for the native shared-authority harness.

These tests do not qualify PostgreSQL, S3, hosted runners, or a native Gateway.
Provider/runtime qualification is the separate producer -> consumer E2E.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "native_shared_continuity_unit",
    Path(__file__).resolve().parents[1] / "scripts/native_shared_continuity.py",
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("native_harness_module_unavailable")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class DocumentControls(unittest.TestCase):
    def test_canonical_document_roundtrip_and_digest(self):
        value = {"nested": {"b": 2, "a": 1}, "phase": "prepared"}
        raw = m.canonical(value).decode()
        self.assertEqual(m.document(raw, m.digest(raw.encode())), value)

    def test_integrity_rejects_changed_content(self):
        raw = m.canonical({"phase": "prepared"}).decode()
        checksum = m.digest(raw.encode())
        with self.assertRaisesRegex(m.HarnessBlocked, "document_integrity"):
            m.document(raw.replace("prepared", "verified"), checksum)

    def test_duplicate_keys_are_rejected_even_with_matching_digest(self):
        raw = '{"phase":"prepared","phase":"verified"}'
        with self.assertRaisesRegex(m.HarnessBlocked, "document_duplicate_key"):
            m.document(raw, m.digest(raw.encode()))

    def test_noncanonical_and_nonfinite_json_rejected(self):
        for raw in ('{"b": 2, "a": 1}', '{"x":NaN}', '{"x":Infinity}', "[]"):
            with self.subTest(raw=raw), self.assertRaises(m.HarnessBlocked):
                m.document(raw)

    def test_document_bound_is_applied_before_parse(self):
        with self.assertRaisesRegex(m.HarnessBlocked, "document_size"):
            m.document(" " * (m.MAX_DOCUMENT + 1))

    def test_private_document_is_born_private_and_readback_matches(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "receipt.json"
            m.protected_json(path, {"ok": True})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(m.read_private_json(path), {"ok": True})
            with self.assertRaisesRegex(m.HarnessBlocked, "receipt_already_exists"):
                m.protected_json(path, {"ok": False})

    def test_private_document_refuses_group_read_access(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "receipt.json"
            m.protected_json(path, {"ok": True})
            path.chmod(0o640)
            with self.assertRaisesRegex(m.HarnessBlocked, "private_document_permissions"):
                m.read_private_json(path)

    def test_private_document_refuses_symlink_or_hardlink(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "receipt.json"
            m.protected_json(path, {"ok": True})
            link = Path(name) / "link.json"
            link.symlink_to(path)
            with self.assertRaises(m.HarnessBlocked):
                m.read_private_json(link)
            link.unlink()
            os.link(path, link)
            with self.assertRaisesRegex(m.HarnessBlocked, "private_document_permissions"):
                m.read_private_json(path)


class FixtureIdentityControls(unittest.TestCase):
    def test_builder_resume_requires_real_empty_command_observation(self):
        m.require_builder_resume_commands(
            {"runtime_commands_invoked": [], "runtime_command_count": 0})

    def test_builder_resume_rejects_missing_nonempty_or_boolean_counts(self):
        for receipt in (
            {}, {"runtime_commands_invoked": [], "runtime_command_count": False},
            {"runtime_commands_invoked": ["ci.patch.fix"], "runtime_command_count": 1},
            {"runtime_commands_invoked": [], "runtime_command_count": 1},
            {"runtime_commands_invoked": (), "runtime_command_count": 0},
        ):
            with self.subTest(receipt=receipt), self.assertRaisesRegex(
                    m.HarnessBlocked, "builder_resume_executed_commands"):
                m.require_builder_resume_commands(receipt)

    def test_phase_cannot_jump_from_prepared_to_verified(self):
        with self.assertRaisesRegex(m.HarnessBlocked, "phase_transition_invalid"):
            m.transition(None, {"run_key": "native-12345-1"}, "prepared", "verified")

    def test_fresh_git_sources_have_the_same_base_without_shared_disk(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            a, b = root / "a", root / "b"
            self.assertEqual(m.source_fixture(a), m.source_fixture(b))
            self.assertEqual(m.git(a, "remote"), "")
            self.assertEqual(m.git(b, "status", "--porcelain"), "")
            self.assertNotEqual((a / ".git").resolve(), (b / ".git").resolve())
            with self.assertRaisesRegex(m.HarnessBlocked, "fixture_source_exists"):
                m.source_fixture(a)

    def test_worker_ids_are_scoped_to_run_and_attempt(self):
        a = {"run_key": "native-12345-1"}
        b = {"run_key": "native-12345-2"}
        self.assertNotEqual(m.worker_id(a, "builder-a"), m.worker_id(b, "builder-a"))
        self.assertNotEqual(m.worker_id(a, "builder-a"), m.worker_id(a, "builder-b"))
        with self.assertRaisesRegex(m.HarnessBlocked, "worker_role_invalid"):
            m.worker_id(a, "product-worker")

    def test_lease_readback_rejects_foreign_worker_and_boolean_generation(self):
        identity = {"run_key": "native-12345-1"}
        task = {"leased_by": m.worker_id(identity, "builder-a"),
                "lease_token": "unit-only-lease-value", "lease_generation": 1,
                "attempt_id": "attempt-one"}
        self.assertEqual(m.lease_fields(task, identity)["lease_generation"], 1)
        for extra in ({"leased_by": "foreign-worker"}, {"lease_generation": True},
                      {"lease_token": "short"}, {"attempt_id": None}):
            with self.subTest(extra=extra), self.assertRaises(m.HarnessBlocked):
                m.lease_fields({**task, **extra}, identity)


class ProcessInventoryControls(unittest.TestCase):
    def test_proc_stat_handles_spaces_and_parentheses_in_name(self):
        fields = ["T", "7", "123", "123"] + ["0"] * 15 + ["9876"]
        raw = "123 (python worker) child) " + " ".join(fields)
        self.assertEqual(m.process_record(raw),
                         {"pid": 123, "state": "T", "ppid": 7, "pgid": 123,
                          "session": 123, "start_ticks": 9876})

    def test_proc_stat_refuses_missing_fields(self):
        for value in ("not a stat", "1 (worker) R 1 1", "bad (worker) " + "0 " * 20):
            with self.subTest(value=value), self.assertRaises(m.HarnessBlocked):
                m.process_record(value)

    def test_owned_tree_requires_api_in_same_group(self):
        group = 123456
        inventory = {
            group: {"pid": group, "ppid": 1, "pgid": group, "session": group, "state": "T"},
            123457: {"pid": 123457, "ppid": group, "pgid": group,
                     "session": group, "state": "T"},
        }
        with patch.object(m, "process_inventory", return_value=inventory), \
                patch.object(m.os, "getpgrp", return_value=999):
            self.assertEqual(len(m.require_owned_tree(group, 123457)), 2)
            with self.assertRaisesRegex(m.HarnessBlocked, "actor_tree_not_owned"):
                m.require_owned_tree(group, 999)

    def test_escaped_descendant_blocks_tree_termination_claim(self):
        group = 123456
        inventory = {
            group: {"pid": group, "ppid": 1, "pgid": group, "session": group, "state": "T"},
            123457: {"pid": 123457, "ppid": group, "pgid": group,
                     "session": group, "state": "T"},
            123458: {"pid": 123458, "ppid": 123457, "pgid": 123458,
                     "session": 123458, "state": "S"},
        }
        with patch.object(m, "process_inventory", return_value=inventory), \
                patch.object(m.os, "getpgrp", return_value=999):
            with self.assertRaisesRegex(m.HarnessBlocked, "actor_descendant_escaped_group"):
                m.require_owned_tree(group, 123457)


class AsgiProtocolControls(unittest.IsolatedAsyncioTestCase):
    """A tiny ASGI fixture proves ordering only; it is not a provider emulator."""

    def config(self, root, role="a"):
        return {"task_id": "cwp-" + "a" * 24, "role": role,
                "identity": {"run_key": "native-12345-1"},
                "barrier": str(root / "barrier.json")}

    async def test_unrelated_requests_are_forwarded_without_interception(self):
        calls, sent = [], []
        async def app(scope, receive, send):
            calls.append(scope["path"])
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"{}", "more_body": False})
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            sent.append(message)
        with tempfile.TemporaryDirectory() as name:
            gate = m.ContinuityGate(app, self.config(Path(name)))
            await gate({"type": "http", "method": "GET", "path": "/v1/contract"},
                       receive, send)
            self.assertEqual(calls, ["/v1/contract"])
            self.assertEqual(sent[0]["status"], 200)
            self.assertFalse(gate.gated)

    async def test_a_barrier_exists_only_after_application_response_and_holds_response(self):
        order, sent = [], []
        async def app(scope, receive, send):
            value = await receive()
            self.assertIn(b"builder_result", value["body"])
            order.append("route_committed_fixture")
            await send({"type": "http.response.start", "status": 201})
            await send({"type": "http.response.body", "body": b"{}", "more_body": False})
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = self.config(root)
            payload = {"worker_id": m.worker_id(config["identity"], "builder-a"),
                       "lease_generation": 1, "stage": "builder_result",
                       "next_action": "submit_validation", "effect_state": "confirmed",
                       "checkpoint_id": "checkpoint-one"}
            async def receive():
                return {"type": "http.request", "body": json.dumps(payload).encode(),
                        "more_body": False}
            async def send(message):
                sent.append(message)
            metadata_patch = patch.object(
                m, "persist_a_checkpoint",
                side_effect=lambda *_args: order.append("metadata_persisted_fixture"))
            metadata_patch.start()
            self.addCleanup(metadata_patch.stop)
            gate = m.ContinuityGate(app, config)
            task = asyncio.create_task(gate(
                {"type": "http", "method": "POST",
                 "path": "/v1/tasks/" + config["task_id"] + "/checkpoints"}, receive, send))
            try:
                for _ in range(1000):
                    if (root / "barrier.json").exists():
                        break
                    await asyncio.sleep(0.001)
                self.assertTrue((root / "barrier.json").exists())
                self.assertEqual(order, ["route_committed_fixture",
                                         "metadata_persisted_fixture"])
                self.assertEqual(sent, [])
                self.assertFalse(task.done())
                self.assertEqual(m.read_private_json(root / "barrier.json")["checkpoint_id"],
                                 "checkpoint-one")
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task


if __name__ == "__main__":
    unittest.main()
