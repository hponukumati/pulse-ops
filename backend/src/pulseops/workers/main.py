"""Dedicated worker process: rule evaluation, outbox dispatch, deletion jobs, retention.

Graceful shutdown on SIGTERM/SIGINT: loops stop taking new work, in-flight transactions
finish (bounded), then pools close. Several worker instances may run concurrently: rule
evaluation serializes on row locks + a unique slot key, dispatch and deletion jobs use
``SKIP LOCKED``, and hourly retention is guarded by an advisory lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert

from pulseops.config import get_settings, validate_for_startup
from pulseops.container import Container, build_container
from pulseops.logging import configure_logging, log_event
from pulseops.models import WorkerHeartbeat
from pulseops.workers import dispatcher, evaluator, maintenance

HEARTBEAT_EVERY_S = 15
DISPATCH_EVERY_S = 1.0
DELETION_EVERY_S = 5.0
RETENTION_EVERY = timedelta(hours=1)
SHUTDOWN_GRACE_S = 10


class Worker:
    def __init__(self, c: Container) -> None:
        self.c = c
        self.worker_id = f"{socket.gethostname()[:32]}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.stop = asyncio.Event()
        self.started_at = c.now()
        self.paused = False
        self.paused_reason: str | None = None
        self.last_slot: datetime | None = None
        self.last_dispatch_ok_at: datetime | None = None
        self.last_dispatch_error_at: datetime | None = None
        self.ledger = maintenance.DeletionLedger(c.settings.deletion_ledger_dir)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)

    async def evaluator_loop(self) -> None:
        while not self.stop.is_set():
            slot = evaluator.slot_for(self.c.now())
            if slot != self.last_slot:
                try:
                    result = await evaluator.evaluate_slot(self.c, slot, self.worker_id)
                    self.paused = result.paused
                    self.paused_reason = "OUTBOX_BACKLOG" if result.paused else None
                    if not result.paused:
                        self.last_slot = slot
                except Exception as exc:
                    log_event("worker.slot_failed", error_class=type(exc).__name__)
                    await self._sleep(5)
                    continue
            next_slot = slot + timedelta(seconds=evaluator.SLOT_SECONDS)
            await self._sleep(max(0.5, (next_slot - self.c.now()).total_seconds() + 0.5))

    async def dispatcher_loop(self) -> None:
        while not self.stop.is_set():
            try:
                r = await dispatcher.dispatch_once(self.c)
                self.last_dispatch_ok_at = r.last_ok_at or self.last_dispatch_ok_at
                self.last_dispatch_error_at = r.last_error_at or self.last_dispatch_error_at
                if r.sent == dispatcher.BATCH:
                    continue
            except Exception as exc:
                log_event("worker.dispatch_loop_failed", error_class=type(exc).__name__)
            await self._sleep(DISPATCH_EVERY_S)

    async def deletion_loop(self) -> None:
        while not self.stop.is_set():
            try:
                if await maintenance.process_deletion_job(self.c, self.ledger):
                    continue
            except Exception as exc:
                log_event("worker.deletion_loop_failed", error_class=type(exc).__name__)
            await self._sleep(DELETION_EVERY_S)

    async def retention_loop(self) -> None:
        while not self.stop.is_set():
            try:
                async with self.c.db.session() as s:
                    got = (await s.execute(text("SELECT pg_try_advisory_lock(hashtext('pulseops:retention'))"))).scalar_one()
                    if got:
                        try:
                            await maintenance.run_hourly(self.c, self.ledger)
                        finally:
                            await s.execute(text("SELECT pg_advisory_unlock(hashtext('pulseops:retention'))"))
            except Exception as exc:
                log_event("worker.retention_failed", error_class=type(exc).__name__)
            await self._sleep(RETENTION_EVERY.total_seconds())

    async def heartbeat_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self.heartbeat()
            except Exception as exc:
                log_event("worker.heartbeat_failed", error_class=type(exc).__name__)
            await self._sleep(HEARTBEAT_EVERY_S)

    async def heartbeat(self) -> None:
        now = self.c.now()
        values = {
            "worker_id": self.worker_id,
            "started_at": self.started_at,
            "heartbeat_at": now,
            "last_dispatch_ok_at": self.last_dispatch_ok_at,
            "last_dispatch_error_at": self.last_dispatch_error_at,
            "evaluations_paused": self.paused,
            "paused_reason": self.paused_reason,
            "last_slot": self.last_slot,
        }
        async with self.c.db.transaction() as s:
            stmt = insert(WorkerHeartbeat).values(**values)
            await s.execute(
                stmt.on_conflict_do_update(index_elements=["worker_id"], set_={k: v for k, v in values.items() if k != "worker_id"})
            )

    async def run(self) -> None:
        log_event("worker.started", worker_id=self.worker_id)
        loops = [self.heartbeat_loop(), self.evaluator_loop(), self.dispatcher_loop(), self.deletion_loop(), self.retention_loop()]
        tasks = [asyncio.create_task(loop) for loop in loops]
        await self.stop.wait()
        log_event("worker.stopping", worker_id=self.worker_id)
        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_S)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        log_event("worker.stopped", worker_id=self.worker_id)


async def main() -> None:
    configure_logging()
    settings = get_settings()
    validate_for_startup(settings)
    c = build_container(settings, application_name="pulseops-worker", with_maint=True)
    worker = Worker(c)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop.set)
    try:
        await worker.run()
    finally:
        await c.close()


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
