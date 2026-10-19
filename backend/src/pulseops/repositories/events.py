"""Aggregate queries over raw retained events (exact percentiles; never averaged buckets)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, INTERVAL, TIMESTAMP, UUID
from sqlalchemy.ext.asyncio import AsyncSession

_ROUTE_FILTER = "AND (CAST(:route_id AS uuid) IS NULL OR route_id = CAST(:route_id AS uuid))"

_SUMMARY = text(
    f"""
    SELECT count(*) AS total,
           count(*) FILTER (WHERE status_code >= 500) AS e5,
           count(*) FILTER (WHERE status_code BETWEEN 400 AND 499) AS e4,
           percentile_cont(ARRAY[0.5, 0.95, 0.99]) WITHIN GROUP (ORDER BY duration_ms) AS pct,
           max(occurred_at) AS last_event_at
    FROM request_events
    WHERE service_id = :service_id AND occurred_at >= :start AND occurred_at < :end {_ROUTE_FILTER}
    """
)

_SERIES = text(
    f"""
    SELECT date_bin(:bucket, occurred_at, TIMESTAMPTZ '1970-01-01 00:00:00+00') AS b,
           count(*) AS total,
           count(*) FILTER (WHERE status_code >= 500) AS e5,
           count(*) FILTER (WHERE status_code BETWEEN 400 AND 499) AS e4,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95
    FROM request_events
    WHERE service_id = :service_id AND occurred_at >= :start AND occurred_at < :end {_ROUTE_FILTER}
    GROUP BY b
    """
).bindparams(bindparam("bucket", type_=INTERVAL()))

_ENDPOINTS = text(
    """
    SELECT e.route_id, r.template, e.method, count(*) AS total,
           count(*) FILTER (WHERE e.status_code >= 500) AS e5,
           count(*) FILTER (WHERE e.status_code BETWEEN 400 AND 499) AS e4,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY e.duration_ms) AS p95
    FROM request_events e JOIN routes r ON r.service_id = e.service_id AND r.id = e.route_id
    WHERE e.service_id = :service_id AND e.occurred_at >= :start AND e.occurred_at < :end
    GROUP BY e.route_id, r.template, e.method
    ORDER BY total DESC, r.template, e.method
    LIMIT :limit OFFSET :offset
    """
)

_HEALTH = text(
    """
    SELECT s.id,
      (SELECT count(*) FROM request_events e
        WHERE e.service_id = s.id AND e.occurred_at >= :health_start AND e.occurred_at < :as_of) AS recent,
      (SELECT max(e.occurred_at) FROM request_events e
        WHERE e.service_id = s.id AND e.occurred_at >= :retention_start AND e.occurred_at < :as_of) AS last_event_at,
      (SELECT count(*) FROM incidents i WHERE i.service_id = s.id AND i.status IN ('OPEN','ACKNOWLEDGED')) AS active
    FROM services s
    WHERE s.id = ANY(:ids) AND s.deleted_at IS NULL
    """
).bindparams(
    bindparam("ids", type_=ARRAY(UUID(as_uuid=True))),
    bindparam("health_start", type_=TIMESTAMP(timezone=True)),
    bindparam("retention_start", type_=TIMESTAMP(timezone=True)),
    bindparam("as_of", type_=TIMESTAMP(timezone=True)),
)


def _params(service_id: uuid.UUID, start: datetime, end: datetime, route_id: uuid.UUID | None) -> dict[str, Any]:
    return {"service_id": service_id, "start": start, "end": end, "route_id": str(route_id) if route_id else None}


async def retention_days(s: AsyncSession) -> int:
    return int((await s.execute(text("SELECT event_retention_days FROM app_settings WHERE id = 1"))).scalar_one())


async def summary(s: AsyncSession, service_id: uuid.UUID, start: datetime, end: datetime, route_id: uuid.UUID | None) -> Any:
    return (await s.execute(_SUMMARY, _params(service_id, start, end, route_id))).one()


async def series(
    s: AsyncSession, service_id: uuid.UUID, start: datetime, end: datetime, route_id: uuid.UUID | None, bucket: timedelta
) -> list[Any]:
    return list((await s.execute(_SERIES, {**_params(service_id, start, end, route_id), "bucket": bucket})).all())


async def endpoints(s: AsyncSession, service_id: uuid.UUID, start: datetime, end: datetime, *, limit: int, offset: int) -> list[Any]:
    return list(
        (await s.execute(_ENDPOINTS, {"service_id": service_id, "start": start, "end": end, "limit": limit, "offset": offset})).all()
    )


async def health_rows(
    s: AsyncSession, ids: list[uuid.UUID], as_of: datetime, health_start: datetime, retention_start: datetime
) -> list[Any]:
    return list(
        (await s.execute(_HEALTH, {"ids": ids, "as_of": as_of, "health_start": health_start, "retention_start": retention_start})).all()
    )


async def latest_heartbeat(s: AsyncSession) -> tuple[datetime | None, bool]:
    row = (
        await s.execute(
            text(
                "SELECT max(heartbeat_at), coalesce(bool_or(evaluations_paused)"
                " FILTER (WHERE heartbeat_at > now() - interval '120 seconds'), false) FROM worker_heartbeats"
            )
        )
    ).one()
    return row[0], bool(row[1])
