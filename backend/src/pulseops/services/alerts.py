"""Alert rules and incident actions (API side). The worker evaluates rules."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select, text

from pulseops.container import Container
from pulseops.errors import Conflict, NotFound, ValidationFailed
from pulseops.models import AlertRule, Incident, IncidentEvent, Service
from pulseops.policy import Action, Principal, authorize, can_read_service
from pulseops.repositories import catalog as catalog_repo
from pulseops.repositories.audit import actor_fields, insert_audit
from pulseops.services import incident_state as st

MAX_RULES = 20
DEFAULT_MIN_SAMPLES = 20
RESOLVED_RETENTION = timedelta(days=30)


class Metric(StrEnum):
    ERROR_RATE_5XX = "ERROR_RATE_5XX"  # fraction 0..1 of requests with 5xx status
    P95_LATENCY_MS = "P95_LATENCY_MS"  # milliseconds
    REQUEST_RATE = "REQUEST_RATE"  # requests per minute; high-traffic alert, not outage detection


class Comparator(StrEnum):
    GT = "GT"
    GTE = "GTE"


THRESHOLD_RANGE = {Metric.ERROR_RATE_5XX: (0.0, 1.0), Metric.P95_LATENCY_MS: (0.0, 120000.0), Metric.REQUEST_RATE: (0.0, 1_000_000.0)}
WINDOWS = (5, 15, 60)


@dataclass(frozen=True)
class RuleInput:
    name: str
    metric: Metric
    comparator: Comparator
    threshold: float
    window_minutes: int
    min_samples: int = DEFAULT_MIN_SAMPLES
    enabled: bool = True


def rule_violations(r: RuleInput) -> list[dict[str, str]]:
    errs: list[dict[str, str]] = []
    if not r.name or len(r.name) > 80 or not r.name.isprintable() or r.name != r.name.strip():
        errs.append({"field": "name", "code": "invalid"})
    lo, hi = THRESHOLD_RANGE[r.metric]
    if (
        not isinstance(r.threshold, (int, float))
        or isinstance(r.threshold, bool)
        or not math.isfinite(r.threshold)
        or not lo <= r.threshold <= hi
    ):
        errs.append({"field": "threshold", "code": "out_of_range"})
    if r.window_minutes not in WINDOWS:
        errs.append({"field": "windowMinutes", "code": "invalid_value"})
    if not 1 <= r.min_samples <= 100_000:
        errs.append({"field": "minSamples", "code": "out_of_range"})
    return errs


class AlertService:
    def __init__(self, c: Container) -> None:
        self.c = c

    # ---- rules ----

    async def list_rules(
        self, principal: Principal, service_id: uuid.UUID, *, after: tuple[str, uuid.UUID] | None, limit: int
    ) -> tuple[list[AlertRule], int]:
        authorize(principal, Action.READ_SERVICE, service_id)
        base = (
            select(AlertRule)
            .join(Service, Service.id == AlertRule.service_id)
            .where(AlertRule.service_id == service_id, AlertRule.deleted_at.is_(None), Service.deleted_at.is_(None))
        )
        q = base
        if after:
            q = q.where(text("(alert_rules.name, alert_rules.id) > (:a_n, :a_id)").bindparams(a_n=after[0], a_id=after[1]))
        async with self.c.db.session() as s:
            items = list((await s.execute(q.order_by(AlertRule.name, AlertRule.id).limit(limit + 1))).scalars())
            total = int((await s.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
        return items, total

    async def get_rule(self, principal: Principal, rule_id: uuid.UUID) -> AlertRule:
        async with self.c.db.session() as s:
            rule = (
                await s.execute(
                    select(AlertRule)
                    .join(Service, Service.id == AlertRule.service_id)
                    .where(AlertRule.id == rule_id, AlertRule.deleted_at.is_(None), Service.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
        if rule is None or not can_read_service(principal, rule.service_id):
            raise NotFound()
        return rule

    async def rules_by_ids(self, principal: Principal, ids: list[uuid.UUID]) -> dict[uuid.UUID, AlertRule]:
        """Loader helper: returns only rules in services the principal may read (incl. deleted rules for history)."""
        async with self.c.db.session() as s:
            rows = await s.execute(
                select(AlertRule)
                .join(Service, Service.id == AlertRule.service_id)
                .where(AlertRule.id.in_(ids), Service.deleted_at.is_(None))
            )
            return {r.id: r for r in rows.scalars() if can_read_service(principal, r.service_id)}

    async def create_rule(self, principal: Principal, service_id: uuid.UUID, r: RuleInput) -> AlertRule:
        authorize(principal, Action.MANAGE_RULES, service_id)
        errs = rule_violations(r)
        if errs:
            raise ValidationFailed(errs)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            if await catalog_repo.get_service(s, service_id, for_update=True) is None:
                raise NotFound()
            count = (
                await s.execute(
                    select(func.count()).select_from(AlertRule).where(AlertRule.service_id == service_id, AlertRule.deleted_at.is_(None))
                )
            ).scalar_one()
            if count >= MAX_RULES:
                raise Conflict([{"code": "rule_limit"}])
            rule = AlertRule(
                id=uuid.uuid4(),
                service_id=service_id,
                name=r.name,
                metric=r.metric.value,
                comparator=r.comparator.value,
                threshold=float(r.threshold),
                window_minutes=r.window_minutes,
                min_samples=r.min_samples,
                enabled=r.enabled,
                version=1,
                breach_streak=0,
                ok_streak=0,
                created_at=now,
                updated_at=now,
            )
            s.add(rule)
        return rule

    async def update_rule(self, principal: Principal, rule_id: uuid.UUID, r: RuleInput) -> AlertRule:
        """Edits bump the version and reset streaks; disabling closes the active incident."""
        existing = await self.get_rule(principal, rule_id)
        authorize(principal, Action.MANAGE_RULES, existing.service_id)
        errs = rule_violations(r)
        if errs:
            raise ValidationFailed(errs)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            rule = await st.lock_rule(s, rule_id)
            if rule is None or rule.deleted_at is not None:
                raise NotFound()
            rule.name, rule.metric, rule.comparator = r.name, r.metric.value, r.comparator.value
            rule.threshold, rule.window_minutes, rule.min_samples = float(r.threshold), r.window_minutes, r.min_samples
            rule.version += 1
            rule.breach_streak, rule.ok_streak, rule.updated_at = 0, 0, now
            if rule.enabled and not r.enabled:
                inc = await st.lock_active_incident(s, rule.id)
                if inc is not None:
                    st.resolve(s, inc, now, reason="RULE_DISABLED", actor=principal.user_id)
            rule.enabled = r.enabled
        return rule

    async def delete_rule(self, principal: Principal, rule_id: uuid.UUID) -> AlertRule:
        existing = await self.get_rule(principal, rule_id)
        authorize(principal, Action.MANAGE_RULES, existing.service_id)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            rule = await st.lock_rule(s, rule_id)
            if rule is None or rule.deleted_at is not None:
                raise NotFound()
            inc = await st.lock_active_incident(s, rule.id)
            if inc is not None:
                st.resolve(s, inc, now, reason="RULE_DELETED", actor=principal.user_id)
            # Soft delete: evaluations/incidents keep referencing the rule for history.
            rule.deleted_at, rule.enabled, rule.updated_at = now, False, now
        return rule

    # ---- incidents ----

    async def list_incidents(
        self, principal: Principal, service_id: uuid.UUID, *, active_only: bool, after: tuple[datetime, uuid.UUID] | None, limit: int
    ) -> tuple[list[Incident], int]:
        authorize(principal, Action.READ_SERVICE, service_id)
        cutoff = self.c.now() - RESOLVED_RETENTION
        base = (
            select(Incident)
            .join(Service, Service.id == Incident.service_id)
            .where(Incident.service_id == service_id, Service.deleted_at.is_(None))
        )
        base = (
            base.where(Incident.status.in_(st.ACTIVE))
            if active_only
            else base.where((Incident.status.in_(st.ACTIVE)) | (Incident.resolved_at >= cutoff))
        )
        q = base
        if after:
            q = q.where(text("(incidents.opened_at, incidents.id) < (:a_ts, :a_id)").bindparams(a_ts=after[0], a_id=after[1]))
        async with self.c.db.session() as s:
            items = list((await s.execute(q.order_by(Incident.opened_at.desc(), Incident.id.desc()).limit(limit + 1))).scalars())
            total = int((await s.execute(select(func.count()).select_from(base.subquery()))).scalar_one())
        return items, total

    async def get_incident(self, principal: Principal, incident_id: uuid.UUID) -> Incident:
        """Authoritative read from PostgreSQL (used before every subscription delivery too)."""
        async with self.c.db.session() as s:
            inc = (
                await s.execute(
                    select(Incident)
                    .join(Service, Service.id == Incident.service_id)
                    .where(Incident.id == incident_id, Service.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
        if inc is None or not can_read_service(principal, inc.service_id):
            raise NotFound()
        if inc.status == "RESOLVED" and inc.resolved_at is not None and inc.resolved_at < self.c.now() - RESOLVED_RETENTION:
            raise NotFound()
        return inc

    async def incident_events(self, principal: Principal, incident: Incident) -> list[IncidentEvent]:
        authorize(principal, Action.READ_SERVICE, incident.service_id)
        async with self.c.db.session() as s:
            rows = await s.execute(select(IncidentEvent).where(IncidentEvent.incident_id == incident.id).order_by(IncidentEvent.version))
            return list(rows.scalars())

    async def acknowledge(self, principal: Principal, incident_id: uuid.UUID) -> Incident:
        current = await self.get_incident(principal, incident_id)
        authorize(principal, Action.MANAGE_INCIDENTS, current.service_id)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            await st.lock_rule(s, current.rule_id)
            inc = (await s.execute(select(Incident).where(Incident.id == incident_id).with_for_update())).scalar_one()
            if inc.status == "RESOLVED":
                raise Conflict([{"code": "invalid_transition"}])
            if inc.status == "OPEN":
                st.acknowledge(s, inc, now, principal.user_id)
                await insert_audit(
                    s, now=now, action="INCIDENT_ACK", result="SUCCESS", target_type="INCIDENT", target_id=inc.id, **actor_fields(principal)
                )
        return inc

    async def resolve(self, principal: Principal, incident_id: uuid.UUID) -> Incident:
        """Manual resolve; resets streaks so a continued breach needs two fresh evaluations."""
        current = await self.get_incident(principal, incident_id)
        authorize(principal, Action.MANAGE_INCIDENTS, current.service_id)
        now = self.c.now()
        async with self.c.db.transaction() as s:
            rule = await st.lock_rule(s, current.rule_id)
            inc = (await s.execute(select(Incident).where(Incident.id == incident_id).with_for_update())).scalar_one()
            if inc.status == "RESOLVED":
                raise Conflict([{"code": "invalid_transition"}])
            st.resolve(s, inc, now, reason="MANUAL", actor=principal.user_id)
            if rule is not None:
                rule.breach_streak, rule.ok_streak = 0, 0
            await insert_audit(
                s, now=now, action="INCIDENT_RESOLVE", result="SUCCESS", target_type="INCIDENT", target_id=inc.id, **actor_fields(principal)
            )
        return inc
