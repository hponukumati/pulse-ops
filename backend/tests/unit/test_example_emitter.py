"""examples/fastapi-emitter: reports only registered routes with accepted methods, builds batches
the ingest validator accepts, and keeps a batch for retry only on network errors, 429 and 503."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import ModuleType

import httpx
import pytest

from pulseops.services.ingest import parse_body, validate_batch
from tests.helpers import load_emitter_example

HEALTH, ORDERS = str(uuid.uuid4()), str(uuid.uuid4())


@pytest.fixture
def emitter(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    return load_emitter_example(monkeypatch, "http://pulseops.invalid", "poi_placeholder", HEALTH, ORDERS)


async def call(emitter: ModuleType, method: str, path: str) -> int:
    # ASGITransport skips the lifespan, so the background sender stays off.
    transport = httpx.ASGITransport(app=emitter.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://demo") as c:
        return (await c.request(method, path)).status_code


async def test_reports_registered_routes_only_and_batches_pass_ingest_validation(
    emitter: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(emitter, "FAIL_RATE", 1.0)
    assert await call(emitter, "GET", "/orders/1") == 503
    monkeypatch.setattr(emitter, "FAIL_RATE", 0.0)
    assert await call(emitter, "GET", "/orders/2") == 200
    assert await call(emitter, "GET", "/health") == 200
    assert await call(emitter, "GET", "/not-registered") == 404
    assert await call(emitter, "TRACE", "/health") == 405  # route matches, but PulseOps rejects the method

    assert [(e["routeId"], e["statusCode"]) for e in emitter.buffer] == [(ORDERS, 503), (ORDERS, 200), (HEALTH, 200)]
    validate_batch(parse_body(json.dumps(emitter.buffer).encode()), datetime.now(UTC))


async def test_unhandled_error_is_reported_as_500(emitter: ModuleType) -> None:
    @emitter.app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("synthetic failure")

    emitter.ROUTE_IDS["/boom"] = ORDERS
    assert await call(emitter, "GET", "/boom") == 500
    assert [e["statusCode"] for e in emitter.buffer] == [500]


def test_buffer_and_duration_are_bounded(emitter: ModuleType) -> None:
    emitter.record("GET", "/health", 200, 999_999.0)
    assert emitter.buffer[0]["durationMs"] == 120_000
    emitter.buffer.extend([{}] * (emitter.MAX_BUFFER - 1))
    emitter.record("GET", "/health", 200, 1.0)
    assert len(emitter.buffer) == emitter.MAX_BUFFER


@pytest.mark.parametrize(("outcome", "kept"), [(200, False), (422, False), (429, True), (503, True), ("network", True)])
async def test_flush_keeps_batch_only_for_retryable_failures(emitter: ModuleType, outcome: int | str, kept: bool) -> None:
    for _ in range(emitter.MAX_BATCH + 1):
        emitter.record("GET", "/health", 200, 1.0)
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if outcome == "network":
            raise httpx.ConnectError("synthetic")
        return httpx.Response(int(outcome))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await emitter.flush_once(client) is not kept
    assert len(emitter.buffer) == (emitter.MAX_BATCH + 1 if kept else 1)
    assert len(json.loads(sent[0].content)) == emitter.MAX_BATCH
    assert sent[0].headers["authorization"] == "Bearer poi_placeholder"
