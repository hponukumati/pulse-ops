"""Incident state machine shared by the API (ack/resolve) and the alert worker.

A transition, its ``incident_events`` row and its ``outbox_events`` row are always written
in the caller's single transaction. ``(incident_id, version)`` is unique, so a transition
can never be stored twice even if two processes race.

Lock order everywhere: alert rule row first, then incident row (prevents deadlocks).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pulseops.models import AlertRule, Incident, IncidentEvent, OutboxEvent

ACTIVE = ("OPEN", "ACKNOWLEDGED")


@dataclass(frozen=True)
class StreakUpdate:
    breach: int
    ok: int
    open_incident: bool
    resolve_incident: bool


def next_streaks(
    *, breach: int, ok: int, last_slot: datetime | None, slot: datetime, slot_seconds: int, status: str, has_active: bool
) -> StreakUpdate:
    """Pure streak logic.

    * A missed or failed previous slot (gap) resets both streaks: observations are never
      manufactured by replaying missed intervals.
    * INSUFFICIENT_DATA / UNKNOWN reset streaks and leave incidents unchanged.
    * Two consecutive eligible breaches open; two consecutive eligible non-breaches resolve.
    """
    contiguous = last_slot is not None and int((slot - last_slot).total_seconds()) == slot_seconds
    if not contiguous:
        breach, ok = 0, 0
    if status == "BREACH":
        breach, ok = breach + 1, 0
    elif status == "OK":
        breach, ok = 0, ok + 1
    else:
        breach, ok = 0, 0
    return StreakUpdate(
        breach=breach,
        ok=ok,
        open_incident=status == "BREACH" and breach >= 2 and not has_active,
        resolve_incident=status == "OK" and ok >= 2 and has_active,
    )


async def lock_rule(s: AsyncSession, rule_id: uuid.UUID) -> AlertRule | None:
    return (await s.execute(select(AlertRule).where(AlertRule.id == rule_id).with_for_update())).scalar_one_or_none()


async def lock_active_incident(s: AsyncSession, rule_id: uuid.UUID) -> Incident | None:
    q = select(Incident).where(Incident.rule_id == rule_id, Incident.status.in_(ACTIVE)).with_for_update()
    return (await s.execute(q)).scalar_one_or_none()


def _record(
    s: AsyncSession, inc: Incident, kind: str, now: datetime, *, reason: str | None, actor: uuid.UUID | None, slot: datetime | None
) -> None:
    s.add(
        IncidentEvent(
            id=uuid.uuid4(),
            service_id=inc.service_id,
            incident_id=inc.id,
            version=inc.version,
            kind=kind,
            reason=reason,
            actor_id=actor,
            scheduled_at=slot,
            occurred_at=now,
        )
    )
    s.add(
        OutboxEvent(
            event_id=uuid.uuid4(),
            service_id=inc.service_id,
            incident_id=inc.id,
            version=inc.version,
            created_at=now,
            attempts=0,
            next_attempt_at=now,
        )
    )


async def open_incident(s: AsyncSession, rule: AlertRule, now: datetime, *, value: float | None, slot: datetime | None) -> Incident:
    inc = Incident(
        id=uuid.uuid4(),
        service_id=rule.service_id,
        rule_id=rule.id,
        status="OPEN",
        version=1,
        metric=rule.metric,
        threshold=rule.threshold,
        opened_value=value,
        opened_at=now,
        updated_at=now,
    )
    s.add(inc)
    await s.flush()  # partial unique index rejects a second active incident for the rule
    _record(s, inc, "OPENED", now, reason=None, actor=None, slot=slot)
    return inc


def acknowledge(s: AsyncSession, inc: Incident, now: datetime, actor: uuid.UUID | None) -> None:
    inc.status, inc.acknowledged_at, inc.acknowledged_by = "ACKNOWLEDGED", now, actor
    inc.version += 1
    inc.updated_at = now
    _record(s, inc, "ACKNOWLEDGED", now, reason=None, actor=actor, slot=None)


def resolve(s: AsyncSession, inc: Incident, now: datetime, *, reason: str, actor: uuid.UUID | None, slot: datetime | None = None) -> None:
    inc.status, inc.resolved_at, inc.resolved_by, inc.resolve_reason = "RESOLVED", now, actor, reason
    inc.version += 1
    inc.updated_at = now
    _record(s, inc, "RESOLVED", now, reason=reason, actor=actor, slot=slot)
