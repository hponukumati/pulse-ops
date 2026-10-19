"""Live subscriptions over graphql-transport-ws with authorization on every delivery."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from pulseops.api import ws as ws_module
from pulseops.container import Container, utcnow
from pulseops.notifications import CHANNEL
from pulseops.policy import Principal
from pulseops.services import incident_state as st
from pulseops.services.admin import AdminService
from pulseops.services.alerts import AlertService, Comparator, Metric, RuleInput
from tests.helpers import AUTH_HEADERS, COOKIE, ORIGIN, World, build_world

pytestmark = pytest.mark.integration

SUB = "subscription($s: ID!) { incidentChanged(serviceId: $s) { eventId version incident { id status version service { id name } } } }"


@pytest.fixture
async def world(container: Container, client: httpx.AsyncClient) -> World:
    return await build_world(container, client)


def ws_url(server: str) -> str:
    return server.replace("http://", "ws://") + "/graphql"


@asynccontextmanager
async def open_ws(server: str, token: str | None, *, origin: str = ORIGIN, ack: bool = True) -> AsyncIterator[ClientConnection]:
    async with connect(ws_url(server), origin=origin, subprotocols=["graphql-transport-ws"], max_size=2**20) as conn:  # type: ignore[arg-type]
        if token is not None:
            await conn.send(json.dumps({"type": "connection_init", "payload": {"authorization": f"Bearer {token}"}}))
            if ack:
                msg = json.loads(await asyncio.wait_for(conn.recv(), 5))
                assert msg["type"] == "connection_ack", msg
        yield conn


async def subscribe(conn: ClientConnection, op_id: str, service_id: str) -> None:
    await conn.send(json.dumps({"id": op_id, "type": "subscribe", "payload": {"query": SUB, "variables": {"s": service_id}}}))


async def recv(conn: ClientConnection, timeout: float = 3.0) -> dict[str, Any] | None:
    try:
        return json.loads(await asyncio.wait_for(conn.recv(), timeout))  # type: ignore[no-any-return]
    except TimeoutError:
        return None


async def close_code(conn: ClientConnection, timeout: float = 5.0) -> int | None:
    try:
        while True:
            await asyncio.wait_for(conn.recv(), timeout)
    except ConnectionClosed as exc:
        return exc.rcvd.code if exc.rcvd else None
    except TimeoutError:
        return None


async def open_incident(c: Container, service_id: str) -> tuple[uuid.UUID, uuid.UUID]:
    rule = await AlertService(c).create_rule(
        Principal.cli(), uuid.UUID(service_id), RuleInput("errors", Metric.ERROR_RATE_5XX, Comparator.GT, 0.1, 5)
    )
    async with c.db.transaction() as s:
        locked = await st.lock_rule(s, rule.id)
        assert locked
        inc = await st.open_incident(s, locked, c.now(), value=0.5, slot=None)
    return rule.id, inc.id


async def notify(c: Container, service_id: str, incident_id: uuid.UUID, version: int = 1, event_id: str | None = None) -> None:
    payload = {"serviceId": service_id, "incidentId": str(incident_id), "eventId": event_id or str(uuid.uuid4()), "version": version}
    await c.redis.publish(CHANNEL, json.dumps(payload))


async def wait_for_hub(server: str) -> None:
    await asyncio.sleep(0.3)  # subscription registration is asynchronous


async def test_live_delivery_with_authoritative_refetch(container: Container, server: str, world: World) -> None:
    async with open_ws(server, world.alice.token) as conn:
        await subscribe(conn, "1", world.service_a)
        await wait_for_hub(server)
        _, inc = await open_incident(container, world.service_a)
        await notify(container, world.service_a, inc)
        msg = await recv(conn)
        assert msg and msg["type"] == "next", msg
        data = msg["payload"]["data"]["incidentChanged"]
        assert data["incident"]["status"] == "OPEN" and data["incident"]["service"]["id"] == world.service_a


async def test_viewer_can_subscribe_but_cross_service_denied(container: Container, server: str, world: World) -> None:
    async with open_ws(server, world.victor.token) as conn:
        await subscribe(conn, "a", world.service_a)
        await subscribe(conn, "b", world.service_b)
        msg = await recv(conn)
        assert msg == {"id": "b", "type": "error", "payload": [{"message": "Not found", "extensions": {"code": "NOT_FOUND"}}]}
        _, inc_b = await open_incident(container, world.service_b)
        await notify(container, world.service_b, inc_b)  # Alice/Victor never see B's events
        assert await recv(conn, 1.0) is None


async def test_handshake_rules(container: Container, server: str, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(InvalidStatus):
        async with open_ws(server, world.alice.token, origin="http://evil.example"):
            pass
    with pytest.raises(InvalidStatus):  # missing subprotocol
        async with connect(ws_url(server), origin=ORIGIN):  # type: ignore[arg-type]
            pass
    async with open_ws(server, "not-a-jwt", ack=False) as conn:
        assert await close_code(conn) == 4403
    monkeypatch.setattr(ws_module, "INIT_TIMEOUT_S", 0.5)
    async with open_ws(server, None) as conn:
        assert await close_code(conn) == 4408


async def test_session_revocation_blocks_next_delivery(container: Container, client: httpx.AsyncClient, server: str, world: World) -> None:
    _, inc = await open_incident(container, world.service_a)
    async with open_ws(server, world.alice.token) as conn:
        await subscribe(conn, "1", world.service_a)
        await wait_for_hub(server)
        r = await client.post("/api/v1/auth/logout", headers={**AUTH_HEADERS, "Cookie": f"{COOKIE}={world.alice.refresh}"}, content="{}")
        assert r.status_code == 204
        await notify(container, world.service_a, inc)
        assert await close_code(conn) == 4403  # closed instead of delivering


async def test_grant_revocation_detected_on_idle_connection(
    container: Container, server: str, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ws_module, "RECHECK_INTERVAL_S", 0.5)
    async with open_ws(server, world.victor.token) as conn:
        await subscribe(conn, "1", world.service_a)
        await wait_for_hub(server)
        await AdminService(container).revoke_grant(Principal.cli(), world.ids["victor"], uuid.UUID(world.service_a))
        msg = await recv(conn, 3.0)
        assert msg and msg["type"] == "error" and msg["payload"][0]["extensions"]["code"] == "NOT_FOUND"


async def test_disabled_account_closes_idle_connection(
    container: Container, server: str, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ws_module, "RECHECK_INTERVAL_S", 0.5)
    async with open_ws(server, world.alice.token) as conn:
        await AdminService(container).set_disabled(Principal.cli(), world.ids["alice"], True)
        assert await close_code(conn) == 4403


async def test_connection_and_subscription_limits(server: str, world: World) -> None:
    async with open_ws(server, world.alice.token), open_ws(server, world.alice.token):
        async with open_ws(server, world.alice.token, ack=False) as third:
            assert await close_code(third) == 4429
    async with open_ws(server, world.alice.token) as conn:
        for i in range(6):
            await subscribe(conn, str(i), world.service_a)
        msg = await recv(conn)
        assert msg and msg["id"] == "5" and msg["payload"][0]["extensions"]["code"] == "TOO_MANY_SUBSCRIPTIONS"
        await subscribe(conn, "0", world.service_a)  # duplicate id
        assert await close_code(conn) == 4409


async def test_frame_and_rate_limits(server: str, world: World) -> None:
    async with open_ws(server, world.alice.token) as conn:
        await conn.send(json.dumps({"type": "ping", "payload": {"x": "a" * 20000}}))
        assert await close_code(conn) in (1009, 4400)
    async with open_ws(server, world.alice.token) as conn:
        for _ in range(61):
            await conn.send(json.dumps({"type": "ping"}))
        assert await close_code(conn) == 4429


async def test_duplicate_notifications_delivered_once(container: Container, server: str, world: World) -> None:
    _, inc = await open_incident(container, world.service_a)
    async with open_ws(server, world.alice.token) as conn:
        await subscribe(conn, "1", world.service_a)
        await wait_for_hub(server)
        event_id = str(uuid.uuid4())
        await notify(container, world.service_a, inc, event_id=event_id)
        await notify(container, world.service_a, inc, event_id=event_id)  # at-least-once republish
        first = await recv(conn)
        assert first and first["type"] == "next"
        assert await recv(conn, 1.0) is None
        await notify(container, world.service_a, inc, version=99)  # bogus version: refetch shows real version 1, already seen
        assert await recv(conn, 1.0) is None


async def test_closes_when_access_token_expires(container: Container, server: str, world: World) -> None:
    async with container.db.transaction() as s:
        sid = (
            await s.execute(text("SELECT id FROM sessions WHERE user_id = :u ORDER BY created_at DESC LIMIT 1"), {"u": world.ids["alice"]})
        ).scalar_one()
        await s.execute(text("UPDATE sessions SET expires_at = :e WHERE id = :id"), {"e": utcnow() + timedelta(seconds=2), "id": sid})
    short, _ = container.jwt.issue(world.ids["alice"], sid, utcnow() + timedelta(seconds=2))
    async with open_ws(server, short) as conn:
        assert await close_code(conn, 8) == 4498
