"""Reliable background processing: evaluator, incident state machine, outbox, jobs, retention."""

from __future__ import annotations

import asyncio
import json
import random
import string
import uuid
from datetime import timedelta

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text

from pulseops.config import Settings
from pulseops.container import Container, build_container, utcnow
from pulseops.notifications import CHANNEL
from pulseops.policy import Principal
from pulseops.services.alerts import AlertService, Comparator, Metric, RuleInput
from pulseops.services.catalog import CatalogService
from pulseops.services.ingest import event_digest
from pulseops.workers import dispatcher, evaluator, maintenance
from pulseops.workers.main import Worker

pytestmark = pytest.mark.integration
M = timedelta(minutes=1)


async def setup_service(c: Container) -> tuple[uuid.UUID, uuid.UUID]:
    name = "svc-" + "".join(random.choices(string.ascii_lowercase, k=10))  # letters only: digits could look like an identifier
    svc = await CatalogService(c).create_service(Principal.cli(), name, "synthetic-test")
    route = await CatalogService(c).register_route(Principal.cli(), svc.id, "/work/{id}")
    return svc.id, route.id


async def add_events(c: Container, service_id: uuid.UUID, route_id: uuid.UUID, at, statuses: list[int]) -> None:  # type: ignore[no-untyped-def]
    async with c.db.transaction() as s:
        for st in statuses:
            eid = uuid.uuid4()
            await s.execute(
                text("INSERT INTO request_events VALUES (:s, :e, :r, :t, :t, 'GET', :st, 10, :g)"),
                {"s": service_id, "e": eid, "r": route_id, "t": at, "st": st, "g": event_digest(eid, at, "GET", route_id, st, 10.0)},
            )


async def make_rule(c: Container, service_id: uuid.UUID, min_samples: int = 5) -> uuid.UUID:
    rule = await AlertService(c).create_rule(
        Principal.cli(), service_id, RuleInput("5xx", Metric.ERROR_RATE_5XX, Comparator.GT, 0.1, 5, min_samples)
    )
    return rule.id


async def q1(c: Container, sql: str, **params: object) -> object:
    async with c.db.session() as s:
        return (await s.execute(text(sql), params)).scalar_one()


async def incidents(c: Container, rule_id: uuid.UUID) -> list[tuple[str, int, str | None]]:
    async with c.db.session() as s:
        rows = await s.execute(
            text("SELECT status, version, resolve_reason FROM incidents WHERE rule_id = :r ORDER BY opened_at"), {"r": rule_id}
        )
        return [tuple(r) for r in rows]  # type: ignore[misc]


@pytest.fixture
def slot() -> object:
    return evaluator.slot_for(utcnow())


async def test_two_breaches_open_two_oks_resolve(container: Container, slot) -> None:  # type: ignore[no-untyped-def]
    sid, rid = await setup_service(container)
    rule = await make_rule(container, sid)
    await add_events(container, sid, rid, slot - 3 * M, [500] * 4 + [200] * 6)  # 40% errors in both windows
    r1 = await evaluator.evaluate_slot(container, slot, "w1")
    assert r1.evaluated == 1 and r1.opened == 0
    r2 = await evaluator.evaluate_slot(container, slot + M, "w1")
    assert r2.opened == 1
    assert await incidents(container, rule) == [("OPEN", 1, None)]
    assert await q1(container, "SELECT count(*) FROM outbox_events") == 1
    # Later windows contain only healthy traffic.
    await add_events(container, sid, rid, slot + 7 * M, [200] * 10)
    await evaluator.evaluate_slot(container, slot + 10 * M, "w1")  # gap since last slot -> streak restarts at 1
    assert (await incidents(container, rule))[0][0] == "OPEN"
    r4 = await evaluator.evaluate_slot(container, slot + 11 * M, "w1")
    assert r4.resolved == 1
    assert await incidents(container, rule) == [("RESOLVED", 2, "AUTO_RECOVERED")]
    kinds = await q1(container, "SELECT string_agg(kind, ',' ORDER BY version) FROM incident_events")
    assert kinds == "OPENED,RESOLVED"


