"""Alert rule evaluation.

For scheduled instant ``slot`` the window is ``[slot - 60s - window, slot - 60s)``. Each
rule is evaluated in its own transaction that:

1. locks the rule row (``FOR UPDATE``) — concurrent workers serialize per rule;
2. skips if ``last_slot >= slot`` (already counted by another worker);
3. computes the metric from raw events, updates streaks, inserts the unique
   ``(rule_id, rule_version, scheduled_at)`` evaluation row, applies any incident
   transition and writes its outbox record — all committed together.

After downtime only the latest closed slot is evaluated; the gap resets streaks so
missed intervals are never replayed into consecutive observations.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert

from pulseops.container import Container
from pulseops.db import classify_db_error
from pulseops.logging import log_event
from pulseops.models import AlertRule, OutboxEvent, RuleEvaluation, Service
from pulseops.services import incident_state as st

SLOT_SECONDS = 60
EVALUATION_LAG = timedelta(seconds=60)
EVAL_STATEMENT_TIMEOUT_MS = 2000
OUTBOX_BACKLOG_CAP = 10_000
EVAL_CONCURRENCY = 4

_METRIC_SQL = text(
    """
    SELECT count(*) AS n,
           count(*) FILTER (WHERE status_code >= 500) AS e5,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95
    FROM request_events
    WHERE service_id = :service_id AND occurred_at >= :start AND occurred_at < :end
    """
)


def slot_for(now: datetime) -> datetime:
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % SLOT_SECONDS, UTC)


@dataclass
class SlotResult:
    evaluated: int = 0
    skipped: int = 0
    failed: int = 0
    opened: int = 0
    resolved: int = 0
    paused: bool = False


def metric_value(metric: str, window_minutes: int, n: int, e5: int, p95: float | None) -> float | None:
    if n == 0:
        return None
    if metric == "ERROR_RATE_5XX":
        return e5 / n
    if metric == "P95_LATENCY_MS":
        return float(p95) if p95 is not None else None
    return n / window_minutes  # REQUEST_RATE: requests per minute


def breached(comparator: str, value: float, threshold: float) -> bool:
    return value > threshold if comparator == "GT" else value >= threshold


async def outbox_backlog(c: Container) -> int:
    async with c.db.session() as s:
        return int((await s.execute(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.sent_at.is_(None)))).scalar_one())


async def due_rule_ids(c: Container) -> list[uuid.UUID]:
    async with c.db.session() as s:
        rows = await s.execute(
            select(AlertRule.id)
            .join(Service, Service.id == AlertRule.service_id)
            .where(AlertRule.enabled.is_(True), AlertRule.deleted_at.is_(None), Service.deleted_at.is_(None))
        )
        return list(rows.scalars())


async def evaluate_rule(c: Container, rule_id: uuid.UUID, slot: datetime, worker_id: str, result: SlotResult) -> None:
    now = c.now()
    try:
        async with c.db.transaction(statement_timeout_ms=EVAL_STATEMENT_TIMEOUT_MS) as s:
            rule = await st.lock_rule(s, rule_id)
            if rule is None or not rule.enabled or rule.deleted_at is not None:
                result.skipped += 1
                return
            service_deleted_at = (await s.execute(select(Service.deleted_at).where(Service.id == rule.service_id))).scalar_one_or_none()
            if service_deleted_at is not None:
                result.skipped += 1
                return
            if rule.last_slot is not None and rule.last_slot >= slot:
                result.skipped += 1  # another worker already counted this slot
                return
            end = slot - EVALUATION_LAG
            start = end - timedelta(minutes=rule.window_minutes)
            row = (await s.execute(_METRIC_SQL, {"service_id": rule.service_id, "start": start, "end": end})).one()
            n = int(row.n)
            value = metric_value(rule.metric, rule.window_minutes, n, int(row.e5), row.p95)
            if n < rule.min_samples or value is None:
                status = "INSUFFICIENT_DATA"
            else:
                status = "BREACH" if breached(rule.comparator, value, rule.threshold) else "OK"
            active = await st.lock_active_incident(s, rule.id)
            upd = st.next_streaks(
                breach=rule.breach_streak,
                ok=rule.ok_streak,
                last_slot=rule.last_slot,
                slot=slot,
                slot_seconds=SLOT_SECONDS,
                status=status,
                has_active=active is not None,
            )
            ins = await s.execute(
                insert(RuleEvaluation)
                .values(
                    id=uuid.uuid4(),
                    service_id=rule.service_id,
                    rule_id=rule.id,
                    rule_version=rule.version,
                    scheduled_at=slot,
                    window_start=start,
                    window_end=end,
                    status=status,
                    value=value,
                    samples=n,
                    evaluated_at=now,
                    worker_id=worker_id,
                )
                .on_conflict_do_nothing(constraint="uq_rule_eval_slot")
                .returning(RuleEvaluation.id)
            )
            if ins.first() is None:
                result.skipped += 1  # unique slot already recorded: never count it twice
                return
            if upd.open_incident:
                await st.open_incident(s, rule, now, value=value, slot=slot)
                result.opened += 1
            elif upd.resolve_incident and active is not None:
                st.resolve(s, active, now, reason="AUTO_RECOVERED", actor=None, slot=slot)
                result.resolved += 1
            rule.breach_streak, rule.ok_streak = upd.breach, upd.ok
            rule.last_slot, rule.last_status, rule.last_value, rule.last_samples = slot, status, value, n
        result.evaluated += 1
    except Exception as exc:
        result.failed += 1
        log_event("worker.evaluation_failed", rule_id=rule_id, error_class=type(exc).__name__)
        if classify_db_error(exc) is None:
            await record_unknown(c, rule_id, slot, worker_id)


async def record_unknown(c: Container, rule_id: uuid.UUID, slot: datetime, worker_id: str) -> None:
    """A failed evaluation reports UNKNOWN and resets streaks; incidents stay as they are."""
    try:
        async with c.db.transaction() as s:
            rule = await st.lock_rule(s, rule_id)
            if rule is None or (rule.last_slot is not None and rule.last_slot >= slot):
                return
            end = slot - EVALUATION_LAG
            await s.execute(
                insert(RuleEvaluation)
                .values(
                    id=uuid.uuid4(),
                    service_id=rule.service_id,
                    rule_id=rule.id,
                    rule_version=rule.version,
                    scheduled_at=slot,
                    window_start=end - timedelta(minutes=rule.window_minutes),
                    window_end=end,
                    status="UNKNOWN",
                    value=None,
                    samples=0,
                    evaluated_at=c.now(),
                    worker_id=worker_id,
                )
                .on_conflict_do_nothing(constraint="uq_rule_eval_slot")
            )
            rule.breach_streak, rule.ok_streak = 0, 0
            rule.last_slot, rule.last_status, rule.last_value, rule.last_samples = slot, "UNKNOWN", None, None
    except Exception as exc:
        # If even this fails, the next slot sees a gap and resets streaks.
        log_event("worker.record_unknown_failed", rule_id=rule_id, error_class=type(exc).__name__)


async def evaluate_slot(c: Container, slot: datetime, worker_id: str) -> SlotResult:
    result = SlotResult()
    if await outbox_backlog(c) >= OUTBOX_BACKLOG_CAP:
        # Never discard committed transitions: pause evaluations until dispatch recovers.
        result.paused = True
        log_event("worker.evaluations_paused", code="outbox_backlog", slot=slot.isoformat())
        return result
    sem = asyncio.Semaphore(EVAL_CONCURRENCY)

    async def run(rule_id: uuid.UUID) -> None:
        async with sem:
            await evaluate_rule(c, rule_id, slot, worker_id, result)

    await asyncio.gather(*(run(r) for r in await due_rule_ids(c)))
    log_event("worker.slot_evaluated", slot=slot.isoformat(), count=result.evaluated, worker_id=worker_id)
    return result
