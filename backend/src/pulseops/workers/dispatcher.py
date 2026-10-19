"""Transactional outbox dispatcher.

Publishes only ``{serviceId, incidentId, eventId, version}`` after the transition has
committed. Rows are claimed with ``FOR UPDATE SKIP LOCKED`` so several workers can run.
A crash after publishing but before marking ``sent_at`` republishes the event: delivery is
*at least once*. Subscribers deduplicate by incident version and refetch authoritative
state from PostgreSQL, so a duplicate notification never becomes a duplicate transition.
"""

from __future__ import annotations

import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from redis.exceptions import RedisError
from sqlalchemy import select

from pulseops.container import Container
from pulseops.logging import log_event
from pulseops.models import OutboxEvent
from pulseops.notifications import CHANNEL

BATCH = 100
MAX_BACKOFF_S = 60.0


def backoff_delay(attempts: int) -> timedelta:
    """Bounded exponential backoff with jitter (max 60 s)."""
    base = min(MAX_BACKOFF_S, 2.0 ** min(attempts, 10))
    return timedelta(seconds=random.uniform(base / 2, base))


def payload(row: OutboxEvent) -> str:
    return json.dumps(
        {"serviceId": str(row.service_id), "incidentId": str(row.incident_id), "eventId": str(row.event_id), "version": row.version},
        separators=(",", ":"),
    )


@dataclass
class DispatchResult:
    sent: int = 0
    failed: int = 0
    last_ok_at: datetime | None = None
    last_error_at: datetime | None = None


async def dispatch_once(
    c: Container,
    *,
    publish: Callable[[str], Awaitable[object]] | None = None,
    after_publish: Callable[[OutboxEvent], Awaitable[None]] | None = None,
) -> DispatchResult:
    """Claim and publish one batch. ``after_publish`` is a test hook to simulate crashes."""
    pub = publish or (lambda data: c.redis.publish(CHANNEL, data))
    result = DispatchResult()
    now = c.now()
    async with c.db.transaction() as s:
        rows = list(
            (
                await s.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.sent_at.is_(None), OutboxEvent.next_attempt_at <= now)
                    .order_by(OutboxEvent.id)
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        failing = False
        for row in rows:
            if not failing:
                try:
                    await pub(payload(row))
                    if after_publish is not None:
                        await after_publish(row)
                    row.sent_at = now
                    result.sent += 1
                    result.last_ok_at = now
                    continue
                except (RedisError, OSError) as exc:
                    failing = True
                    log_event("worker.dispatch_failed", error_class=type(exc).__name__)
            # Redis is likely down: back off this and the remaining claimed rows.
            row.attempts += 1
            row.next_attempt_at = now + backoff_delay(row.attempts)
            row.last_error_code = "PUBLISH_FAILED"
            result.failed += 1
            result.last_error_at = now
    return result
