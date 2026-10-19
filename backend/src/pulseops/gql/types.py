"""GraphQL object types.

Types expose only safe fields: password hashes, token digests, signing keys and session
secrets have no GraphQL representation. Nested fields re-authorize through services or
authorization-filtered loaders, so a nested path cannot reach an object a direct query
could not.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Generic, TypeVar

import strawberry

from pulseops.gql.context import GqlContext, parse_id
from pulseops.models import AlertRule, AuditEvent, IncidentEvent, IngestToken, Route, Service, ServiceGrant, User
from pulseops.models import Incident as IncidentModel
from pulseops.pagination import decode_cursor, encode_cursor, page_size
from pulseops.policy import Action, allowed
from pulseops.services import metrics as m

Info = strawberry.Info[GqlContext, None]
T = TypeVar("T")


@strawberry.enum
class Role(Enum):
    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    VIEWER = "VIEWER"


@strawberry.enum
class MetricsRange(Enum):
    LAST_15M = "15m"
    LAST_1H = "1h"
    LAST_24H = "24h"
    LAST_7D = "7d"


@strawberry.enum
class AlertMetric(Enum):
    ERROR_RATE_5XX = "ERROR_RATE_5XX"
    P95_LATENCY_MS = "P95_LATENCY_MS"
    REQUEST_RATE = "REQUEST_RATE"


@strawberry.enum
class Comparator(Enum):
    GT = "GT"
    GTE = "GTE"


@strawberry.type
class PageInfo:
    has_next_page: bool
    end_cursor: str | None


@strawberry.type
class Edge(Generic[T]):  # noqa: UP046 - Strawberry generic types use typing.Generic
    cursor: str
    node: T


@strawberry.type
class Connection(Generic[T]):  # noqa: UP046 - Strawberry generic types use typing.Generic
    edges: list[Edge[T]]
    page_info: PageInfo
    total_count: int


def make_connection(items: list, limit: int, total: int, convert, cursor_of) -> Connection:  # type: ignore[no-untyped-def, type-arg]
    page = items[:limit]
    edges = [Edge(cursor=cursor_of(it), node=convert(it)) for it in page]
    return Connection(
        edges=edges, page_info=PageInfo(has_next_page=len(items) > limit, end_cursor=edges[-1].cursor if edges else None), total_count=total
    )


# ---- accounts ----


@strawberry.type(name="User")
class UserType:
    id: strawberry.ID
    email: str
    role: Role
    disabled: bool
    created_at: datetime

    @classmethod
    def from_model(cls, u: User) -> UserType:
        return cls(
            id=strawberry.ID(str(u.id)), email=u.email, role=Role(u.role), disabled=u.disabled_at is not None, created_at=u.created_at
        )


@strawberry.type
class Me:
    id: strawberry.ID
    email: str
    role: Role
    service_ids: list[strawberry.ID]
    reauthenticated_until: datetime | None


@strawberry.type(name="ServiceGrant")
class ServiceGrantType:
    user_id: strawberry.ID
    service_id: strawberry.ID
    created_at: datetime

    @strawberry.field
    async def service(self, info: Info) -> ServiceType | None:
        svc = await info.context.loaders.service.load(uuid.UUID(self.service_id))
        return ServiceType.from_model(svc) if svc else None

    @classmethod
    def from_model(cls, g: ServiceGrant) -> ServiceGrantType:
        return cls(user_id=strawberry.ID(str(g.user_id)), service_id=strawberry.ID(str(g.service_id)), created_at=g.created_at)


@strawberry.type(name="AuditEvent")
class AuditEventType:
    id: strawberry.ID
    occurred_at: datetime
    actor_kind: str
    actor_id: strawberry.ID | None
    action: str
    target_type: str | None
    target_id: strawberry.ID | None
    result: str
    request_id: strawberry.ID | None

    @classmethod
    def from_model(cls, a: AuditEvent) -> AuditEventType:
        sid = lambda v: strawberry.ID(str(v)) if v else None  # noqa: E731
        return cls(
            id=strawberry.ID(str(a.id)),
            occurred_at=a.occurred_at,
            actor_kind=a.actor_kind,
            actor_id=sid(a.actor_id),
            action=a.action,
            target_type=a.target_type,
            target_id=sid(a.target_id),
            result=a.result,
            request_id=sid(a.request_id),
        )


# ---- catalog ----


@strawberry.type(name="Route")
class RouteType:
    id: strawberry.ID
    service_id: strawberry.ID
    template: str
    created_at: datetime
    retired_at: datetime | None

    @classmethod
    def from_model(cls, r: Route) -> RouteType:
        return cls(
            id=strawberry.ID(str(r.id)),
            service_id=strawberry.ID(str(r.service_id)),
            template=r.template,
            created_at=r.created_at,
            retired_at=r.retired_at,
        )


@strawberry.type(name="IngestToken")
class IngestTokenType:
    """Token metadata only. The secret is returned once by create/rotate and never again."""

    id: strawberry.ID
    service_id: strawberry.ID
    public_id: str
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None

    @classmethod
    def from_model(cls, t: IngestToken) -> IngestTokenType:
        return cls(
            id=strawberry.ID(str(t.id)),
            service_id=strawberry.ID(str(t.service_id)),
            public_id=t.public_id,
            created_at=t.created_at,
            expires_at=t.expires_at,
            revoked_at=t.revoked_at,
        )


@strawberry.type
class IssuedIngestToken:
    token: IngestTokenType
    secret: str


@strawberry.type
class ServiceHealth:
    status: str
    sample_count_15m: int
    last_event_at: datetime | None
    active_incidents: int
    telemetry_stale: bool
    evaluation_stale: bool


@strawberry.type
class MetricsBucket:
    start: datetime
    end: datetime
    duration_seconds: float
    requests: int
    requests_per_minute: float
    error_rate_5xx: float
    client_error_rate_4xx: float
    p95_ms: float | None


@strawberry.type
class EndpointStat:
    route_id: strawberry.ID
    template: str
    method: str
    requests: int
    error_rate_5xx: float
    client_error_rate_4xx: float
    p95_ms: float | None

    @classmethod
    def from_stat(cls, e: m.EndpointStat) -> EndpointStat:
        return cls(
            route_id=strawberry.ID(str(e.route_id)),
            template=e.template,
            method=e.method,
            requests=e.requests,
            error_rate_5xx=e.error_rate_5xx,
            client_error_rate_4xx=e.client_error_rate_4xx,
            p95_ms=e.p95_ms,
        )


@strawberry.type
class Metrics:
    service_id: strawberry.ID
    range: MetricsRange
    route_id: strawberry.ID | None
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
    status: str
    timeseries: list[MetricsBucket]
    top_endpoints: list[EndpointStat]

    @classmethod
    def from_result(cls, r: m.MetricsResult) -> Metrics:
        return cls(
            service_id=strawberry.ID(str(r.service_id)),
            range=MetricsRange(r.range.value),
            route_id=strawberry.ID(str(r.route_id)) if r.route_id else None,
            as_of=r.as_of,
            window_start=r.window_start,
            window_end=r.window_end,
            total_requests=r.total_requests,
            requests_per_minute=r.requests_per_minute,
            error_rate_5xx=r.error_rate_5xx,
            client_error_rate_4xx=r.client_error_rate_4xx,
            p50_ms=r.p50_ms,
            p95_ms=r.p95_ms,
            p99_ms=r.p99_ms,
            last_event_at=r.last_event_at,
            sample_count=r.sample_count,
            status=r.status,
            timeseries=[MetricsBucket(**b.__dict__) for b in r.timeseries],
            top_endpoints=[EndpointStat.from_stat(e) for e in r.top_endpoints],
        )


@strawberry.type(name="Service")
class ServiceType:
    id: strawberry.ID
    name: str
    environment: str
    created_at: datetime

    @classmethod
    def from_model(cls, s: Service) -> ServiceType:
        return cls(id=strawberry.ID(str(s.id)), name=s.name, environment=s.environment, created_at=s.created_at)

    @property
    def _uuid(self) -> uuid.UUID:
        return uuid.UUID(self.id)

    @strawberry.field
    async def health(self, info: Info) -> ServiceHealth | None:
        h = await info.context.loaders.health.load(self._uuid)
        if h is None:
            return None
        return ServiceHealth(
            status=h.status,
            sample_count_15m=h.sample_count_15m,
            last_event_at=h.last_event_at,
            active_incidents=h.active_incidents,
            telemetry_stale=h.telemetry_stale,
            evaluation_stale=h.evaluation_stale,
        )

    @strawberry.field
    def can_manage(self, info: Info) -> bool:
        return allowed(info.context.principal, Action.MANAGE_ROUTES, self._uuid)

    @strawberry.field
    async def routes(
        self, info: Info, first: int | None = None, after: str | None = None, include_retired: bool = False
    ) -> Connection[RouteType]:
        return await routes_connection(info, self._uuid, first, after, include_retired)

    @strawberry.field
    async def alert_rules(self, info: Info, first: int | None = None, after: str | None = None) -> Connection[AlertRuleType]:
        return await rules_connection(info, self._uuid, first, after)

    @strawberry.field
    async def incidents(
        self, info: Info, first: int | None = None, after: str | None = None, active_only: bool = False
    ) -> Connection[IncidentType]:
        return await incidents_connection(info, self._uuid, first, after, active_only)

    @strawberry.field
    async def ingest_tokens(self, info: Info) -> list[IngestTokenType]:
        return [IngestTokenType.from_model(t) for t in await info.context.catalog.list_tokens(info.context.principal, self._uuid)]

    @strawberry.field
    async def metrics(self, info: Info, range: MetricsRange, route_id: strawberry.ID | None = None) -> Metrics:
        ctx = info.context
        rid = parse_id(route_id) if route_id else None
        return Metrics.from_result(await ctx.metrics.metrics(ctx.principal, self._uuid, m.MetricsRange(range.value), rid, ctx.as_of))


async def routes_connection(
    info: Info, service_id: uuid.UUID, first: int | None, after: str | None, include_retired: bool
) -> Connection[RouteType]:
    limit = page_size(first)
    items, total = await info.context.catalog.list_routes(
        info.context.principal, service_id, include_retired=include_retired, after=decode_cursor(after, str), limit=limit
    )
    return make_connection(items, limit, total, RouteType.from_model, lambda r: encode_cursor(r.template, r.id))


async def rules_connection(info: Info, service_id: uuid.UUID, first: int | None, after: str | None) -> Connection[AlertRuleType]:
    limit = page_size(first)
    items, total = await info.context.alerts.list_rules(info.context.principal, service_id, after=decode_cursor(after, str), limit=limit)
    return make_connection(items, limit, total, AlertRuleType.from_model, lambda r: encode_cursor(r.name, r.id))


async def incidents_connection(
    info: Info, service_id: uuid.UUID, first: int | None, after: str | None, active_only: bool
) -> Connection[IncidentType]:
    limit = page_size(first)
    items, total = await info.context.alerts.list_incidents(
        info.context.principal, service_id, active_only=active_only, after=decode_cursor(after, datetime), limit=limit
    )
    return make_connection(items, limit, total, IncidentType.from_model, lambda i: encode_cursor(i.opened_at, i.id))


# ---- alerting ----


@strawberry.type(name="AlertRule")
class AlertRuleType:
    id: strawberry.ID
    service_id: strawberry.ID
    name: str
    metric: AlertMetric
    comparator: Comparator
    threshold: float
    window_minutes: int
    min_samples: int
    enabled: bool
    version: int
    breach_streak: int
    ok_streak: int
    last_status: str | None
    last_value: float | None
    last_samples: int | None
    last_evaluated_slot: datetime | None
    deleted: bool

    @classmethod
    def from_model(cls, r: AlertRule) -> AlertRuleType:
        return cls(
            id=strawberry.ID(str(r.id)),
            service_id=strawberry.ID(str(r.service_id)),
            name=r.name,
            metric=AlertMetric(r.metric),
            comparator=Comparator(r.comparator),
            threshold=r.threshold,
            window_minutes=r.window_minutes,
            min_samples=r.min_samples,
            enabled=r.enabled,
            version=r.version,
            breach_streak=r.breach_streak,
            ok_streak=r.ok_streak,
            last_status=r.last_status,
            last_value=r.last_value,
            last_samples=r.last_samples,
            last_evaluated_slot=r.last_slot,
            deleted=r.deleted_at is not None,
        )


@strawberry.type(name="IncidentTransition")
class IncidentTransitionType:
    version: int
    kind: str
    reason: str | None
    actor_id: strawberry.ID | None
    occurred_at: datetime

    @classmethod
    def from_model(cls, e: IncidentEvent) -> IncidentTransitionType:
        return cls(
            version=e.version,
            kind=e.kind,
            reason=e.reason,
            actor_id=strawberry.ID(str(e.actor_id)) if e.actor_id else None,
            occurred_at=e.occurred_at,
        )


@strawberry.type(name="Incident")
class IncidentType:
    id: strawberry.ID
    service_id: strawberry.ID
    rule_id: strawberry.ID
    status: str
    version: int
    metric: AlertMetric
    threshold: float
    opened_value: float | None
    opened_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    resolve_reason: str | None
    model: strawberry.Private[IncidentModel]

    @classmethod
    def from_model(cls, i: IncidentModel) -> IncidentType:
        return cls(
            id=strawberry.ID(str(i.id)),
            service_id=strawberry.ID(str(i.service_id)),
            rule_id=strawberry.ID(str(i.rule_id)),
            status=i.status,
            version=i.version,
            metric=AlertMetric(i.metric),
            threshold=i.threshold,
            opened_value=i.opened_value,
            opened_at=i.opened_at,
            acknowledged_at=i.acknowledged_at,
            resolved_at=i.resolved_at,
            resolve_reason=i.resolve_reason,
            model=i,
        )

    @strawberry.field
    async def service(self, info: Info) -> ServiceType | None:
        svc = await info.context.loaders.service.load(uuid.UUID(self.service_id))
        return ServiceType.from_model(svc) if svc else None

    @strawberry.field
    async def rule(self, info: Info) -> AlertRuleType | None:
        r = await info.context.loaders.rule.load(uuid.UUID(self.rule_id))
        return AlertRuleType.from_model(r) if r else None

    @strawberry.field
    async def transitions(self, info: Info) -> list[IncidentTransitionType]:
        return [IncidentTransitionType.from_model(e) for e in await info.context.alerts.incident_events(info.context.principal, self.model)]


@strawberry.type
class IncidentChange:
    event_id: strawberry.ID
    version: int
    incident: IncidentType


@strawberry.type
class SystemStatus:
    evaluation_stale: bool
    evaluations_paused: bool
    last_worker_heartbeat_at: datetime | None


@strawberry.type
class Diagnostics:
    database_ok: bool
    redis_ok: bool
    last_worker_heartbeat_at: datetime | None
    evaluations_paused: bool
    paused_reason: str | None
    outbox_pending: int
    outbox_oldest_pending_at: datetime | None
    last_dispatch_ok_at: datetime | None
    last_dispatch_error_at: datetime | None
    deletion_jobs_pending: int
    retained_events: int
    retained_event_limit: int
    event_retention_days: int
