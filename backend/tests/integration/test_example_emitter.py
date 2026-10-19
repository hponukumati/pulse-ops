"""examples/fastapi-emitter end to end: its batches are accepted by the real ingest endpoint."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import text

from pulseops.container import Container
from tests.helpers import build_world, issue_token, load_emitter_example, register_route

pytestmark = pytest.mark.integration


async def test_example_emitter_events_are_stored(
    container: Container, server: str, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await build_world(container, client)
    health = await register_route(client, world.alice, world.service_a, "/health")
    _, secret = await issue_token(client, world.alice, world.service_a)
    # The example's "/orders/{order_id}" maps to the registered "/orders/{orderId}" by route ID.
    emitter = load_emitter_example(monkeypatch, server, secret, health, world.route_a)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=emitter.app), base_url="http://demo") as demo:
        monkeypatch.setattr(emitter, "FAIL_RATE", 1.0)
        assert (await demo.get("/orders/7")).status_code == 503
        monkeypatch.setattr(emitter, "FAIL_RATE", 0.0)
        assert (await demo.get("/orders/8")).status_code == 200
        assert (await demo.get("/health")).status_code == 200
        assert (await demo.get("/unregistered")).status_code == 404

    async with httpx.AsyncClient(timeout=5) as http:
        assert await emitter.flush_once(http) is True
    assert emitter.buffer == []

    async with container.db.session() as s:
        rows = (
            await s.execute(
                text("SELECT route_id, status_code FROM request_events WHERE service_id = :s"), {"s": uuid.UUID(world.service_a)}
            )
        ).all()
    assert sorted((str(route), status) for route, status in rows) == sorted([(world.route_a, 503), (world.route_a, 200), (health, 200)])