async def test_insufficient_data_resets_and_keeps_incident(container: Container, slot) -> None:  # type: ignore[no-untyped-def]
    sid, rid = await setup_service(container)
    rule = await make_rule(container, sid, min_samples=20)
    await add_events(container, sid, rid, slot - 3 * M, [500] * 25)
    await evaluator.evaluate_slot(container, slot, "w1")
    await evaluator.evaluate_slot(container, slot + M, "w1")
    assert (await incidents(container, rule))[0][0] == "OPEN"
    # The next windows have too few samples: UNKNOWN/INSUFFICIENT, streaks reset, incident stays active.
    await evaluator.evaluate_slot(container, slot + 20 * M, "w1")
    row = await q1(container, "SELECT last_status || ':' || breach_streak || ':' || ok_streak FROM alert_rules WHERE id = :r", r=rule)
    assert row == "INSUFFICIENT_DATA:0:0"
    assert (await incidents(container, rule))[0][0] == "OPEN"


async def test_concurrent_evaluators_count_slot_once(container: Container, migrated: Settings, slot) -> None:  # type: ignore[no-untyped-def]
    sid, rid = await setup_service(container)
    rule = await make_rule(container, sid)
    await add_events(container, sid, rid, slot - 3 * M, [500] * 10)
    async with container.db.transaction() as s:  # one breach already counted in the previous slot
        await s.execute(text("UPDATE alert_rules SET breach_streak = 1, last_slot = :p WHERE id = :r"), {"p": slot - M, "r": rule})
    others = [build_container(migrated, application_name=f"pulseops-test-w{i}") for i in range(3)]
    try:
        results = await asyncio.gather(*(evaluator.evaluate_slot(c, slot, f"w{i}") for i, c in enumerate([container, *others])))
    finally:
        for c in others:
            await c.close()
    assert sum(r.evaluated for r in results) == 1
    assert sum(r.opened for r in results) == 1
    assert await q1(container, "SELECT count(*) FROM rule_evaluations WHERE rule_id = :r AND scheduled_at = :s", r=rule, s=slot) == 1
    assert await incidents(container, rule) == [("OPEN", 1, None)]
    assert await q1(container, "SELECT count(*) FROM incident_events") == 1
    assert await q1(container, "SELECT count(*) FROM outbox_events") == 1


async def test_restart_around_slot_no_duplicate_and_no_replay(container: Container, migrated: Settings, slot) -> None:  # type: ignore[no-untyped-def]
    sid, rid = await setup_service(container)
    rule = await make_rule(container, sid)
    await add_events(container, sid, rid, slot - 3 * M, [500] * 10)
    await evaluator.evaluate_slot(container, slot, "worker-before-restart")
    restarted = build_container(migrated, application_name="pulseops-test-restarted")
    try:
        again = await evaluator.evaluate_slot(restarted, slot, "worker-after-restart")
    finally:
        await restarted.close()
    assert again.evaluated == 0 and again.skipped == 1
    # Downtime: the next evaluation is several slots later; streak restarts instead of replaying.
    await add_events(container, sid, rid, slot + 2 * M, [500] * 10)
    await evaluator.evaluate_slot(container, slot + 5 * M, "w")
    assert await q1(container, "SELECT breach_streak FROM alert_rules WHERE id = :r", r=rule) == 1
    assert await incidents(container, rule) == []


