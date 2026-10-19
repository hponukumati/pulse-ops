"""Metrics correctness over real stored events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from pulseops.container import Container, utcnow
from pulseops.services.ingest import event_digest
from tests.helpers import World, build_world, error_codes, event, gql, ingest, issue_token, reauth

pytestmark = pytest.mark.integration

METRICS = """query($s: ID!, $r: MetricsRange!, $route: ID) { metrics(serviceId: $s, range: $r, routeId: $route) {
  asOf windowStart windowEnd totalRequests requestsPerMinute errorRate5xx clientErrorRate4xx p50Ms p95Ms p99Ms lastEventAt sampleCount status
  timeseries { start end durationSeconds requests } topEndpoints { routeId template method requests p95Ms } } }"""


def percentile_cont(values: list[float], p: float) -> float:
    xs = sorted(values)
    rank = p * (len(xs) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (rank - lo)


@pytest.fixture
async def world(container: Container, client: httpx.AsyncClient) -> World:
    return await build_world(container, client)


@pytest.fixture
async def secret_a(client: httpx.AsyncClient, world: World) -> str:
    return (await issue_token(client, world.alice, world.service_a))[1]


async def insert_raw(c: Container, service_id: str, route_id: str, at: datetime, status: int = 200, duration: float = 5.0) -> None:
    """Direct insert for historical/retention scenarios the ingest window forbids."""
    eid = uuid.uuid4()
    async with c.db.transaction() as s:
        await s.execute(
            text("INSERT INTO request_events VALUES (:s, :e, :r, :t, :t, 'GET', :st, :d, :g)"),
            {
                "s": uuid.UUID(service_id),
                "e": eid,
                "r": uuid.UUID(route_id),
                "t": at,
                "st": status,
                "d": duration,
                "g": event_digest(eid, at, "GET", uuid.UUID(route_id), status, duration),
            },
        )


async def test_exact_summary_metrics(container: Container, client: httpx.AsyncClient, world: World, secret_a: str) -> None:
    durations = [float(d) for d in (3, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79, 83, 89, 97, 101, 997)]
    statuses = [500] * 3 + [404] * 5 + [200] * (len(durations) - 8)
    batch = [
        event(world.route_a, status=st, duration=d, method="POST" if i % 2 else "GET")
        for i, (st, d) in enumerate(zip(statuses, durations, strict=True))
    ]
    assert (await ingest(client, secret_a, batch)).status_code == 200
    m = (await gql(client, world.alice, METRICS, {"s": world.service_a, "r": "LAST_15M"}))["data"]["metrics"]
    n = len(durations)
    assert m["totalRequests"] == m["sampleCount"] == n and m["status"] == "OK"
    assert m["errorRate5xx"] == pytest.approx(3 / n) and m["clientErrorRate4xx"] == pytest.approx(5 / n)
    for key, p in (("p50Ms", 0.5), ("p95Ms", 0.95), ("p99Ms", 0.99)):
        assert m[key] == pytest.approx(percentile_cont(durations, p)), key
    window_min = (datetime.fromisoformat(m["windowEnd"]) - datetime.fromisoformat(m["windowStart"])).total_seconds() / 60
    assert window_min == pytest.approx(15.0)
    assert m["requestsPerMinute"] == pytest.approx(n / 15.0)  # quiet minutes count in the denominator
    assert {e["method"] for e in m["topEndpoints"]} == {"GET", "POST"}
    assert sum(e["requests"] for e in m["topEndpoints"]) == n


async def test_buckets_are_clipped_and_cover_window(client: httpx.AsyncClient, world: World) -> None:
    for rng, minutes, size in (("LAST_15M", 15, 60), ("LAST_1H", 60, 60), ("LAST_24H", 1440, 900), ("LAST_7D", 10080, 3600)):
        m = (await gql(client, world.alice, METRICS, {"s": world.service_a, "r": rng}))["data"]["metrics"]
        series = m["timeseries"]
        assert sum(b["durationSeconds"] for b in series) == pytest.approx(minutes * 60)
        assert series[0]["start"] == m["windowStart"] and series[-1]["end"] == m["windowEnd"]
        assert all(b["durationSeconds"] <= size for b in series)
        assert all(series[i]["end"] == series[i + 1]["start"] for i in range(len(series) - 1))


async def test_empty_range_no_data(client: httpx.AsyncClient, world: World) -> None:
    m = (await gql(client, world.alice, METRICS, {"s": world.service_a, "r": "LAST_1H"}))["data"]["metrics"]
    assert m["status"] == "NO_DATA" and m["totalRequests"] == 0 and m["errorRate5xx"] == 0 and m["requestsPerMinute"] == 0
    assert m["p50Ms"] is None and m["p95Ms"] is None and m["p99Ms"] is None and m["lastEventAt"] is None


async def test_future_and_late_events(container: Container, client: httpx.AsyncClient, world: World, secret_a: str) -> None:
    now = utcnow()
    assert (
        await ingest(
            client, secret_a, [event(world.route_a, at=now + timedelta(minutes=4)), event(world.route_a, at=now - timedelta(hours=23))]
        )
    ).status_code == 200
    q = lambda r: gql(client, world.alice, METRICS, {"s": world.service_a, "r": r})  # noqa: E731
    assert (await q("LAST_1H"))["data"]["metrics"]["totalRequests"] == 0  # future event not yet visible
    assert (await q("LAST_24H"))["data"]["metrics"]["totalRequests"] == 1  # late event updates history
    container.clock = lambda: now + timedelta(minutes=5)
    assert (await q("LAST_1H"))["data"]["metrics"]["totalRequests"] == 1  # becomes visible once its time enters the window


async def test_read_time_retention(container: Container, client: httpx.AsyncClient, world: World) -> None:
    now = utcnow()
    await insert_raw(container, world.service_a, world.route_a, now - timedelta(days=8))
    await insert_raw(container, world.service_a, world.route_a, now - timedelta(days=2))
    await insert_raw(container, world.service_a, world.route_a, now - timedelta(hours=2))
    seven = (await gql(client, world.alice, METRICS, {"s": world.service_a, "r": "LAST_7D"}))["data"]["metrics"]
    assert seven["totalRequests"] == 2  # 8-day-old row hidden even though not yet purged
    await reauth(client, world.admin)
    assert "errors" not in await gql(client, world.admin, "mutation { setEventRetentionDays(days: 1) }")
    await container.redis.flushall()  # drop cached aggregates computed under the old setting
    one = (await gql(client, world.alice, METRICS, {"s": world.service_a, "r": "LAST_7D"}))["data"]["metrics"]
    assert one["totalRequests"] == 1
    assert (datetime.fromisoformat(one["windowEnd"]) - datetime.fromisoformat(one["windowStart"])) == timedelta(days=1)


async def test_route_filter_scoping(client: httpx.AsyncClient, world: World, secret_a: str) -> None:
    await ingest(client, secret_a, [event(world.route_a)])
    own = await gql(client, world.alice, METRICS, {"s": world.service_a, "r": "LAST_15M", "route": world.route_a})
    assert own["data"]["metrics"]["totalRequests"] == 1
    other = await gql(client, world.alice, METRICS, {"s": world.service_a, "r": "LAST_15M", "route": world.route_b})
    assert error_codes(other) == ["NOT_FOUND"]


async def test_cache_reauthorized_per_caller_and_deletion(
    container: Container, client: httpx.AsyncClient, world: World, secret_a: str
) -> None:
    await ingest(client, secret_a, [event(world.route_a) for _ in range(5)])
    first = await gql(client, world.alice, METRICS, {"s": world.service_a, "r": "LAST_1H"})
    assert first["data"]["metrics"]["totalRequests"] == 5
    assert [k async for k in container.redis.scan_iter(match=f"mc:v1:{world.service_a}:*")]  # aggregate is cached
    # Bob asks for the same key moments later: authorization happens before the cache.
    assert error_codes(await gql(client, world.bob, METRICS, {"s": world.service_a, "r": "LAST_1H"})) == ["NOT_FOUND"]
    await reauth(client, world.admin)
    await gql(client, world.admin, "mutation($id: ID!) { deleteService(id: $id) }", {"id": world.service_a})
    assert error_codes(await gql(client, world.admin, METRICS, {"s": world.service_a, "r": "LAST_1H"})) == ["NOT_FOUND"]
    assert error_codes(await gql(client, world.alice, METRICS, {"s": world.service_a, "r": "LAST_1H"})) == ["NOT_FOUND"]


async def test_health_states(container: Container, client: httpx.AsyncClient, world: World, secret_a: str) -> None:
    q = "query { services { edges { node { id health { status sampleCount15m telemetryStale evaluationStale activeIncidents } } } } }"

    async def health() -> dict[str, object]:
        body = await gql(client, world.alice, q)
        return body["data"]["services"]["edges"][0]["node"]["health"]  # type: ignore[no-any-return]

    h = await health()
    assert h["status"] == "NO_DATA" and h["telemetryStale"] is True and h["evaluationStale"] is True  # no worker heartbeat
    await ingest(client, secret_a, [event(world.route_a) for _ in range(19)])
    assert (await health())["status"] == "NO_DATA"
    await ingest(client, secret_a, [event(world.route_a)])
    await container.redis.flushall()
    h = await health()
    assert h["status"] == "HEALTHY" and h["sampleCount15m"] == 20 and h["telemetryStale"] is False
    async with container.db.transaction() as s:
        await s.execute(text("INSERT INTO worker_heartbeats (worker_id, started_at, heartbeat_at) VALUES ('w', now(), now())"))
    assert (await health())["evaluationStale"] is False
    now = utcnow()
    container.clock = lambda: now + timedelta(minutes=6)
    h = await health()
    assert h["telemetryStale"] is True and h["status"] == "HEALTHY"  # separate stale indicator alongside status
    container.clock = lambda: now + timedelta(minutes=16)
    h = await health()
    assert h["telemetryStale"] is True and h["status"] == "NO_DATA"  # silence never implies recovery


async def test_endpoints_pagination(client: httpx.AsyncClient, world: World, secret_a: str) -> None:
    await ingest(client, secret_a, [event(world.route_a, method=m) for m in ("GET", "POST", "PUT", "DELETE") for _ in range(2)])
    q = "query($s: ID!, $a: String) { endpoints(serviceId: $s, range: LAST_15M, first: 3, after: $a) { edges { cursor node { method requests } } pageInfo { hasNextPage endCursor } } }"
    p1 = (await gql(client, world.alice, q, {"s": world.service_a}))["data"]["endpoints"]
    assert len(p1["edges"]) == 3 and p1["pageInfo"]["hasNextPage"]
    p2 = (await gql(client, world.alice, q, {"s": world.service_a, "a": p1["pageInfo"]["endCursor"]}))["data"]["endpoints"]
    assert len(p2["edges"]) == 1 and not p2["pageInfo"]["hasNextPage"]
    assert {e["node"]["method"] for e in p1["edges"] + p2["edges"]} == {"GET", "POST", "PUT", "DELETE"}


async def test_as_of_single_per_operation(client: httpx.AsyncClient, world: World) -> None:
    body = await gql(
        client,
        world.alice,
        "query($s: ID!) { a: metrics(serviceId: $s, range: LAST_15M) { asOf } b: metrics(serviceId: $s, range: LAST_7D) { asOf } }",
        {"s": world.service_a},
    )
    assert body["data"]["a"]["asOf"] == body["data"]["b"]["asOf"]
    assert datetime.fromisoformat(body["data"]["a"]["asOf"]).tzinfo == UTC or body["data"]["a"]["asOf"].endswith("+00:00")
