"""Overall HTTP timeout and security headers from the ASGI middleware."""

from __future__ import annotations

import asyncio

import httpx

from pulseops.api.middleware import RequestContextMiddleware


async def slow_app(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
    await asyncio.sleep(5)


async def fast_app(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_overall_timeout_returns_503_json() -> None:
    app = RequestContextMiddleware(slow_app, timeout_s=0.2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/anything")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "TIMEOUT"
    assert r.headers["cache-control"] == "no-store"


async def test_security_headers_and_request_id() -> None:
    app = RequestContextMiddleware(fast_app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/")
    for header, value in (
        ("cache-control", "no-store"),
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "no-referrer"),
        ("x-frame-options", "DENY"),
    ):
        assert r.headers[header] == value
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert len(r.headers["x-request-id"]) == 36


def test_deadlock_and_serialization_failures_map_to_503() -> None:
    from sqlalchemy.exc import DBAPIError

    from pulseops.db import classify_db_error
    from pulseops.errors import DependencyUnavailable

    class Orig(Exception):
        def __init__(self, sqlstate: str) -> None:
            super().__init__("x")
            self.sqlstate = sqlstate

    for state in ("40P01", "40001"):
        err = DBAPIError("stmt", {}, Orig(state))
        assert isinstance(classify_db_error(err), DependencyUnavailable)
    assert classify_db_error(DBAPIError("stmt", {}, Orig("23505"))) is None  # integrity errors are not infrastructure
