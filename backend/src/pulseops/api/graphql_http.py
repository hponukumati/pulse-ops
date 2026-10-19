"""POST /graphql: one JSON operation per request, bearer JWT required.

No GET, no batching, no uploads, no introspection. Limits: 64 KiB body, depth/alias/cost
limits, 120 operations/min/user, 5 concurrent/user, 30 concurrent globally, 2 MiB
response. If Redis is down, mutations fail closed (503) and reads fall back to a stricter
in-process limit of 60/min/user.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from graphql import OperationType
from strawberry.types.graphql import OperationType as StrawberryOperationType

from pulseops.api.http import PRIVATE_HEADERS, read_body_capped, require_json_content
from pulseops.container import Container, current_request_id
from pulseops.errors import AppError, BadRequest, DependencyUnavailable
from pulseops.gql.context import GqlContext
from pulseops.gql.schema import QueryRejected, format_error, prepare, schema
from pulseops.policy import Principal
from pulseops.ratelimit import Bucket
from pulseops.services.auth import AuthService
from pulseops.services.ingest import parse_body
from pulseops.services.metrics import operation_as_of

router = APIRouter()
GRAPHQL_BODY_LIMIT = 64 * 1024
RESPONSE_LIMIT = 2 * 1024 * 1024
USER_OPS_PER_MIN = 120
USER_CONCURRENCY = 5
GLOBAL_CONCURRENCY = 30
_ALLOWED_KEYS = {"query", "variables", "operationName", "extensions"}


def gql_error(code: str, message: str, status: int, retry_after: int | None = None) -> JSONResponse:
    headers = dict(PRIVATE_HEADERS)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse({"errors": [{"message": message, "extensions": {"code": code}}]}, status_code=status, headers=headers)


async def acquire_operation_slot(c: Container, principal: Principal, *, is_mutation: bool) -> str | None:
    """Rate + concurrency limits. Returns a lease id to release, or None in local-fallback mode."""
    uid = str(principal.user_id)
    try:
        await c.limiter.consume([Bucket(f"rl:api:user:{uid}", USER_OPS_PER_MIN / 60, USER_OPS_PER_MIN, 1)])
        return await c.limiter.acquire_lease([(f"cc:api:user:{uid}", USER_CONCURRENCY), ("cc:api:global", GLOBAL_CONCURRENCY)])
    except DependencyUnavailable:
        if is_mutation:
            raise  # mutations fail closed without shared limits
        c.local_limiter.consume(uid)
        return None


@router.post("/graphql")
async def graphql_http(request: Request) -> Response:
    c: Container = request.app.state.container
    try:
        require_json_content(request)
        raw = await read_body_capped(request, GRAPHQL_BODY_LIMIT)
        doc = parse_body(raw)
        if not isinstance(doc, dict) or set(doc) - _ALLOWED_KEYS:
            raise BadRequest()  # batch arrays and unknown members are rejected
        variables = doc.get("variables") or {}
        if not isinstance(variables, dict):
            raise BadRequest()
        principal = await AuthService(c).authenticate_bearer(request.headers.get("authorization"))
    except AppError as exc:
        return gql_error(exc.code, exc.message, exc.http_status, exc.retry_after)

    try:
        prepared = prepare(doc.get("query"), doc.get("operationName"), variables)
    except QueryRejected as exc:
        return gql_error(exc.code, "Query rejected", 400)
    if prepared.operation_type == OperationType.SUBSCRIPTION:
        return gql_error("BAD_REQUEST", "Subscriptions require the WebSocket transport", 400)

    try:
        lease = await acquire_operation_slot(c, principal, is_mutation=prepared.operation_type == OperationType.MUTATION)
    except AppError as exc:
        return gql_error(exc.code, exc.message, exc.http_status, exc.retry_after)
    try:
        ctx = GqlContext(c=c, principal=principal, as_of=operation_as_of(c.now()), request_id=current_request_id())
        result = await schema.execute(
            doc["query"],
            variable_values=variables,
            context_value=ctx,
            operation_name=doc.get("operationName"),
            allowed_operation_types=(StrawberryOperationType.QUERY, StrawberryOperationType.MUTATION),
        )
    finally:
        if lease:
            await c.limiter.release_lease([f"cc:api:user:{principal.user_id}", "cc:api:global"], lease)

    body: dict[str, Any] = {"data": result.data}
    if result.errors:
        body["errors"] = [format_error(e) for e in result.errors]
    payload = json.dumps(body, separators=(",", ":"), default=str).encode()
    if len(payload) > RESPONSE_LIMIT:
        return gql_error("RESPONSE_TOO_LARGE", "Response exceeds the size limit", 400)
    return Response(payload, media_type="application/json", headers=PRIVATE_HEADERS)
