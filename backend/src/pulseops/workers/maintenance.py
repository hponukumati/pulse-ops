"""Retention purges, service-deletion jobs and the protected deletion ledger.

* Events: physically deleted in chunks once older than the retention window (read-time
  filtering hides them even if this job is late). The retained-event quota counter is
  decremented in the same transaction as each chunk.
* Rule evaluations 7 d; resolved incidents/transitions 30 d; sent outbox 24 h; expired
  session families after absolute expiry; audit 30 d (via the separate maintenance role).
* Service deletion jobs are retryable and idempotent. Before purging, the worker appends
  the service id/time to a small ledger kept outside the database backup so restores can
  reapply deletions.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from redis.exceptions import RedisError
from sqlalchemy import select, text

from pulseops.container import Container
from pulseops.logging import log_event
from pulseops.models import DeletionJob
from pulseops.workers.dispatcher import backoff_delay

CHUNK = 5000
EVALUATION_RETENTION = timedelta(days=7)
INCIDENT_RETENTION = timedelta(days=30)
AUDIT_RETENTION = timedelta(days=30)
SENT_OUTBOX_RETENTION = timedelta(hours=24)
LEDGER_RETENTION = timedelta(days=30)  # >= 15 days required
JOB_LEASE = timedelta(minutes=10)


# ---- deletion ledger (outside the DB) ----


class DeletionLedger:
    def __init__(self, directory: str) -> None:
        self.path = Path(directory) / "deletions.jsonl"

    def entries(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):
                d = json.loads(line)
                if isinstance(d, dict) and {"serviceId", "deletedAt"} <= set(d):
                    out.append({"serviceId": str(d["serviceId"]), "deletedAt": str(d["deletedAt"])})
        return out

    def append(self, service_id: uuid.UUID, deleted_at: datetime) -> None:
        """Idempotent, fsynced append containing only the service id and time."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0)
            if any(str(service_id) in line for line in fh.read().splitlines()):
                return
            fh.write(json.dumps({"serviceId": str(service_id), "deletedAt": deleted_at.isoformat()}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def prune(self, now: datetime) -> None:
        if not self.path.exists():
            return
        keep = [e for e in self.entries() if datetime.fromisoformat(e["deletedAt"]) >= now - LEDGER_RETENTION]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(e) + "\n" for e in keep), encoding="utf-8")
        os.replace(tmp, self.path)


# ---- retention ----


async def purge_events(c: Container) -> int:
    total = 0
    while True:
        async with c.db.transaction() as s:
            days = int((await s.execute(text("SELECT event_retention_days FROM app_settings WHERE id = 1"))).scalar_one())
            cutoff = c.now() - timedelta(days=days)
            n = int(
                (
                    await s.execute(
                        text(
                            """
                            WITH doomed AS (
                                SELECT service_id, event_id FROM request_events WHERE occurred_at < :cutoff LIMIT :chunk
                            ), d AS (
                                DELETE FROM request_events e USING doomed
                                WHERE e.service_id = doomed.service_id AND e.event_id = doomed.event_id
                                RETURNING 1
                            ), q AS (
                                UPDATE ingest_quota SET used = greatest(0, used - (SELECT count(*) FROM d)) WHERE id = 1
                            )
                            SELECT count(*) FROM d
                            """
                        ),
                        {"cutoff": cutoff, "chunk": CHUNK},
                    )
                ).scalar_one()
            )
        total += n
        if n < CHUNK:
            return total


async def purge_misc(c: Container) -> dict[str, int]:
    now = c.now()
    counts: dict[str, int] = {}
    statements = {
        "evaluations": ("DELETE FROM rule_evaluations WHERE scheduled_at < :t", now - EVALUATION_RETENTION),
        "outbox": ("DELETE FROM outbox_events WHERE sent_at IS NOT NULL AND sent_at < :t", now - SENT_OUTBOX_RETENTION),
        "sessions": ("DELETE FROM sessions WHERE expires_at < :t", now),  # cascades refresh-token replay records
        "heartbeats": ("DELETE FROM worker_heartbeats WHERE heartbeat_at < :t", now - timedelta(days=1)),
        "jobs": ("DELETE FROM deletion_jobs WHERE status = 'DONE' AND completed_at < :t", now - AUDIT_RETENTION),
    }
    async with c.db.transaction() as s:
        for name, (sql, t) in statements.items():
            counts[name] = (await s.execute(text(sql), {"t": t})).rowcount or 0  # type: ignore[attr-defined]
        cutoff = now - INCIDENT_RETENTION
        await s.execute(
            text(
                "DELETE FROM incident_events ie USING incidents i WHERE ie.incident_id = i.id"
                " AND i.status = 'RESOLVED' AND i.resolved_at < :t"
            ),
            {"t": cutoff},
        )
        counts["incidents"] = (
            await s.execute(text("DELETE FROM incidents WHERE status = 'RESOLVED' AND resolved_at < :t"), {"t": cutoff})
        ).rowcount or 0  # type: ignore[attr-defined]
    return counts


