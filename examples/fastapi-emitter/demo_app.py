# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi==0.143.3", "uvicorn==0.54.0", "httpx==0.28.1"]
# ///
"""Example service that reports every request it handles to PulseOps (demo only).

Middleware records the six permitted fields per request and a background task sends them in
batches. Only requests on registered routes are reported, as route templates, never raw URLs.
Configuration (environment): PULSEOPS_INGEST_TOKEN, HEALTH_ROUTE_ID, ORDERS_ROUTE_ID, and
optionally PULSEOPS_URL (default http://127.0.0.1:8080), FAIL_RATE (default 0.02), PORT (9000).
"""

import asyncio
import os
import random
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

PULSEOPS_URL = os.environ.get("PULSEOPS_URL", "http://127.0.0.1:8080") + "/api/v1/ingest/events"
TOKEN = os.environ["PULSEOPS_INGEST_TOKEN"]
FAIL_RATE = float(os.environ.get("FAIL_RATE", "0.02"))
# Route template as the framework matched it -> PulseOps route ID (shown in the service's Settings tab).
# Anything else is not reported: one unknown route or method would reject the whole batch.
ROUTE_IDS = {
    "/health": os.environ["HEALTH_ROUTE_ID"],
    "/orders/{order_id}": os.environ["ORDERS_ROUTE_ID"],
}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
MAX_BATCH = 500  # PulseOps limit per request
MAX_BUFFER = 10_000  # while PulseOps is unreachable, drop new events instead of growing without bound
FLUSH_SECONDS = 2.0

buffer: list[dict[str, Any]] = []


def record(method: str, route_template: str | None, status: int, duration_ms: float) -> None:
    route_id = ROUTE_IDS.get(route_template or "")
    if route_id and method in METHODS and len(buffer) < MAX_BUFFER:
        buffer.append(
            {
                "eventId": str(uuid.uuid4()),
                "timestamp": datetime.now(UTC).isoformat(),
                "method": method,
                "routeId": route_id,
                "statusCode": status,
                "durationMs": round(min(duration_ms, 120_000), 3),
            }
        )


async def flush_once(client: httpx.AsyncClient) -> bool:
    """Send the oldest batch. Returns False if it was kept for a retry (network error, 429, 503)."""
    batch = buffer[:MAX_BATCH]
    if not batch:
        return True
    try:
        r = await client.post(PULSEOPS_URL, json=batch, headers={"Authorization": f"Bearer {TOKEN}"})
    except httpx.HTTPError:
        return False
    if r.status_code in (429, 503):
        return False  # retried later with the same event IDs, so nothing is counted twice
    del buffer[: len(batch)]  # accepted, or rejected for good (for example 422 after a route was retired)
    return True


async def flush_forever() -> None:
    delay = FLUSH_SECONDS
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            await asyncio.sleep(delay * random.uniform(0.8, 1.2))
            delay = FLUSH_SECONDS if await flush_once(client) else min(delay * 2, 60.0)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    task = asyncio.create_task(flush_forever())
    yield
    task.cancel()  # events still buffered at shutdown are lost (acceptable for a demo)


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def report_to_pulseops(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    start, status = time.perf_counter(), 500  # stays 500 if the handler raises
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")  # set by FastAPI once a route matches
        record(request.method, getattr(route, "path", None), status, (time.perf_counter() - start) * 1000)


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/orders/{order_id}")
async def get_order(order_id: int) -> dict[str, int]:
    await asyncio.sleep(random.uniform(0.01, 0.15))  # simulated work
    if random.random() < FAIL_RATE:
        raise HTTPException(503)
    return {"id": order_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "9000")))
