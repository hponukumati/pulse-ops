"""HTTP helpers shared by REST routes and the GraphQL endpoint."""

from __future__ import annotations

import ipaddress
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from pulseops.config import Settings
from pulseops.container import current_request_id
from pulseops.errors import AppError, OriginRejected, PayloadTooLarge, UnsupportedMedia

CSRF_HEADER = "x-pulseops-csrf"

PRIVATE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def error_response(exc: AppError) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": exc.code, "message": exc.message, "requestId": str(current_request_id() or "")}}
    if exc.details:
        body["error"]["details"] = exc.details
    headers = dict(PRIVATE_HEADERS)
    if exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    return JSONResponse(body, status_code=exc.http_status, headers=headers)


def require_json_content(request: Request) -> None:
    ctype = request.headers.get("content-type", "")
    media = ctype.split(";", 1)[0].strip().lower()
    params = ctype.split(";", 1)[1].strip().lower().replace(" ", "") if ";" in ctype else ""
    if media != "application/json" or (params and params not in ("charset=utf-8",)):
        raise UnsupportedMedia()
    encoding = request.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        raise UnsupportedMedia()  # compressed bodies are rejected in v1


async def read_body_capped(request: Request, limit: int) -> bytes:
    """Read the body while enforcing the cap on the stream (covers chunked uploads)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise PayloadTooLarge()
        except ValueError as exc:
            raise PayloadTooLarge() from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


def check_origin(request: Request, settings: Settings) -> None:
    """Exact Origin match plus custom header for state-changing auth requests."""
    origin = request.headers.get("origin")
    if not origin or origin == "null" or origin not in settings.allowed_origins:
        raise OriginRejected()
    if request.headers.get(CSRF_HEADER) != "1":
        raise OriginRejected()


def client_key(request: Request, settings: Settings) -> str:
    """Client address for rate limiting. Forwarding headers are trusted only from configured proxies."""
    peer = request.client.host if request.client else "unknown"
    if peer in settings.trusted_proxies:
        forwarded = request.headers.get("x-forwarded-for", "")
        candidate = forwarded.split(",")[-1].strip() if forwarded else ""
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            return peer
    return peer
