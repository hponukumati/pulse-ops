"""Service metrics over stored events.

Windows are UTC ``[start, end)`` with one server ``asOf`` per operation. Percentiles use
PostgreSQL ``percentile_cont`` over raw retained events. Read-time retention filtering
applies even if the purge job is late. Aggregates may be served from a short-lived Redis
cache, but only *after* the caller has been authorized for the service.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from redis.exceptions import RedisError

from pulseops.container import Container
from pulseops.errors import NotFound
from pulseops.policy import Action, Principal, authorize
from pulseops.repositories import catalog as catalog_repo
from pulseops.repositories import events as repo

METRICS_STATEMENT_TIMEOUT_MS = 2000
HEALTH_WINDOW = timedelta(minutes=15)
HEALTH_MIN_SAMPLES = 20
TELEMETRY_STALE_AFTER = timedelta(minutes=5)
WORKER_STALE_AFTER = timedelta(seconds=120)
CACHE_TTL_S = 10
AS_OF_GRANULARITY_S = 2
TOP_ENDPOINTS = 10


class MetricsRange(StrEnum):
    M15 = "15m"
    H1 = "1h"
    H24 = "24h"
    D7 = "7d"


RANGE_SPEC: dict[MetricsRange, tuple[timedelta, timedelta]] = {
    MetricsRange.M15: (timedelta(minutes=15), timedelta(minutes=1)),
    MetricsRange.H1: (timedelta(hours=1), timedelta(minutes=1)),
    MetricsRange.H24: (timedelta(hours=24), timedelta(minutes=15)),
    MetricsRange.D7: (timedelta(days=7), timedelta(hours=1)),
}


def operation_as_of(now: datetime) -> datetime:
    """One asOf per operation, floored so concurrent dashboards can share cache entries."""
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % AS_OF_GRANULARITY_S, UTC)


def _rate(part: int, total: int) -> float:
    return part / total if total else 0.0


@dataclass
class BucketStat:
    start: datetime
    end: datetime
    duration_seconds: float
    requests: int
    requests_per_minute: float
    error_rate_5xx: float
    client_error_rate_4xx: float
    p95_ms: float | None


@dataclass
class EndpointStat:
    route_id: uuid.UUID
    template: str
    method: str
    requests: int
    error_rate_5xx: float
    client_error_rate_4xx: float
    p95_ms: float | None


@dataclass
class MetricsResult:
    service_id: uuid.UUID
    range: MetricsRange
    route_id: uuid.UUID | None
    as_of: datetime
    window_start: datetime
    window_end: datetime
    total_requests: int
    requests_per_minute: float
    error_rate_5xx: float
    client_error_rate_4xx: float
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    last_event_at: datetime | None
    sample_count: int
    status: str  # OK | NO_DATA
    timeseries: list[BucketStat] = field(default_factory=list)
    top_endpoints: list[EndpointStat] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)

    @classmethod
    def from_json(cls, raw: str | bytes) -> MetricsResult:
        d = json.loads(raw)
        dt = lambda v: datetime.fromisoformat(v) if v else None  # noqa: E731
        return cls(
            service_id=uuid.UUID(d["service_id"]),
            range=MetricsRange(d["range"]),
            route_id=uuid.UUID(d["route_id"]) if d["route_id"] else None,
            as_of=dt(d["as_of"]),  # type: ignore[arg-type]
            window_start=dt(d["window_start"]),  # type: ignore[arg-type]
            window_end=dt(d["window_end"]),  # type: ignore[arg-type]
            total_requests=d["total_requests"],
            requests_per_minute=d["requests_per_minute"],
            error_rate_5xx=d["error_rate_5xx"],
            client_error_rate_4xx=d["client_error_rate_4xx"],
            p50_ms=d["p50_ms"],
            p95_ms=d["p95_ms"],
            p99_ms=d["p99_ms"],
            last_event_at=dt(d["last_event_at"]),
            sample_count=d["sample_count"],
            status=d["status"],
            timeseries=[BucketStat(**{**b, "start": dt(b["start"]), "end": dt(b["end"])}) for b in d["timeseries"]],
            top_endpoints=[EndpointStat(**{**e, "route_id": uuid.UUID(e["route_id"])}) for e in d["top_endpoints"]],
        )


@dataclass
class ServiceHealth:
    service_id: uuid.UUID
    status: str  # HEALTHY | DEGRADED | NO_DATA
    sample_count_15m: int
    last_event_at: datetime | None
    active_incidents: int
    telemetry_stale: bool
    evaluation_stale: bool


def bucket_layout(start: datetime, end: datetime, size: timedelta) -> list[tuple[datetime, datetime, datetime]]:
    """(aligned_start, clipped_start, clipped_end) for each bucket overlapping [start, end)."""
    step = int(size.total_seconds())
    first = int(start.timestamp()) // step * step
    out = []
    b = datetime.fromtimestamp(first, UTC)
    while b < end:
        bs, be = max(b, start), min(b + size, end)
        if be > bs:
            out.append((b, bs, be))
        b += size
    return out


class MetricsService:
    def __init__(self, c: Container) -> None:
        self.c = c

    def _cache_key(self, service_id: uuid.UUID, rng: MetricsRange, route_id: uuid.UUID | None, as_of: datetime) -> str:
        # Key includes service and every filter; served only after authorization.
        return f"mc:v1:{service_id}:{rng.value}:{route_id or 'all'}:{int(as_of.timestamp())}"

    async def metrics(
        self, principal: Principal, service_id: uuid.UUID, rng: MetricsRange, route_id: uuid.UUID | None, as_of: datetime
    ) -> MetricsResult:
        authorize(principal, Action.READ_SERVICE, service_id)
        # Existence is rechecked on every call (deleted services/routes stop serving at once,
        # even when a cached aggregate exists).
        async with self.c.db.session() as s:
            if await catalog_repo.get_service(s, service_id) is None:
                raise NotFound()
            if route_id is not None:
                route = await catalog_repo.get_route(s, route_id)
                if route is None or route.service_id != service_id:
                    raise NotFound()
        key = self._cache_key(service_id, rng, route_id, as_of)
        try:
            cached = await self.c.redis.get(key)
        except (RedisError, OSError):
            cached = None
        if cached:
            return MetricsResult.from_json(cached)
        result = await self._compute(service_id, rng, route_id, as_of)
        try:
            await self.c.redis.set(key, result.to_json(), ex=CACHE_TTL_S)
        except (RedisError, OSError):
            pass
        return result

    async def _compute(self, service_id: uuid.UUID, rng: MetricsRange, route_id: uuid.UUID | None, as_of: datetime) -> MetricsResult:
        length, size = RANGE_SPEC[rng]
        async with self.c.db.transaction(statement_timeout_ms=METRICS_STATEMENT_TIMEOUT_MS) as s:
            retention = timedelta(days=await repo.retention_days(s))
            start = max(as_of - length, as_of - retention)
            end = as_of
            summ = await repo.summary(s, service_id, start, end, route_id)
            rows = await repo.series(s, service_id, start, end, route_id, size)
            top = [] if route_id else await repo.endpoints(s, service_id, start, end, limit=TOP_ENDPOINTS, offset=0)
        minutes = (end - start).total_seconds() / 60
        total = int(summ.total)
        pct = summ.pct or [None, None, None]
        by_bucket = {r.b: r for r in rows}
        series = []
        for aligned, bs, be in bucket_layout(start, end, size):
            r = by_bucket.get(aligned)
            n = int(r.total) if r else 0
            dur = (be - bs).total_seconds()
            series.append(
                BucketStat(
                    start=bs,
                    end=be,
                    duration_seconds=dur,
                    requests=n,
                    requests_per_minute=n / (dur / 60) if dur else 0.0,
                    error_rate_5xx=_rate(int(r.e5), n) if r else 0.0,
                    client_error_rate_4xx=_rate(int(r.e4), n) if r else 0.0,
                    p95_ms=float(r.p95) if r and r.p95 is not None else None,
                )
            )
        return MetricsResult(
            service_id=service_id,
            range=rng,
            route_id=route_id,
            as_of=as_of,
            window_start=start,
            window_end=end,
            total_requests=total,
            requests_per_minute=total / minutes if minutes else 0.0,
            error_rate_5xx=_rate(int(summ.e5), total),
            client_error_rate_4xx=_rate(int(summ.e4), total),
            p50_ms=pct[0] if total else None,
            p95_ms=pct[1] if total else None,
            p99_ms=pct[2] if total else None,
            last_event_at=summ.last_event_at,
            sample_count=total,
            status="OK" if total else "NO_DATA",
            timeseries=series,
            top_endpoints=[_endpoint(r) for r in top],
        )

    async def endpoints(
        self, principal: Principal, service_id: uuid.UUID, rng: MetricsRange, as_of: datetime, *, offset: int, limit: int
    ) -> list[EndpointStat]:
        authorize(principal, Action.READ_SERVICE, service_id)
        length, _ = RANGE_SPEC[rng]
        async with self.c.db.transaction(statement_timeout_ms=METRICS_STATEMENT_TIMEOUT_MS) as s:
            if await catalog_repo.get_service(s, service_id) is None:
                raise NotFound()
            retention = timedelta(days=await repo.retention_days(s))
            rows = await repo.endpoints(s, service_id, max(as_of - length, as_of - retention), as_of, limit=limit, offset=offset)
        return [_endpoint(r) for r in rows]

    async def health(self, service_ids: list[uuid.UUID], as_of: datetime) -> dict[uuid.UUID, ServiceHealth]:
        """Batch health for already-authorized service ids (called from a DataLoader)."""
        if not service_ids:
            return {}
        async with self.c.db.transaction(statement_timeout_ms=METRICS_STATEMENT_TIMEOUT_MS) as s:
            retention = timedelta(days=await repo.retention_days(s))
            rows = await repo.health_rows(s, service_ids, as_of, as_of - HEALTH_WINDOW, as_of - retention)
            heartbeat, paused = await repo.latest_heartbeat(s)
        evaluation_stale = paused or heartbeat is None or as_of - heartbeat > WORKER_STALE_AFTER
        out: dict[uuid.UUID, ServiceHealth] = {}
        for r in rows:
            recent, active = int(r.recent), int(r.active)
            status = "DEGRADED" if active else ("NO_DATA" if recent < HEALTH_MIN_SAMPLES else "HEALTHY")
            telemetry_stale = r.last_event_at is None or as_of - r.last_event_at > TELEMETRY_STALE_AFTER
            out[r.id] = ServiceHealth(r.id, status, recent, r.last_event_at, active, telemetry_stale, evaluation_stale)
        return out


def _endpoint(r: Any) -> EndpointStat:
    n = int(r.total)
    return EndpointStat(
        route_id=r.route_id,
        template=r.template,
        method=r.method,
        requests=n,
        error_rate_5xx=_rate(int(r.e5), n),
        client_error_rate_4xx=_rate(int(r.e4), n),
        p95_ms=float(r.p95) if r.p95 is not None else None,
    )
