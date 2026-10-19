"""Ingestion: atomic batches, retries/conflicts (incl. concurrency), service scoping,
revocation ordering, size/encoding limits, quota, and fail-closed dependency handling."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import text

from pulseops.container import Container, build_container
from pulseops.services.ingest import MAX_BODY_BYTES
from tests.conftest import make_settings
from tests.helpers import PASSWORD, World, build_world, event, gql, ingest, issue_token, reauth

pytestmark = pytest.mark.integration


async def count_events(c: Container, service_id: str | None = None) -> int:
    async with c.db.session() as s:
        if service_id:
            return int(
                (
                    await s.execute(text("SELECT count(*) FROM request_events WHERE service_id = :s"), {"s": uuid.UUID(service_id)})
                ).scalar_one()
            )
        return int((await s.execute(text("SELECT count(*) FROM request_events"))).scalar_one())


@pytest.fixture
async def world(container: Container, client: httpx.AsyncClient) -> World:
    return await build_world(container, client)


@pytest.fixture
async def secret_a(client: httpx.AsyncClient, world: World) -> str:
    return (await issue_token(client, world.alice, world.service_a))[1]


async def test_accept_then_duplicate_then_conflict(container: Container, client: httpx.AsyncClient, world: World, secret_a: str) -> None:
    batch = [event(world.route_a) for _ in range(3)]
    r = await ingest(client, secret_a, batch)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["accepted"], body["duplicate"]) == (3, 0) and uuid.UUID(body["requestId"])
    r = await ingest(client, secret_a, batch)  # exact retry after a lost response
    assert (r.json()["accepted"], r.json()["duplicate"]) == (0, 3)
    changed = [event(world.route_a), {**batch[1], "statusCode": 503}]
    r = await ingest(client, secret_a, changed)
    assert r.status_code == 409
    assert r.json()["error"]["details"] == [{"index": 1, "field": "eventId", "code": "conflicting_event_id"}]
    assert await count_events(container) == 3  # the new event in the conflicting batch was rolled back
    async with container.db.session() as s:
        assert (await s.execute(text("SELECT used FROM ingest_quota"))).scalar_one() == 3  # duplicates do not consume quota


async def test_invalid_item_rejects_whole_batch(container: Container, client: httpx.AsyncClient, world: World, secret_a: str) -> None:
    r = await ingest(client, secret_a, [event(world.route_a), {**event(world.route_a), "statusCode": "500"}, event(world.route_a)])
    assert r.status_code == 422
    assert r.json()["error"]["details"] == [{"index": 1, "field": "statusCode", "code": "invalid_type"}]
    assert await count_events(container) == 0


async def test_route_scoping_same_error(container: Container, client: httpx.AsyncClient, world: World, secret_a: str) -> None:
    other_service = await ingest(client, secret_a, [event(world.route_b)])
    missing = await ingest(client, secret_a, [event(str(uuid.uuid4()))])
    assert other_service.status_code == missing.status_code == 422
    assert (
        other_service.json()["error"]["details"]
        == missing.json()["error"]["details"]
        == [{"index": 0, "field": "routeId", "code": "unknown_route"}]
    )
    # Retired routes are rejected too.
    body = await gql(client, world.alice, "mutation($id: ID!) { retireRoute(id: $id) { retiredAt } }", {"id": world.route_a})
    assert body["data"]["retireRoute"]["retiredAt"]
    assert (await ingest(client, secret_a, [event(world.route_a)])).status_code == 422
    assert await count_events(container) == 0


async def test_token_cannot_choose_service(container: Container, client: httpx.AsyncClient, world: World, secret_a: str) -> None:
    r = await ingest(client, secret_a, [{**event(world.route_a), "serviceId": world.service_b}])
    assert r.status_code == 422 and await count_events(container, world.service_b) == 0


async def test_concurrent_conflicting_batches(container: Container, client: httpx.AsyncClient, world: World, secret_a: str) -> None:
    shared = str(uuid.uuid4())
    batches = [[event(world.route_a, event_id=shared, status=200 + i), event(world.route_a)] for i in range(8)]
    results = await asyncio.gather(*(ingest(client, secret_a, b) for b in batches))
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 1 and codes.count(409) == 7, codes
    # Exactly the winning batch's two events exist; losers rolled back atomically.
    assert await count_events(container) == 2


async def test_concurrent_identical_batches_count_once(
    container: Container, client: httpx.AsyncClient, world: World, secret_a: str
) -> None:
    batch = [event(world.route_a) for _ in range(20)]
    results = await asyncio.gather(*(ingest(client, secret_a, batch) for _ in range(6)))
    assert all(r.status_code == 200 for r in results)
    assert sum(r.json()["accepted"] for r in results) == 20
    assert all(r.json()["accepted"] + r.json()["duplicate"] == 20 for r in results)
    assert await count_events(container) == 20


async def test_revoked_and_rotated_tokens(container: Container, client: httpx.AsyncClient, world: World) -> None:
    token_id, secret = await issue_token(client, world.alice, world.service_a)
    assert (await ingest(client, secret, [event(world.route_a)])).status_code == 200
    body = await gql(client, world.alice, "mutation($id: ID!) { rotateIngestToken(id: $id) { token { id } secret } }", {"id": token_id})
    new_secret = body["data"]["rotateIngestToken"]["secret"]
    assert (await ingest(client, secret, [event(world.route_a)])).status_code == 401  # no grace period
    assert (await ingest(client, new_secret, [event(world.route_a)])).status_code == 200
    new_id = body["data"]["rotateIngestToken"]["token"]["id"]
    await gql(client, world.alice, "mutation($id: ID!) { revokeIngestToken(id: $id) { revokedAt } }", {"id": new_id})
    assert (await ingest(client, new_secret, [event(world.route_a)])).status_code == 401
    for bad in ("Bearer nope", "Basic abc", "", f"Bearer {secret}x"):
        r = await client.post("/api/v1/ingest/events", headers={"Authorization": bad, "Content-Type": "application/json"}, content="[]")
        assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIAL"


async def test_revocation_race_has_defined_order(container: Container, client: httpx.AsyncClient, world: World) -> None:
    """Every ingest acknowledged with 200 committed before the revocation; later ones are 401."""
    token_id, secret = await issue_token(client, world.alice, world.service_a)
    batches = [[event(world.route_a) for _ in range(10)] for _ in range(12)]

    async def revoke_soon() -> float:
        await asyncio.sleep(0.02)
        await gql(client, world.alice, "mutation($id: ID!) { revokeIngestToken(id: $id) { revokedAt } }", {"id": token_id})
        return datetime.now(UTC).timestamp()

    results = await asyncio.gather(*(ingest(client, secret, b) for b in batches), revoke_soon())
    responses = results[:-1]
    accepted = sum(r.json()["accepted"] for r in responses if r.status_code == 200)  # type: ignore[union-attr]
    assert {r.status_code for r in responses} <= {200, 401, 429}  # type: ignore[union-attr]
    assert await count_events(container) == accepted  # no unacknowledged commits, no acknowledged-but-missing events
    async with container.db.session() as s:
        revoked_at = (
            await s.execute(text("SELECT revoked_at FROM ingest_tokens WHERE id = :id"), {"id": uuid.UUID(token_id)})
        ).scalar_one()
        late = (await s.execute(text("SELECT count(*) FROM request_events WHERE received_at > :t"), {"t": revoked_at})).scalar_one()
    assert late == 0
    assert (await ingest(client, secret, [event(world.route_a)])).status_code == 401


async def test_size_and_encoding_limits(container: Container, client: httpx.AsyncClient, world: World, secret_a: str) -> None:
    payload = json.dumps([event(world.route_a)]).encode()
    exact = payload + b" " * (MAX_BODY_BYTES - len(payload))
    assert len(exact) == MAX_BODY_BYTES
    assert (await ingest(client, secret_a, exact)).status_code == 200
    assert (await ingest(client, secret_a, exact + b" ")).status_code == 413

    async def chunks():  # type: ignore[no-untyped-def]
        yield b"["
        for _ in range(300):
            yield b" " * 4096
        yield b"]"

    r = await client.post(
        "/api/v1/ingest/events", headers={"Authorization": f"Bearer {secret_a}", "Content-Type": "application/json"}, content=chunks()
    )
    assert r.status_code == 413  # cap enforced on the stream without Content-Length
    assert (await ingest(client, secret_a, payload, **{"Content-Encoding": "gzip"})).status_code == 415
    assert (
        await client.post(
            "/api/v1/ingest/events", headers={"Authorization": f"Bearer {secret_a}", "Content-Type": "text/plain"}, content=payload
        )
    ).status_code == 415
    assert (await ingest(client, secret_a, b'[{"eventId": 1,]')).status_code == 400
    assert (await ingest(client, secret_a, [event(world.route_a)] * 501)).status_code == 422
    assert (await ingest(client, secret_a, [])).status_code == 422


async def test_time_and_number_boundaries(client: httpx.AsyncClient, world: World, secret_a: str) -> None:
    now = datetime.now(UTC)
    ok = [
        event(world.route_a, at=now - timedelta(hours=23, minutes=59)),
        event(world.route_a, at=now + timedelta(minutes=4)),
        event(world.route_a, duration=0),
        event(world.route_a, duration=120000),
        event(world.route_a, status=100),
        event(world.route_a, status=599),
    ]
    assert (await ingest(client, secret_a, ok)).status_code == 200
    for bad in (
        event(world.route_a, at=now - timedelta(hours=24, minutes=1)),
        event(world.route_a, at=now + timedelta(minutes=6)),
        {**event(world.route_a), "durationMs": 120000.5},
        {**event(world.route_a), "statusCode": 600},
        {**event(world.route_a), "durationMs": True},
    ):
        assert (await ingest(client, secret_a, [bad])).status_code == 422


async def test_rate_limit_and_quota(container: Container, client: httpx.AsyncClient, world: World, secret_a: str) -> None:
    results = await asyncio.gather(*(ingest(client, secret_a, [event(world.route_a)]) for _ in range(40)))
    limited = [r for r in results if r.status_code == 429]
    assert limited and all(int(r.headers["retry-after"]) >= 1 for r in limited)
    await container.redis.flushall()
    async with container.db.transaction() as s:
        await s.execute(text("UPDATE ingest_quota SET used = 999999 WHERE id = 1"))
    before = await count_events(container)
    r = await ingest(client, secret_a, [event(world.route_a), event(world.route_a)])
    assert r.status_code == 429 and r.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert await count_events(container) == before  # nothing committed, nothing evicted


async def test_redis_outage_fails_closed(
    container: Container, client: httpx.AsyncClient, world: World, secret_a: str, secrets_dir, pg, redis_container
) -> None:  # type: ignore[no-untyped-def]
    from pulseops.api.app import create_app

    broken = build_container(make_settings(secrets_dir, pg, redis_container, redis_port=1), application_name="pulseops-test-noredis")
    app = create_app(container=broken)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c2,
        ):
            assert (await ingest(c2, secret_a, [event(world.route_a)])).status_code == 503
            login = await c2.post(
                "/api/v1/auth/login",
                headers={"Origin": "http://127.0.0.1:8080", "X-PulseOps-CSRF": "1", "Content-Type": "application/json"},
                content=json.dumps({"email": "alice@pulseops.test", "password": PASSWORD}),
            )
            assert login.status_code == 503
            # Existing authenticated reads use the stricter local fallback; mutations fail closed.
            read = await c2.post(
                "/graphql",
                headers={"Authorization": f"Bearer {world.alice.token}", "Content-Type": "application/json"},
                content=json.dumps({"query": "query { me { id } }"}),
            )
            assert read.status_code == 200 and "errors" not in read.json()
            write = await c2.post(
                "/graphql",
                headers={"Authorization": f"Bearer {world.alice.token}", "Content-Type": "application/json"},
                content=json.dumps({"query": 'mutation { createService(input: {name: "x", environment: "y"}) { id } }'}),
            )
            assert write.status_code == 503
            assert (await c2.get("/health/ready")).status_code == 503
    finally:
        await broken.close()
    assert await count_events(container) == 0


async def test_database_outage_returns_503(container: Container, world: World, secret_a: str, secrets_dir, pg, redis_container) -> None:  # type: ignore[no-untyped-def]
    from pulseops.api.app import create_app

    broken = build_container(
        make_settings(secrets_dir, pg, redis_container, db_port=1, db_pool_timeout_s=0.5), application_name="pulseops-test-nodb"
    )
    app = create_app(container=broken)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c2,
        ):
            r = await ingest(c2, secret_a, [event(world.route_a)])
            assert r.status_code == 503 and r.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
            r = await c2.post(
                "/graphql",
                headers={"Authorization": f"Bearer {world.alice.token}", "Content-Type": "application/json"},
                content=json.dumps({"query": "query { me { id } }"}),
            )
            assert r.status_code in (401, 503)  # auth-state failure always denies
            assert (await c2.get("/health/ready")).status_code == 503
            assert (await c2.get("/health/live")).status_code == 200
    finally:
        await broken.close()


async def test_token_limit_and_reauth(client: httpx.AsyncClient, world: World) -> None:
    q = "mutation($s: ID!) { createIngestToken(serviceId: $s) { secret } }"
    no_reauth = await gql(client, world.bob, q, {"s": world.service_b})
    assert no_reauth["errors"][0]["extensions"]["code"] == "REAUTH_REQUIRED"
    await reauth(client, world.bob)
    for _ in range(2):
        assert "errors" not in await gql(client, world.bob, q, {"s": world.service_b})
    third = await gql(client, world.bob, q, {"s": world.service_b})
    assert third["errors"][0]["extensions"] == {"code": "CONFLICT", "details": [{"code": "token_limit"}]}
    listing = await gql(
        client, world.bob, "query($s: ID!) { service(id: $s) { ingestTokens { publicId revokedAt } } }", {"s": world.service_b}
    )
    assert len(listing["data"]["service"]["ingestTokens"]) == 2
    assert "secret" not in json.dumps(listing)


async def test_concurrent_rotation_and_deletion_never_500(container: Container, client: httpx.AsyncClient, world: World) -> None:
    """Ingests racing token rotation and service deletion end in 200/401/429/503, never a 500."""
    token_id, secret = await issue_token(client, world.alice, world.service_a)
    batches = [[event(world.route_a) for _ in range(5)] for _ in range(15)]

    async def rotate_then_delete() -> None:
        await asyncio.sleep(0.01)
        await gql(client, world.alice, "mutation($id: ID!) { rotateIngestToken(id: $id) { secret } }", {"id": token_id})
        await reauth(client, world.admin)
        await gql(client, world.admin, "mutation($id: ID!) { deleteService(id: $id) }", {"id": world.service_a})

    results = await asyncio.gather(*(ingest(client, secret, b) for b in batches), rotate_then_delete())
    codes = {r.status_code for r in results[:-1]}  # type: ignore[union-attr]
    assert codes <= {200, 401, 429, 503}, codes
    accepted = sum(r.json()["accepted"] for r in results[:-1] if r.status_code == 200)  # type: ignore[union-attr]
    assert await count_events(container) == accepted
