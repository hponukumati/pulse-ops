"""POST /api/v1/ingest/events — service-scoped telemetry ingestion.

Order of checks: content type/encoding (415) → credential (401) → per-token request
rate (429) → streamed body cap (413) → strict JSON (400) → field validation (422) →
event-count budgets (429) → atomic insert (409/429/503) → 200 after COMMIT.
The Authorization header authenticates the emitter only; it is never stored or logged.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from pulseops.api.http import PRIVATE_HEADERS, read_body_capped, require_json_content
from pulseops.container import Container, current_request_id
from pulseops.services.ingest import MAX_BODY_BYTES, IngestService, parse_body, validate_batch

router = APIRouter(prefix="/api/v1/ingest")


@router.post("/events")
async def ingest_events(request: Request) -> JSONResponse:
    c: Container = request.app.state.container
    svc = IngestService(c)
    require_json_content(request)
    token = await svc.authenticate(request.headers.get("authorization"))
    await svc.check_request_rate(token)
    raw = await read_body_capped(request, MAX_BODY_BYTES)
    doc = parse_body(raw)
    events = validate_batch(doc, c.now())
    await svc.check_event_rate(token, len(events))
    result = await svc.ingest(token, events, submitted=len(events))
    return JSONResponse(
        {"accepted": result.accepted, "duplicate": result.duplicate, "requestId": str(current_request_id())},
        headers=PRIVATE_HEADERS,
    )
