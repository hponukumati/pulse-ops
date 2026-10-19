"""Shared integration-test helpers (synthetic data only)."""

from __future__ import annotations

import importlib.util
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

from pulseops.container import Container
from pulseops.policy import Principal, Role
from pulseops.services.admin import AdminService

EMITTER_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "fastapi-emitter" / "demo_app.py"
ORIGIN = "http://127.0.0.1:8080"
AUTH_HEADERS = {"Origin": ORIGIN, "X-PulseOps-CSRF": "1", "Content-Type": "application/json"}
PASSWORD = "violet-anchor-prism-42"  # gitleaks:allow - synthetic password for accounts in ephemeral test containers
COOKIE = "pulseops_refresh_dev"


@dataclass
class Session:
    token: str
    refresh: str
    user_id: str


async def create_user(c: Container, email: str, role: Role, password: str = PASSWORD) -> uuid.UUID:
    user = await AdminService(c).create_user(Principal.cli(), email, password, role)
    return user.id


async def grant(c: Container, user_id: uuid.UUID, service_id: uuid.UUID) -> None:
    await AdminService(c).grant(Principal.cli(), user_id, service_id)


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> Session:
    r = await client.post("/api/v1/auth/login", headers=AUTH_HEADERS, content=json.dumps({"email": email, "password": password}))
    assert r.status_code == 200, r.text
    refresh = r.cookies.get(COOKIE)
    assert refresh
    client.cookies.clear()
    return Session(token=r.json()["accessToken"], refresh=refresh, user_id=r.json()["user"]["id"])


async def reauth(client: httpx.AsyncClient, s: Session, password: str = PASSWORD) -> None:
    r = await client.post(
        "/api/v1/auth/reauthenticate",
        headers={**AUTH_HEADERS, "Authorization": f"Bearer {s.token}"},
        content=json.dumps({"password": password}),
    )
    assert r.status_code == 200, r.text


async def gql(client: httpx.AsyncClient, s: Session | str | None, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    token = s.token if isinstance(s, Session) else s
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = await client.post("/graphql", headers=headers, content=json.dumps({"query": query, "variables": variables or {}}))
    body: dict[str, Any] = r.json()
    body["_status"] = r.status_code
    return body


def error_codes(body: dict[str, Any]) -> list[str]:
    return [e["extensions"]["code"] for e in body.get("errors", [])]


async def create_service(client: httpx.AsyncClient, s: Session, name: str, env: str = "synthetic-test") -> str:
    body = await gql(
        client, s, "mutation($i: ServiceInput!) { createService(input: $i) { id } }", {"i": {"name": name, "environment": env}}
    )
    assert "errors" not in body, body
    return str(body["data"]["createService"]["id"])


async def register_route(client: httpx.AsyncClient, s: Session, service_id: str, template: str) -> str:
    body = await gql(
        client, s, "mutation($s: ID!, $t: String!) { registerRoute(serviceId: $s, template: $t) { id } }", {"s": service_id, "t": template}
    )
    assert "errors" not in body, body
    return str(body["data"]["registerRoute"]["id"])


async def issue_token(client: httpx.AsyncClient, s: Session, service_id: str) -> tuple[str, str]:
    await reauth(client, s)
    body = await gql(client, s, "mutation($s: ID!) { createIngestToken(serviceId: $s) { token { id } secret } }", {"s": service_id})
    assert "errors" not in body, body
    return str(body["data"]["createIngestToken"]["token"]["id"]), str(body["data"]["createIngestToken"]["secret"])


def event(
    route_id: str,
    *,
    status: int = 200,
    duration: float = 10.0,
    at: datetime | None = None,
    event_id: str | None = None,
    method: str = "GET",
) -> dict[str, Any]:
    ts = (at or datetime.now(UTC) - timedelta(seconds=5)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {
        "eventId": event_id or str(uuid.uuid4()),
        "timestamp": ts,
        "method": method,
        "routeId": route_id,
        "statusCode": status,
        "durationMs": duration,
    }


async def ingest(client: httpx.AsyncClient, secret: str, events: list[dict[str, Any]] | bytes, **headers: str) -> httpx.Response:
    body = events if isinstance(events, bytes) else json.dumps(events).encode()
    h = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json", **headers}
    return await client.post("/api/v1/ingest/events", headers=h, content=body)


@dataclass
class World:
    """Alice (OPERATOR, service A), Bob (OPERATOR, service B), Victor (VIEWER, A), Admin."""

    admin: Session
    alice: Session
    bob: Session
    victor: Session
    service_a: str
    service_b: str
    route_a: str
    route_b: str
    ids: dict[str, uuid.UUID]


async def build_world(c: Container, client: httpx.AsyncClient) -> World:
    ids = {
        "admin": await create_user(c, "admin@pulseops.test", Role.ADMIN),
        "alice": await create_user(c, "alice@pulseops.test", Role.OPERATOR),
        "bob": await create_user(c, "bob@pulseops.test", Role.OPERATOR),
        "victor": await create_user(c, "victor@pulseops.test", Role.VIEWER),
    }
    alice = await login(client, "alice@pulseops.test")
    bob = await login(client, "bob@pulseops.test")
    sa = await create_service(client, alice, "service-a")
    sb = await create_service(client, bob, "service-b")
    await grant(c, ids["victor"], uuid.UUID(sa))
    ra = await register_route(client, alice, sa, "/orders/{orderId}")
    rb = await register_route(client, bob, sb, "/invoices/{invoiceId}")
    return World(
        admin=await login(client, "admin@pulseops.test"),
        alice=alice,
        bob=bob,
        victor=await login(client, "victor@pulseops.test"),
        service_a=sa,
        service_b=sb,
        route_a=ra,
        route_b=rb,
        ids=ids,
    )


def load_emitter_example(monkeypatch: pytest.MonkeyPatch, url: str, token: str, health_route: str, orders_route: str) -> ModuleType:
    """A fresh copy of examples/fastapi-emitter, configured through its environment variables."""
    env = {"PULSEOPS_URL": url, "PULSEOPS_INGEST_TOKEN": token, "HEALTH_ROUTE_ID": health_route, "ORDERS_ROUTE_ID": orders_route}
    for key, value in {**env, "FAIL_RATE": "0"}.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("pulseops_emitter_example", EMITTER_EXAMPLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