async def purge_audit(c: Container) -> int:
    """Audit retention runs with the maintenance role; the runtime role cannot delete audit rows."""
    if c.maint_db is None:
        return 0
    async with c.maint_db.transaction() as s:
        res = await s.execute(text("DELETE FROM audit_events WHERE occurred_at < :t"), {"t": c.now() - AUDIT_RETENTION})
        return res.rowcount or 0  # type: ignore[attr-defined]


async def reconcile_quota(c: Container) -> int:
    async with c.db.transaction() as s:
        await s.execute(text("SELECT used FROM ingest_quota WHERE id = 1 FOR UPDATE"))  # blocks concurrent ingests
        n = int((await s.execute(text("SELECT count(*) FROM request_events"))).scalar_one())  # fresh snapshot after lock
        await s.execute(text("UPDATE ingest_quota SET used = :n WHERE id = 1"), {"n": n})
    return n


# ---- service deletion ----


async def process_deletion_job(c: Container, ledger: DeletionLedger) -> bool:
    now = c.now()
    async with c.db.transaction() as s:
        job = (
            await s.execute(
                select(DeletionJob)
                .where(DeletionJob.status.in_(("PENDING", "FAILED", "RUNNING")), DeletionJob.next_attempt_at <= now)
                .order_by(DeletionJob.requested_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if job is None:
            return False
        job.status, job.attempts, job.next_attempt_at = "RUNNING", job.attempts + 1, now + JOB_LEASE
        job_id, service_id, requested_at, attempts = job.id, job.service_id, job.requested_at, job.attempts
    try:
        ledger.append(service_id, requested_at)
        while True:
            async with c.db.transaction() as s:
                n = int(
                    (
                        await s.execute(
                            text(
                                """
                                WITH doomed AS (SELECT event_id FROM request_events WHERE service_id = :sid LIMIT :chunk),
                                d AS (DELETE FROM request_events e USING doomed
                                      WHERE e.service_id = :sid AND e.event_id = doomed.event_id RETURNING 1),
                                q AS (UPDATE ingest_quota SET used = greatest(0, used - (SELECT count(*) FROM d)) WHERE id = 1)
                                SELECT count(*) FROM d
                                """
                            ),
                            {"sid": service_id, "chunk": CHUNK},
                        )
                    ).scalar_one()
                )
            if n < CHUNK:
                break
        async with c.db.transaction() as s:
            for sql in (
                "DELETE FROM rule_evaluations WHERE service_id = :sid",
                "DELETE FROM outbox_events WHERE service_id = :sid",
                "DELETE FROM incident_events WHERE service_id = :sid",
                "DELETE FROM incidents WHERE service_id = :sid",
                "DELETE FROM alert_rules WHERE service_id = :sid",
                "DELETE FROM ingest_tokens WHERE service_id = :sid",
                "DELETE FROM routes WHERE service_id = :sid",
                "DELETE FROM service_grants WHERE service_id = :sid",
                "DELETE FROM services WHERE id = :sid AND deleted_at IS NOT NULL",
            ):
                await s.execute(text(sql), {"sid": service_id})
            await s.execute(
                text("UPDATE deletion_jobs SET status = 'DONE', completed_at = :now, last_error_code = NULL WHERE id = :id"),
                {"now": c.now(), "id": job_id},
            )
        with contextlib.suppress(RedisError, OSError):
            async for key in c.redis.scan_iter(match=f"mc:v1:{service_id}:*", count=200):
                await c.redis.delete(key)
        log_event("worker.deletion_done", job_id=job_id, service_id=service_id)
    except Exception as exc:
        log_event("worker.deletion_failed", job_id=job_id, error_class=type(exc).__name__, attempt=attempts)
        with contextlib.suppress(Exception):
            async with c.db.transaction() as s:
                await s.execute(
                    text("UPDATE deletion_jobs SET status = 'FAILED', next_attempt_at = :t, last_error_code = :code WHERE id = :id"),
                    {"t": c.now() + backoff_delay(attempts), "code": type(exc).__name__[:48], "id": job_id},
                )
    return True


async def run_hourly(c: Container, ledger: DeletionLedger) -> None:
    events = await purge_events(c)
    misc = await purge_misc(c)
    audit = await purge_audit(c)
    used = await reconcile_quota(c)
    ledger.prune(c.now())
    log_event("worker.retention_done", count=events + audit + sum(misc.values()), pending=used)
