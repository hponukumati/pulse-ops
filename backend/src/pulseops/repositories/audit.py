"""Audit trail persistence. Records hold IDs and codes only: no emails, tokens or payloads."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from pulseops.models import AuditEvent

if TYPE_CHECKING:
    from pulseops.policy import Principal

COALESCE_BUCKET = timedelta(minutes=5)


class ActorFields(TypedDict):
    actor_kind: str
    actor_id: uuid.UUID | None
    request_id: uuid.UUID | None


def actor_fields(principal: Principal) -> ActorFields:
    from pulseops.container import current_request_id

    return {"actor_kind": principal.actor_kind, "actor_id": principal.user_id, "request_id": current_request_id()}


async def insert_audit(
    s: AsyncSession,
    *,
    now: datetime,
    action: str,
    result: str,
    actor_kind: str,
    actor_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    coalesce: bool = False,
) -> None:
    """Insert an audit record in the caller's transaction.

    ``coalesce=True`` bounds storage for repetitive failures: at most one row per
    (action, actor, target, result) per 5-minute bucket. The runtime role cannot
    UPDATE audit rows, so coalescing is insert-once rather than a counter.
    """
    key = None
    if coalesce:
        bucket = int(now.timestamp() // COALESCE_BUCKET.total_seconds())
        key = f"{action}:{result}:{actor_id or '-'}:{target_id or '-'}:{bucket}"[:96]
    stmt = insert(AuditEvent).values(
        id=uuid.uuid4(),
        occurred_at=now,
        actor_kind=actor_kind,
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        result=result,
        request_id=request_id,
        coalesce_key=key,
    )
    if coalesce:
        stmt = stmt.on_conflict_do_nothing(index_elements=["coalesce_key"], index_where=text("coalesce_key IS NOT NULL"))
    await s.execute(stmt)


async def list_audit(s: AsyncSession, *, since: datetime, after: tuple[datetime, uuid.UUID] | None, limit: int) -> list[AuditEvent]:
    q = select(AuditEvent).where(AuditEvent.occurred_at >= since)
    if after:
        q = q.where(text("(occurred_at, id) < (:a_ts, :a_id)").bindparams(a_ts=after[0], a_id=after[1]))
    q = q.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc()).limit(limit)
    return list((await s.execute(q)).scalars())
