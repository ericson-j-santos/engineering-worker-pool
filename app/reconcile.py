from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from app.store import WorkerPoolStore


logger = logging.getLogger("codex-worker-pool.reconciler")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ReconciliationController:
    """Continuously reconciles expired leases and stalled tasks."""

    def __init__(
        self,
        store: WorkerPoolStore,
        *,
        interval_seconds: float = 5.0,
    ) -> None:
        if interval_seconds < 0.1:
            raise ValueError("reconciliation interval must be >= 0.1 seconds")
        self.store = store
        self.interval_seconds = float(interval_seconds)
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._last_started_at: str | None = None
        self._last_completed_at: str | None = None
        self._last_result: dict[str, int] | None = None
        self._consecutive_failures = 0
        self._last_error_code: str | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(
            self._run(),
            name="worker-pool-reconciliation-controller",
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        self._running = False
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def run_once(self) -> dict[str, int] | None:
        self._last_started_at = _utc_iso()
        try:
            result = await asyncio.to_thread(self.store.reconcile)
        except Exception as exc:
            self._consecutive_failures += 1
            self._last_error_code = "reconciliation_cycle_failed"
            self._last_completed_at = _utc_iso()
            logger.error(
                "reconciliation_cycle_failed exception_type=%s failures=%s",
                type(exc).__name__,
                self._consecutive_failures,
            )
            return None

        self._last_result = dict(result)
        self._consecutive_failures = 0
        self._last_error_code = None
        self._last_completed_at = _utc_iso()
        return dict(result)

    async def _run(self) -> None:
        try:
            while True:
                await self.run_once()
                await asyncio.sleep(self.interval_seconds)
        finally:
            self._running = False

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "interval_seconds": self.interval_seconds,
            "last_started_at": self._last_started_at,
            "last_completed_at": self._last_completed_at,
            "last_result": dict(self._last_result) if self._last_result is not None else None,
            "consecutive_failures": self._consecutive_failures,
            "last_error_code": self._last_error_code,
        }
