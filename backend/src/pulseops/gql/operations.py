"""Root Query, Mutation and Subscription types. Each resolver authorizes through services."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime

import strawberry
from sqlalchemy import func, select

from pulseops.errors import NotFound
from pulseops.gql.context import parse_id
from pulseops.gql.types import (
    AlertMetric,
    AlertRuleType,
    AuditEventType,
    Comparator,
    Connection,
    Diagnostics,
    EndpointStat,
    IncidentChange,
    IncidentType,
    Info,
    IngestTokenType,
    IssuedIngestToken,
    Me,
    Metrics,
    MetricsRange,
    Role,
    RouteType,
    ServiceGrantType,
    ServiceType,
    SystemStatus,
    UserType,
    incidents_connection,
    make_connection,
    routes_connection,
    rules_connection,
)
from pulseops.models import DeletionJob, OutboxEvent, WorkerHeartbeat
from pulseops.pagination import decode_cursor, encode_cursor, page_size
from pulseops.policy import REAUTH_WINDOW, Action, authorize, can_read_service
from pulseops.policy import Role as PolicyRole
from pulseops.services import metrics as m
from pulseops.services.alerts import Comparator as SvcComparator
from pulseops.services.alerts import Metric as SvcMetric
from pulseops.services.alerts import RuleInput
from pulseops.services.ingest import MAX_RETAINED_EVENTS, quota_used


@strawberry.input
class ServiceInput:
    name: str
    environment: str


@strawberry.input
class ServiceUpdateInput:
    name: str | None = None
    environment: str | None = None


@strawberry.input
class AlertRuleInput:
    name: str
    metric: AlertMetric
    comparator: Comparator
    threshold: float
    window_minutes: int
    min_samples: int = 20
    enabled: bool = True

    def to_rule(self) -> RuleInput:
        return RuleInput(
            name=self.name,
            metric=SvcMetric(self.metric.value),
            comparator=SvcComparator(self.comparator.value),
            threshold=self.threshold,
            window_minutes=self.window_minutes,
            min_samples=self.min_samples,
            enabled=self.enabled,
        )


@strawberry.type
class Query:
    @strawberry.field
    async def me(self, info: Info) -> Me:
        p = info.context.principal
        user = await info.context.auth.current_user(p)
        return Me(
            id=strawberry.ID(str(user.id)),
            email=user.email,
            role=Role(p.role.value),
            service_ids=[strawberry.ID(str(i)) for i in sorted(p.granted_service_ids, key=str)],
            reauthenticated_until=p.reauth_at + REAUTH_WINDOW if p.reauth_at else None,
        )

    @strawberry.field
    async def services(self, info: Info, first: int | None = None, after: str | None = None) -> Connection[ServiceType]:
        limit = page_size(first)
        items, total = await info.context.catalog.list_services(info.context.principal, after=decode_cursor(after, str), limit=limit)
        return make_connection(items, limit, total, ServiceType.from_model, lambda s: encode_cursor(s.name, s.id))

    @strawberry.field
    async def service(self, info: Info, id: strawberry.ID) -> ServiceType:
        return ServiceType.from_model(await info.context.catalog.get_service(info.context.principal, parse_id(id)))

    @strawberry.field
    async def routes(
        self, info: Info, service_id: strawberry.ID, first: int | None = None, after: str | None = None, include_retired: bool = False
    ) -> Connection[RouteType]:
        return await routes_connection(info, parse_id(service_id), first, after, include_retired)

    @strawberry.field
    async def alert_rules(
        self, info: Info, service_id: strawberry.ID, first: int | None = None, after: str | None = None
    ) -> Connection[AlertRuleType]:
        return await rules_connection(info, parse_id(service_id), first, after)

    @strawberry.field
    async def incidents(
        self, info: Info, service_id: strawberry.ID, first: int | None = None, after: str | None = None, active_only: bool = False
    ) -> Connection[IncidentType]:
        return await incidents_connection(info, parse_id(service_id), first, after, active_only)

    @strawberry.field
    async def incident(self, info: Info, id: strawberry.ID) -> IncidentType:
        return IncidentType.from_model(await info.context.alerts.get_incident(info.context.principal, parse_id(id)))

    @strawberry.field
    async def endpoints(
        self, info: Info, service_id: strawberry.ID, range: MetricsRange, first: int | None = None, after: str | None = None
    ) -> Connection[EndpointStat]:
        ctx = info.context
        limit = page_size(first)
        offset = decode_cursor(after, int)
        start = offset[0] if offset else 0
        if not 0 <= start <= 10_000:
            raise NotFound()
        stats = await ctx.metrics.endpoints(
            ctx.principal, parse_id(service_id), m.MetricsRange(range.value), ctx.as_of, offset=start, limit=limit + 1
        )
        nil = uuid.UUID(int=0)
        conn = make_connection(stats, limit, 0, EndpointStat.from_stat, lambda _: "")
        for i, edge in enumerate(conn.edges):
            edge.cursor = encode_cursor(start + i + 1, nil)
        conn.page_info.end_cursor = conn.edges[-1].cursor if conn.edges else None
        conn.total_count = start + len(conn.edges) + (1 if conn.page_info.has_next_page else 0)
        return conn

    @strawberry.field
    async def metrics(self, info: Info, service_id: strawberry.ID, range: MetricsRange, route_id: strawberry.ID | None = None) -> Metrics:
        ctx = info.context
        rid = parse_id(route_id) if route_id else None
        return Metrics.from_result(
            await ctx.metrics.metrics(ctx.principal, parse_id(service_id), m.MetricsRange(range.value), rid, ctx.as_of)
        )

    @strawberry.field
    async def system_status(self, info: Info) -> SystemStatus:
        ctx = info.context
        async with ctx.c.db.session() as s:
            row = (await s.execute(select(func.max(WorkerHeartbeat.heartbeat_at)))).scalar_one_or_none()
            paused = (
                await s.execute(
                    select(func.bool_or(WorkerHeartbeat.evaluations_paused)).where(
                        WorkerHeartbeat.heartbeat_at > ctx.as_of - m.WORKER_STALE_AFTER
                    )
                )
            ).scalar_one_or_none()
        stale = bool(paused) or row is None or ctx.as_of - row > m.WORKER_STALE_AFTER
        return SystemStatus(evaluation_stale=stale, evaluations_paused=bool(paused), last_worker_heartbeat_at=row)

    # ---- ADMIN ----

    @strawberry.field
    async def users(self, info: Info, first: int | None = None, after: str | None = None) -> Connection[UserType]:
        limit = page_size(first)
        items, total = await info.context.admin.list_users(info.context.principal, after=decode_cursor(after, str), limit=limit)
        return make_connection(items, limit, total, UserType.from_model, lambda u: encode_cursor(u.email, u.id))

    @strawberry.field
    async def service_grants(
        self,
        info: Info,
        user_id: strawberry.ID | None = None,
        service_id: strawberry.ID | None = None,
        first: int | None = None,
        after: str | None = None,
    ) -> Connection[ServiceGrantType]:
        limit = page_size(first)
        items, total = await info.context.admin.list_grants(
            info.context.principal,
            user_id=parse_id(user_id) if user_id else None,
            service_id=parse_id(service_id) if service_id else None,
            after=decode_cursor(after, str),
            limit=limit,
        )
        return make_connection(items, limit, total, ServiceGrantType.from_model, lambda g: encode_cursor(str(g.user_id), g.service_id))

    @strawberry.field
    async def audit_events(self, info: Info, first: int | None = None, after: str | None = None) -> Connection[AuditEventType]:
        limit = page_size(first)
        items, total = await info.context.admin.list_audit(info.context.principal, after=decode_cursor(after, datetime), limit=limit)
        return make_connection(items, limit, total, AuditEventType.from_model, lambda a: encode_cursor(a.occurred_at, a.id))

    @strawberry.field
    async def diagnostics(self, info: Info) -> Diagnostics:
        ctx = info.context
        authorize(ctx.principal, Action.VIEW_DIAGNOSTICS)
        db_ok = await ctx.c.db.ping()
        try:
            redis_ok = bool(await ctx.c.redis.ping())
        except Exception:
            redis_ok = False
        async with ctx.c.db.session() as s:
            hb = (await s.execute(select(WorkerHeartbeat).order_by(WorkerHeartbeat.heartbeat_at.desc()).limit(1))).scalar_one_or_none()
            pending, oldest = (
                await s.execute(select(func.count(), func.min(OutboxEvent.created_at)).where(OutboxEvent.sent_at.is_(None)))
            ).one()
            jobs = (await s.execute(select(func.count()).select_from(DeletionJob).where(DeletionJob.status != "DONE"))).scalar_one()
        return Diagnostics(
            database_ok=db_ok,
            redis_ok=redis_ok,
            last_worker_heartbeat_at=hb.heartbeat_at if hb else None,
            evaluations_paused=bool(hb and hb.evaluations_paused),
            paused_reason=hb.paused_reason if hb else None,
            outbox_pending=int(pending),
            outbox_oldest_pending_at=oldest,
            last_dispatch_ok_at=hb.last_dispatch_ok_at if hb else None,
            last_dispatch_error_at=hb.last_dispatch_error_at if hb else None,
            deletion_jobs_pending=int(jobs),
            retained_events=await quota_used(ctx.c),
            retained_event_limit=MAX_RETAINED_EVENTS,
            event_retention_days=await ctx.admin.retention_days(ctx.principal),
        )


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def create_service(self, info: Info, input: ServiceInput) -> ServiceType:
        return ServiceType.from_model(await info.context.catalog.create_service(info.context.principal, input.name, input.environment))

    @strawberry.mutation
    async def update_service(self, info: Info, id: strawberry.ID, input: ServiceUpdateInput) -> ServiceType:
        return ServiceType.from_model(
            await info.context.catalog.update_service(info.context.principal, parse_id(id), input.name, input.environment)
        )

    @strawberry.mutation
    async def delete_service(self, info: Info, id: strawberry.ID) -> bool:
        await info.context.catalog.delete_service(info.context.principal, parse_id(id))
        return True

    @strawberry.mutation
    async def register_route(self, info: Info, service_id: strawberry.ID, template: str) -> RouteType:
        return RouteType.from_model(await info.context.catalog.register_route(info.context.principal, parse_id(service_id), template))

    @strawberry.mutation
    async def retire_route(self, info: Info, id: strawberry.ID) -> RouteType:
        return RouteType.from_model(await info.context.catalog.retire_route(info.context.principal, parse_id(id)))

    @strawberry.mutation
    async def create_ingest_token(self, info: Info, service_id: strawberry.ID) -> IssuedIngestToken:
        issued = await info.context.catalog.create_token(info.context.principal, parse_id(service_id))
        return IssuedIngestToken(token=IngestTokenType.from_model(issued.token), secret=issued.plaintext)

    @strawberry.mutation
    async def rotate_ingest_token(self, info: Info, id: strawberry.ID) -> IssuedIngestToken:
        issued = await info.context.catalog.rotate_token(info.context.principal, parse_id(id))
        return IssuedIngestToken(token=IngestTokenType.from_model(issued.token), secret=issued.plaintext)

    @strawberry.mutation
    async def revoke_ingest_token(self, info: Info, id: strawberry.ID) -> IngestTokenType:
        return IngestTokenType.from_model(await info.context.catalog.revoke_token(info.context.principal, parse_id(id)))

    @strawberry.mutation
    async def create_alert_rule(self, info: Info, service_id: strawberry.ID, input: AlertRuleInput) -> AlertRuleType:
        return AlertRuleType.from_model(
            await info.context.alerts.create_rule(info.context.principal, parse_id(service_id), input.to_rule())
        )

    @strawberry.mutation
    async def update_alert_rule(self, info: Info, id: strawberry.ID, input: AlertRuleInput) -> AlertRuleType:
        return AlertRuleType.from_model(await info.context.alerts.update_rule(info.context.principal, parse_id(id), input.to_rule()))

    @strawberry.mutation
    async def delete_alert_rule(self, info: Info, id: strawberry.ID) -> AlertRuleType:
        return AlertRuleType.from_model(await info.context.alerts.delete_rule(info.context.principal, parse_id(id)))

    @strawberry.mutation
    async def acknowledge_incident(self, info: Info, id: strawberry.ID) -> IncidentType:
        return IncidentType.from_model(await info.context.alerts.acknowledge(info.context.principal, parse_id(id)))

    @strawberry.mutation
    async def resolve_incident(self, info: Info, id: strawberry.ID) -> IncidentType:
        return IncidentType.from_model(await info.context.alerts.resolve(info.context.principal, parse_id(id)))

    # ---- ADMIN (reauthentication required) ----

    @strawberry.mutation
    async def set_user_role(self, info: Info, user_id: strawberry.ID, role: Role) -> UserType:
        return UserType.from_model(await info.context.admin.set_role(info.context.principal, parse_id(user_id), PolicyRole(role.value)))

    @strawberry.mutation
    async def set_user_disabled(self, info: Info, user_id: strawberry.ID, disabled: bool) -> UserType:
        return UserType.from_model(await info.context.admin.set_disabled(info.context.principal, parse_id(user_id), disabled))

    @strawberry.mutation
    async def grant_service_access(self, info: Info, user_id: strawberry.ID, service_id: strawberry.ID) -> ServiceGrantType:
        return ServiceGrantType.from_model(await info.context.admin.grant(info.context.principal, parse_id(user_id), parse_id(service_id)))

    @strawberry.mutation
    async def revoke_service_access(self, info: Info, user_id: strawberry.ID, service_id: strawberry.ID) -> bool:
        return await info.context.admin.revoke_grant(info.context.principal, parse_id(user_id), parse_id(service_id))

    @strawberry.mutation
    async def set_event_retention_days(self, info: Info, days: int) -> int:
        return await info.context.admin.set_retention_days(info.context.principal, days)


@strawberry.type
class Subscription:
    @strawberry.subscription
    async def incident_changed(self, info: Info, service_id: strawberry.ID) -> AsyncGenerator[IncidentChange]:
        """Live incident changes. Requires the WebSocket transport (graphql-transport-ws).

        Before each delivery the principal is reloaded from PostgreSQL and the incident is
        refetched; notifications carry only ids, never incident data.
        """
        ctx = info.context
        sub = ctx.ws
        if sub is None:
            raise NotFound()
        sid = parse_id(service_id)
        authorize(ctx.principal, Action.READ_SERVICE, sid)
        async with sub.listen(sid) as queue:
            while True:
                note = await queue.get()
                await sub.refresh_principal()  # closes the connection if the session was revoked
                if not can_read_service(ctx.principal, sid):
                    raise NotFound()
                try:
                    inc = await ctx.alerts.get_incident(ctx.principal, note.incident_id)
                except NotFound:
                    continue  # deleted/expired meanwhile: nothing to deliver
                if not sub.should_deliver(inc.id, inc.version):
                    continue  # duplicate notification (at-least-once delivery); not a new transition
                yield IncidentChange(event_id=strawberry.ID(str(note.event_id)), version=inc.version, incident=IncidentType.from_model(inc))