async def test_failed_evaluation_reports_unknown(container: Container, slot, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    sid, rid = await setup_service(container)
    rule = await make_rule(container, sid)
    await add_events(container, sid, rid, slot - 3 * M, [500] * 10)
    await evaluator.evaluate_slot(container, slot, "w")

    def boom(*_: object) -> float:
        raise ValueError("simulated evaluation failure")

    monkeypatch.setattr(evaluator, "metric_value", boom)
    r = await evaluator.evaluate_slot(container, slot + M, "w")
    assert r.failed == 1
    row = await q1(container, "SELECT last_status || ':' || breach_streak FROM alert_rules WHERE id = :r", r=rule)
    assert row == "UNKNOWN:0"
    assert await q1(container, "SELECT status FROM rule_evaluations WHERE scheduled_at = :s", s=slot + M) == "UNKNOWN"
    assert await incidents(container, rule) == []


async def test_ack_manual_resolve_edit_disable_delete(container: Container, slot) -> None:  # type: ignore[no-untyped-def]
    alerts = AlertService(container)
    sid, rid = await setup_service(container)
    rule = await make_rule(container, sid)
    await add_events(container, sid, rid, slot - 3 * M, [500] * 10)
    for i in range(2):
        await evaluator.evaluate_slot(container, slot + i * M, "w")
    inc_id = await q1(container, "SELECT id FROM incidents WHERE rule_id = :r", r=rule)
    acked = await alerts.acknowledge(Principal.cli(), inc_id)  # type: ignore[arg-type]
    assert acked.status == "ACKNOWLEDGED"  # acknowledge does not resolve
    await evaluator.evaluate_slot(container, slot + 2 * M, "w")
    assert len(await incidents(container, rule)) == 1  # no duplicate incident while acknowledged
    await add_events(container, sid, rid, slot + M, [500] * 10)  # keeps later windows breaching
    resolved = await alerts.resolve(Principal.cli(), inc_id)  # type: ignore[arg-type]
    assert resolved.status == "RESOLVED" and resolved.version == 3
    assert await q1(container, "SELECT breach_streak FROM alert_rules WHERE id = :r", r=rule) == 0
    # Continued breach reopens only after two fresh eligible evaluations.
    await evaluator.evaluate_slot(container, slot + 3 * M, "w")
    assert [i[0] for i in await incidents(container, rule)] == ["RESOLVED"]
    await evaluator.evaluate_slot(container, slot + 4 * M, "w")
    assert [i[0] for i in await incidents(container, rule)] == ["RESOLVED", "OPEN"]
    # Editing resets streaks and bumps the version; disabling closes with an explicit reason.
    edited = await alerts.update_rule(
        Principal.cli(), rule, RuleInput("5xx", Metric.ERROR_RATE_5XX, Comparator.GTE, 0.2, 5, 5, enabled=True)
    )
    assert edited.version == 2 and edited.breach_streak == 0
    await alerts.update_rule(Principal.cli(), rule, RuleInput("5xx", Metric.ERROR_RATE_5XX, Comparator.GTE, 0.2, 5, 5, enabled=False))
    assert (await incidents(container, rule))[-1] == ("RESOLVED", 2, "RULE_DISABLED")
    rule2 = await make_rule(container, sid)
    await add_events(container, sid, rid, slot + 10 * M, [500] * 10)
    for i in range(2):
        await evaluator.evaluate_slot(container, slot + (13 + i) * M, "w")
    await alerts.delete_rule(Principal.cli(), rule2)
    assert (await incidents(container, rule2))[-1] == ("RESOLVED", 2, "RULE_DELETED")
    # Every transition is recorded once per version.
    dupes = await q1(
        container, "SELECT count(*) FROM (SELECT incident_id, version FROM incident_events GROUP BY 1, 2 HAVING count(*) > 1) d"
    )
    assert dupes == 0


async def test_outbox_publish_payload_and_redis_outage_recovery(container: Container, slot) -> None:  # type: ignore[no-untyped-def]
    sid, rid = await setup_service(container)
    rule = await make_rule(container, sid)
    await add_events(container, sid, rid, slot - 3 * M, [500] * 10)

    async def redis_down(_: str) -> int:
        raise RedisConnectionError("simulated outage")

    for i in range(2):
        await evaluator.evaluate_slot(container, slot + i * M, "w")
    # Transition committed while Redis is down: outbox keeps it for retry with backoff.
    failed = await dispatcher.dispatch_once(container, publish=redis_down)
    assert (failed.sent, failed.failed) == (0, 1)
    async with container.db.session() as s:
        row = (await s.execute(text("SELECT attempts, next_attempt_at, sent_at FROM outbox_events"))).one()
    assert row.attempts == 1 and row.sent_at is None
    assert timedelta(0) < row.next_attempt_at - utcnow() <= timedelta(seconds=60)
    assert await incidents(container, rule) == [("OPEN", 1, None)]
    # Redis is back; once the backoff elapses the dispatcher delivers.
    pubsub = container.redis.pubsub()
    await pubsub.subscribe(CHANNEL)
    await pubsub.get_message(timeout=1)
    now = utcnow()
    container.clock = lambda: now + timedelta(seconds=61)
    ok = await dispatcher.dispatch_once(container)
    assert ok.sent == 1
    msg = None
    for _ in range(20):
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if msg:
            break
    await pubsub.aclose()
    assert msg is not None
    payload = json.loads(msg["data"])
    assert set(payload) == {"serviceId", "incidentId", "eventId", "version"} and payload["version"] == 1
    assert await q1(container, "SELECT count(*) FROM outbox_events WHERE sent_at IS NULL") == 0


async def test_crash_after_publish_duplicates_notification_not_transition(container: Container, slot) -> None:  # type: ignore[no-untyped-def]
    sid, rid = await setup_service(container)
    rule = await make_rule(container, sid)
    await add_events(container, sid, rid, slot - 3 * M, [500] * 10)
    for i in range(2):
        await evaluator.evaluate_slot(container, slot + i * M, "w")
    published: list[str] = []

    async def capture(data: str) -> int:
        published.append(data)
        return 1

    async def crash(_: object) -> None:
        raise RuntimeError("worker crashed before marking sent")

    with pytest.raises(RuntimeError):
        await dispatcher.dispatch_once(container, publish=capture, after_publish=crash)
    assert await q1(container, "SELECT count(*) FROM outbox_events WHERE sent_at IS NULL") == 1
    await dispatcher.dispatch_once(container, publish=capture)
    assert len(published) == 2 and published[0] == published[1]  # duplicate notification (at-least-once)
    assert await q1(container, "SELECT count(*) FROM incident_events") == 1  # but one business transition
    assert await incidents(container, rule) == [("OPEN", 1, None)]


async def test_backlog_cap_pauses_evaluation(container: Container, slot) -> None:  # type: ignore[no-untyped-def]
    sid, _ = await setup_service(container)
    await make_rule(container, sid)
    async with container.db.transaction() as s:
        await s.execute(
            text(
                "INSERT INTO outbox_events (event_id, service_id, incident_id, version, created_at, attempts, next_attempt_at)"
                " SELECT gen_random_uuid(), :s, gen_random_uuid(), 1, now(), 0, now() FROM generate_series(1, 10000)"
            ),
            {"s": sid},
        )
    r = await evaluator.evaluate_slot(container, slot, "w")
    assert r.paused and r.evaluated == 0
    assert await q1(container, "SELECT count(*) FROM rule_evaluations") == 0
    assert await q1(container, "SELECT count(*) FROM outbox_events") == 10000  # nothing discarded


async def test_deletion_job_retry_and_purge(container: Container, secrets_dir, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    sid, rid = await setup_service(container)
    await make_rule(container, sid)
    await add_events(container, sid, rid, utcnow() - M, [200] * 7)
    async with container.db.transaction() as s:
        await s.execute(text("UPDATE ingest_quota SET used = 7"))
    await CatalogService(container).delete_service(Principal.cli(), sid)
    ledger = maintenance.DeletionLedger(str(secrets_dir / f"ledger-{uuid.uuid4().hex[:6]}"))
    original = ledger.append

    def flaky(*_: object) -> None:
        raise OSError("ledger volume unavailable")

    monkeypatch.setattr(ledger, "append", flaky)
    assert await maintenance.process_deletion_job(container, ledger)
    assert await q1(container, "SELECT status FROM deletion_jobs") == "FAILED"
    assert await q1(container, "SELECT count(*) FROM request_events") == 7
    monkeypatch.setattr(ledger, "append", original)
    now = utcnow()
    container.clock = lambda: now + timedelta(minutes=5)
    assert await maintenance.process_deletion_job(container, ledger)
    assert await q1(container, "SELECT status FROM deletion_jobs") == "DONE"
    for table in ("request_events", "routes", "alert_rules", "ingest_tokens", "services"):
        assert await q1(container, f"SELECT count(*) FROM {table}") == 0, table
    assert await q1(container, "SELECT used FROM ingest_quota") == 0
    assert [e["serviceId"] for e in ledger.entries()] == [str(sid)]
    assert await q1(container, "SELECT count(*) FROM audit_events WHERE action = 'SERVICE_DELETE' AND target_id = :s", s=sid) == 1


async def test_retention_purges(container: Container) -> None:
    sid, rid = await setup_service(container)
    now = utcnow()
    await add_events(container, sid, rid, now - timedelta(days=8), [200] * 12)
    await add_events(container, sid, rid, now - timedelta(days=1), [200] * 3)
    async with container.db.transaction() as s:
        await s.execute(text("UPDATE ingest_quota SET used = 15"))
        await s.execute(
            text(
                "INSERT INTO audit_events (id, occurred_at, actor_kind, action, result) VALUES (gen_random_uuid(), now() - interval '31 days', 'SYSTEM', 'OLD', 'SUCCESS')"
            )
        )
    monkey_chunk = maintenance.CHUNK
    maintenance.CHUNK = 5  # exercise chunking
    try:
        assert await maintenance.purge_events(container) == 12
    finally:
        maintenance.CHUNK = monkey_chunk
    assert await q1(container, "SELECT count(*) FROM request_events") == 3
    assert await q1(container, "SELECT used FROM ingest_quota") == 3
    assert await maintenance.purge_audit(container) == 1
    assert await maintenance.reconcile_quota(container) == 3


async def test_worker_runs_and_shuts_down_gracefully(container: Container) -> None:
    worker = Worker(container)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(1.5)
    worker.stop.set()
    await asyncio.wait_for(task, timeout=15)
    assert await q1(container, "SELECT count(*) FROM worker_heartbeats WHERE worker_id = :w", w=worker.worker_id) == 1
