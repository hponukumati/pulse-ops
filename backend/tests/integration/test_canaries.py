"""Sensitive-data canaries: rejected inputs and exception paths must leave no trace in
responses, application logs, PostgreSQL or Redis."""

from __future__ import annotations

import io
import json
import logging
import secrets
import uuid

import httpx
import pytest
from sqlalchemy import text
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from pulseops.container import Container
from pulseops.logging import AllowlistJsonFormatter
from pulseops.services import catalog as catalog_module
from tests.conftest import APP_TABLES
from tests.helpers import AUTH_HEADERS, COOKIE, ORIGIN, World, build_world, event, gql, ingest, issue_token

pytestmark = pytest.mark.integration

TAG = secrets.token_hex(6)
SECRET = f"CANARY-SECRET-{TAG}"
EMAIL = f"canary-{TAG}@leak.test"
BODY = f"CANARY-BODY-{TAG}"
CANARIES = (SECRET, EMAIL, BODY, TAG)


@pytest.fixture
def log_capture() -> io.StringIO:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(AllowlistJsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    prev = root.level
    root.setLevel(logging.INFO)
    logging.getLogger("pulseops").setLevel(logging.INFO)
    yield buf
    root.removeHandler(handler)
    root.setLevel(prev)


def assert_clean(label: str, blob: str) -> None:
    for c in CANARIES:
        assert c not in blob, f"canary leaked into {label}"


async def test_canaries_never_persist_or_leak(
    container: Container, client: httpx.AsyncClient, server: str, log_capture: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    world: World = await build_world(container, client)
    _, ingest_secret = await issue_token(client, world.alice, world.service_a)
    responses: list[str] = []

    async def keep(r: httpx.Response) -> None:
        responses.append(r.text + json.dumps(dict(r.headers)))

    # Authentication surfaces.
    await keep(await client.post("/api/v1/auth/login", headers=AUTH_HEADERS, content=json.dumps({"email": EMAIL, "password": SECRET})))
    await keep(
        await client.post(
            "/api/v1/auth/login", headers=AUTH_HEADERS, content=json.dumps({"email": EMAIL, "password": SECRET, "note": BODY})
        )
    )
    await keep(
        await client.post(
            "/api/v1/auth/login", headers={**AUTH_HEADERS, "X-Debug": SECRET}, content=f'{{"email": "{EMAIL}", "email": "{BODY}"}}'
        )
    )
    await keep(await client.post("/api/v1/auth/refresh", headers={**AUTH_HEADERS, "Cookie": f"{COOKIE}={SECRET}"}, content="{}"))
    await keep(
        await client.post(
            "/api/v1/auth/reauthenticate",
            headers={**AUTH_HEADERS, "Authorization": f"Bearer {world.alice.token}"},
            content=json.dumps({"password": SECRET}),
        )
    )

    # Ingest: every field, unknown fields, headers, query string, bad credentials, malformed JSON.
    bad_items = [
        {**event(world.route_a), "method": BODY},
        {**event(world.route_a), "statusCode": SECRET},
        {**event(world.route_a), "durationMs": EMAIL},
        {**event(world.route_a), "timestamp": BODY},
        {**event(world.route_a), "routeId": SECRET},
        {**event(world.route_a), "eventId": EMAIL},
        {**event(world.route_a), "headers": {"authorization": SECRET}, "body": BODY, "url": f"/x?email={EMAIL}"},
    ]
    await keep(await ingest(client, ingest_secret, bad_items, **{"X-Forwarded-User": EMAIL, "Cookie": f"session={SECRET}"}))
    await keep(
        await client.post(
            f"/api/v1/ingest/events?token={SECRET}&email={EMAIL}",
            headers={"Authorization": f"Bearer {ingest_secret}", "Content-Type": "application/json"},
            content=json.dumps(bad_items),
        )
    )
    await keep(await ingest(client, f"poi_{'a' * 20}_{SECRET}", [event(world.route_a)]))
    await keep(await ingest(client, SECRET, [event(world.route_a)]))
    await keep(await ingest(client, ingest_secret, f'[{{"eventId": "{SECRET}", "eventId": "{BODY}"}}]'.encode()))
    await keep(await ingest(client, ingest_secret, f'["{SECRET}", NaN]'.encode()))

    # GraphQL: invalid variables/literals, invalid metadata, unknown paths.
    await keep(
        await client.post(
            "/graphql",
            headers={"Authorization": f"Bearer {world.alice.token}", "Content-Type": "application/json"},
            content=json.dumps({"query": "query($id: ID!) { service(id: $id) { id } }", "variables": {"id": SECRET}}),
        )
    )
    await keep(
        await client.post(
            "/graphql",
            headers={"Authorization": f"Bearer {world.alice.token}", "Content-Type": "application/json"},
            content=json.dumps({"query": "query($n: Int) { services(first: $n) { totalCount } }", "variables": {"n": BODY}}),
        )
    )
    await keep(
        await client.post(
            "/graphql",
            headers={"Authorization": f"Bearer {world.alice.token}", "Content-Type": "application/json"},
            content=json.dumps({"query": f'query {{ services(first: "{SECRET}") {{ totalCount }} }}'}),
        )
    )
    for template in (f"/users/{EMAIL}", f"/x?token={SECRET}", f"https://{TAG}.example/x"):
        r = await gql(
            client,
            world.alice,
            "mutation($s: ID!, $t: String!) { registerRoute(serviceId: $s, template: $t) { id } }",
            {"s": world.service_a, "t": template},
        )
        responses.append(json.dumps(r))
    r = await gql(
        client,
        world.alice,
        "mutation($i: ServiceInput!) { createService(input: $i) { id } }",
        {"i": {"name": EMAIL, "environment": SECRET}},
    )
    responses.append(json.dumps(r))
    await keep(
        await client.post(
            "/graphql",
            headers={"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"},
            content=json.dumps({"query": "{ me { id } }"}),
        )
    )
    await keep(await client.get(f"/{SECRET}/{EMAIL}?q={BODY}"))

    # WebSocket: credential canary in connection_init.
    async with connect(server.replace("http", "ws") + "/graphql", origin=ORIGIN, subprotocols=["graphql-transport-ws"]) as ws:  # type: ignore[arg-type]
        await ws.send(json.dumps({"type": "connection_init", "payload": {"authorization": f"Bearer {SECRET}", "note": BODY}}))
        with pytest.raises(ConnectionClosed):
            while True:
                responses.append(str(await ws.recv()))

    # Exception path: an unexpected error whose message contains a canary.
    async def explode(*_: object, **__: object) -> None:
        raise RuntimeError(f"unexpected failure {SECRET} {EMAIL}")

    monkeypatch.setattr(catalog_module.CatalogService, "register_route", explode)
    r = await gql(client, world.alice, 'mutation($s: ID!) { registerRoute(serviceId: $s, template: "/ok") { id } }', {"s": world.service_a})
    assert r["errors"][0]["extensions"]["code"] == "INTERNAL"
    responses.append(json.dumps(r))

    # ---- assertions ----
    assert_clean("HTTP/WebSocket responses", "\n".join(responses))
    assert_clean("application logs", log_capture.getvalue())
    assert "unexpected_error" in log_capture.getvalue() or "graphql.error" in log_capture.getvalue()  # the failure *was* logged, safely
    async with container.db.session() as s:
        for table in [*APP_TABLES, "ingest_quota", "app_settings"]:
            rows = (await s.execute(text(f"SELECT coalesce(string_agg(row_to_json(t)::text, '\n'), '') FROM {table} t"))).scalar_one()
            assert_clean(f"table {table}", rows)
    redis_blob = []
    async for key in container.redis.scan_iter(count=500):
        kind = (await container.redis.type(key)).decode()
        value: object = None
        if kind == "string":
            value = await container.redis.get(key)
        elif kind == "hash":
            value = await container.redis.hgetall(key)
        elif kind == "zset":
            value = await container.redis.zrange(key, 0, -1)
        redis_blob.append(f"{key!r} {value!r}")
    assert redis_blob  # rate-limit state exists, keyed by opaque digests
    assert_clean("Redis", "\n".join(redis_blob))
    assert uuid.UUID(world.service_a)
