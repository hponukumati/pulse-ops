"""Authentication: CSRF/Origin, generic errors, rate limits, refresh rotation/replay/race,
logout/reset revocation, absolute expiry, cookie attributes, token-type separation."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest

from pulseops.container import Container, utcnow
from pulseops.policy import Principal, Role
from pulseops.services.admin import AdminService
from tests.helpers import AUTH_HEADERS, COOKIE, ORIGIN, PASSWORD, create_user, gql, login

pytestmark = pytest.mark.integration

ME = "query { me { id email role } }"


async def _refresh(client: httpx.AsyncClient, cookie: str | None) -> httpx.Response:
    client.cookies.clear()
    headers = dict(AUTH_HEADERS)
    if cookie:
        headers["Cookie"] = f"{COOKIE}={cookie}"
    return await client.post("/api/v1/auth/refresh", headers=headers, content="{}")


async def test_login_success_sets_hardened_cookie(container: Container, client: httpx.AsyncClient) -> None:
    await create_user(container, "u1@pulseops.test", Role.OPERATOR)
    r = await client.post(
        "/api/v1/auth/login", headers=AUTH_HEADERS, content=json.dumps({"email": "U1@pulseops.test", "password": PASSWORD})
    )
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    set_cookie = r.headers["set-cookie"].lower()
    assert set_cookie.startswith(f"{COOKIE}=")
    for attr in ("httponly", "samesite=strict", "path=/", "max-age="):
        assert attr in set_cookie
    assert "domain=" not in set_cookie
    assert "accessToken" in r.json() and "refresh" not in json.dumps(r.json()).lower().replace("refreshtoken", "")
    me = await gql(client, r.json()["accessToken"], ME)
    assert me["data"]["me"]["email"] == "u1@pulseops.test"


async def test_generic_login_errors(container: Container, client: httpx.AsyncClient) -> None:
    uid = await create_user(container, "u2@pulseops.test", Role.VIEWER)
    bodies = []
    for email, pw in (("u2@pulseops.test", "wrong-password-123456"), ("nobody@pulseops.test", "wrong-password-123456")):
        r = await client.post("/api/v1/auth/login", headers=AUTH_HEADERS, content=json.dumps({"email": email, "password": pw}))
        assert r.status_code == 401
        bodies.append({k: v for k, v in r.json()["error"].items() if k != "requestId"})
    await AdminService(container).set_disabled(Principal.cli(), uid, True)
    r = await client.post(
        "/api/v1/auth/login", headers=AUTH_HEADERS, content=json.dumps({"email": "u2@pulseops.test", "password": PASSWORD})
    )
    bodies.append({k: v for k, v in r.json()["error"].items() if k != "requestId"})
    assert bodies[0] == bodies[1] == bodies[2] == {"code": "INVALID_CREDENTIAL", "message": "Invalid credential"}


@pytest.mark.parametrize(
    "headers",
    [
        {"X-PulseOps-CSRF": "1", "Content-Type": "application/json"},  # no Origin
        {"Origin": "null", "X-PulseOps-CSRF": "1", "Content-Type": "application/json"},
        {"Origin": "http://evil.example", "X-PulseOps-CSRF": "1", "Content-Type": "application/json"},
        {"Origin": ORIGIN + ".evil.example", "X-PulseOps-CSRF": "1", "Content-Type": "application/json"},
        {"Origin": ORIGIN, "Content-Type": "application/json"},  # no custom header
    ],
)
async def test_csrf_origin_enforced(container: Container, client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    await create_user(container, "u3@pulseops.test", Role.VIEWER)
    for path in ("/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/auth/logout", "/api/v1/auth/reauthenticate"):
        r = await client.post(path, headers=headers, content=json.dumps({"email": "u3@pulseops.test", "password": PASSWORD}))
        assert r.status_code == 403, (path, r.text)
        assert r.json()["error"]["code"] == "ORIGIN_REJECTED"


async def test_auth_requires_post_json(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/auth/login")).status_code == 405
    r = await client.post(
        "/api/v1/auth/login", headers={**AUTH_HEADERS, "Content-Type": "application/x-www-form-urlencoded"}, content="email=a&password=b"
    )
    assert r.status_code == 415
    r = await client.post(
        "/api/v1/auth/login", headers=AUTH_HEADERS, content=json.dumps({"email": "a@b.c", "password": "x", "admin": True})
    )
    assert r.status_code == 422


async def test_cors_only_exact_origin(client: httpx.AsyncClient) -> None:
    pre = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type,x-pulseops-csrf"}
    evil = await client.options("/api/v1/auth/login", headers={"Origin": "http://evil.example", **pre})
    assert "access-control-allow-origin" not in evil.headers
    ok = await client.options("/api/v1/auth/login", headers={"Origin": ORIGIN, **pre})
    assert ok.headers.get("access-control-allow-origin") == ORIGIN


async def test_login_rate_limit_per_account(container: Container, client: httpx.AsyncClient) -> None:
    await create_user(container, "u4@pulseops.test", Role.VIEWER)
    for _ in range(5):
        r = await client.post(
            "/api/v1/auth/login",
            headers=AUTH_HEADERS,
            content=json.dumps({"email": "u4@pulseops.test", "password": "wrong-password-123456"}),
        )
        assert r.status_code == 401
    r = await client.post(
        "/api/v1/auth/login", headers=AUTH_HEADERS, content=json.dumps({"email": "u4@pulseops.test", "password": PASSWORD})
    )
    assert r.status_code == 429
    assert 0 < int(r.headers["retry-after"]) <= 900
    # Temporary: once the window expires the account works again (no permanent lockout).
    await container.redis.flushall()
    assert (
        await client.post(
            "/api/v1/auth/login", headers=AUTH_HEADERS, content=json.dumps({"email": "u4@pulseops.test", "password": PASSWORD})
        )
    ).status_code == 200


async def test_login_rate_limit_per_ip(container: Container, client: httpx.AsyncClient) -> None:
    for i in range(30):
        await client.post(
            "/api/v1/auth/login",
            headers=AUTH_HEADERS,
            content=json.dumps({"email": f"x{i}@pulseops.test", "password": "wrong-password-123456"}),
        )
    r = await client.post(
        "/api/v1/auth/login", headers=AUTH_HEADERS, content=json.dumps({"email": "y@pulseops.test", "password": "wrong-password-123456"})
    )
    assert r.status_code == 429 and "retry-after" in r.headers


async def test_refresh_rotation_and_replay_revokes_family(container: Container, client: httpx.AsyncClient) -> None:
    await create_user(container, "u5@pulseops.test", Role.VIEWER)
    s = await login(client, "u5@pulseops.test")
    r1 = await _refresh(client, s.refresh)
    assert r1.status_code == 200
    new_cookie = r1.cookies.get(COOKIE)
    assert new_cookie and new_cookie != s.refresh
    # Replay of the spent token: whole family revoked, including the rotated token and access JWTs.
    replay = await _refresh(client, s.refresh)
    assert replay.status_code == 401
    assert (await _refresh(client, new_cookie)).status_code == 401
    assert "errors" in await gql(client, r1.json()["accessToken"], ME)


async def test_concurrent_refresh_race_requires_login(container: Container, client: httpx.AsyncClient) -> None:
    await create_user(container, "u6@pulseops.test", Role.VIEWER)
    s = await login(client, "u6@pulseops.test")

    async def attempt() -> int:
        async with httpx.AsyncClient(base_url=str(client.base_url)) as c2:
            r = await c2.post("/api/v1/auth/refresh", headers={**AUTH_HEADERS, "Cookie": f"{COOKIE}={s.refresh}"}, content="{}")
            return r.status_code

    results = await asyncio.gather(*(attempt() for _ in range(5)))
    assert results.count(200) <= 1
    assert results.count(401) >= 4
    # The race is treated as replay: no undocumented grace period, session is revoked.
    assert "errors" in await gql(client, s.token, ME)


async def test_logout_revokes_session_and_clears_cookie(container: Container, client: httpx.AsyncClient) -> None:
    await create_user(container, "u7@pulseops.test", Role.VIEWER)
    s = await login(client, "u7@pulseops.test")
    r = await client.post("/api/v1/auth/logout", headers={**AUTH_HEADERS, "Cookie": f"{COOKIE}={s.refresh}"}, content="{}")
    assert r.status_code == 204
    assert f"{COOKIE}=" in r.headers["set-cookie"] and "max-age=0" in r.headers["set-cookie"].lower()
    assert (await gql(client, s.token, ME))["_status"] == 401
    assert (await _refresh(client, s.refresh)).status_code == 401


async def test_password_change_and_reset_revoke_sessions(container: Container, client: httpx.AsyncClient) -> None:
    await create_user(container, "u8@pulseops.test", Role.VIEWER)
    a = await login(client, "u8@pulseops.test")
    b = await login(client, "u8@pulseops.test")
    bad = await client.post(
        "/api/v1/auth/password",
        headers={**AUTH_HEADERS, "Authorization": f"Bearer {a.token}"},
        content=json.dumps({"currentPassword": "not-the-password-x", "newPassword": "brand-new-passphrase-77"}),
    )
    assert bad.status_code == 401
    weak = await client.post(
        "/api/v1/auth/password",
        headers={**AUTH_HEADERS, "Authorization": f"Bearer {a.token}"},
        content=json.dumps({"currentPassword": PASSWORD, "newPassword": "short"}),
    )
    assert weak.status_code == 422
    ok = await client.post(
        "/api/v1/auth/password",
        headers={**AUTH_HEADERS, "Authorization": f"Bearer {a.token}"},
        content=json.dumps({"currentPassword": PASSWORD, "newPassword": "brand-new-passphrase-77"}),
    )
    assert ok.status_code == 204
    assert "data" in await gql(client, a.token, ME) and (await gql(client, a.token, ME)).get("errors") is None
    assert (await gql(client, b.token, ME))["_status"] == 401  # other sessions revoked
    # Host-operator reset (CLI path) revokes every session including the current one.
    await AdminService(container).reset_password(Principal.cli(), "u8@pulseops.test", "operator-reset-passphrase-9")
    assert (await gql(client, a.token, ME))["_status"] == 401
    await login(client, "u8@pulseops.test", "operator-reset-passphrase-9")


async def test_session_absolute_expiry_not_extended_by_refresh(container: Container, client: httpx.AsyncClient) -> None:
    await create_user(container, "u9@pulseops.test", Role.VIEWER)
    s = await login(client, "u9@pulseops.test")
    start = utcnow()
    container.clock = lambda: start + timedelta(hours=11, minutes=59)
    r = await _refresh(client, s.refresh)
    assert r.status_code == 200
    assert r.json()["sessionExpiresAt"] < (start + timedelta(hours=12, seconds=5)).isoformat()
    container.clock = lambda: start + timedelta(hours=12, seconds=1)
    assert (await _refresh(client, r.cookies.get(COOKIE))).status_code == 401


async def test_token_types_are_separated(container: Container, client: httpx.AsyncClient) -> None:
    await create_user(container, "u10@pulseops.test", Role.ADMIN)
    s = await login(client, "u10@pulseops.test")
    # GraphQL never accepts the refresh cookie alone.
    r = await client.post(
        "/graphql", headers={"Content-Type": "application/json", "Cookie": f"{COOKIE}={s.refresh}"}, content=json.dumps({"query": ME})
    )
    assert r.status_code == 401
    # A user JWT is not an ingest credential.
    r = await client.post(
        "/api/v1/ingest/events", headers={"Content-Type": "application/json", "Authorization": f"Bearer {s.token}"}, content="[]"
    )
    assert r.status_code == 401
    # Ingest-format credentials are not user tokens.
    assert (await gql(client, "poi_" + "a" * 20 + "_" + "b" * 43, ME))["_status"] == 401


async def test_role_and_disable_take_effect_immediately(container: Container, client: httpx.AsyncClient) -> None:
    uid = await create_user(container, "u11@pulseops.test", Role.OPERATOR)
    s = await login(client, "u11@pulseops.test")
    assert (await gql(client, s, ME))["data"]["me"]["role"] == "OPERATOR"
    await AdminService(container).set_role(Principal.cli(), uid, Role.VIEWER)
    assert (await gql(client, s, ME))["data"]["me"]["role"] == "VIEWER"
    await AdminService(container).set_disabled(Principal.cli(), uid, True)
    assert (await gql(client, s, ME))["_status"] == 401
