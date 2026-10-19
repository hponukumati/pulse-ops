"""Pure ASGI middleware: correlation IDs, overall timeout, security headers, safe access logs."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pulseops.container import request_id_var
from pulseops.logging import log_event

HTTP_TIMEOUT_S = 5.0

SECURITY_HEADERS = [
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"),
    (b"cross-origin-resource-policy", b"same-origin"),
]


def _route_label(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, timeout_s: float = HTTP_TIMEOUT_S) -> None:
        self.app = app
        self.timeout_s = timeout_s

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                token = request_id_var.set(uuid.uuid4())
                try:
                    await self.app(scope, receive, send)
                finally:
                    request_id_var.reset(token)
                return
            await self.app(scope, receive, send)
            return

        request_id = uuid.uuid4()
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        state: dict[str, Any] = {"started": False, "status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["started"], state["status"] = True, message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in {h for h, _ in SECURITY_HEADERS}]
                headers += SECURITY_HEADERS
                headers.append((b"x-request-id", str(request_id).encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            async with asyncio.timeout(self.timeout_s):
                await self.app(scope, receive, send_wrapper)
        except TimeoutError:
            if not state["started"]:
                body = json.dumps({"error": {"code": "TIMEOUT", "message": "Request timed out", "requestId": str(request_id)}}).encode()
                await send_wrapper({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": body})
            state["status"] = 503
        finally:
            log_event(
                "http.request",
                request_id=request_id,
                method=scope.get("method"),
                route=_route_label(scope),
                status=state["status"],
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            request_id_var.reset(token)
