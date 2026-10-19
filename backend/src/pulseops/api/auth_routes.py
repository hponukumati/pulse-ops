"""REST authentication endpoints.

Auth state changes require POST + application/json + exact approved Origin + custom CSRF
header. Only these endpoints read the refresh cookie; GraphQL requires a bearer JWT.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pulseops.api.http import PRIVATE_HEADERS, check_origin, client_key, error_response, read_body_capped, require_json_content
from pulseops.container import Container
from pulseops.errors import AppError, ValidationFailed
from pulseops.policy import REAUTH_WINDOW
from pulseops.services.auth import AuthService, IssuedSession
from pulseops.services.ingest import parse_body

router = APIRouter(prefix="/api/v1/auth")
AUTH_BODY_LIMIT = 4096


class LoginIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class PasswordIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    currentPassword: str = Field(min_length=1, max_length=1024)
    newPassword: str = Field(min_length=1, max_length=1024)


class EmptyIn(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


def _c(request: Request) -> Container:
    return request.app.state.container  # type: ignore[no-any-return]


async def _guarded_body[M: BaseModel](request: Request, model: type[M]) -> M:
    c = _c(request)
    check_origin(request, c.settings)
    require_json_content(request)
    raw = await read_body_capped(request, AUTH_BODY_LIMIT)
    doc = parse_body(raw) if raw else {}
    try:
        return model.model_validate(doc)
    except ValidationError as exc:
        fields = sorted({str(e["loc"][0]) if e.get("loc") else "$" for e in exc.errors(include_input=False, include_url=False)})
        raise ValidationFailed([{"field": f, "code": "invalid"} for f in fields]) from exc


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _session_response(c: Container, issued: IssuedSession) -> JSONResponse:
    resp = JSONResponse(
        {
            "accessToken": issued.access_token,
            "accessTokenExpiresAt": _iso(issued.access_expires_at),
            "sessionExpiresAt": _iso(issued.session_expires_at),
            "user": {"id": str(issued.user_id), "role": issued.role},
        },
        headers=PRIVATE_HEADERS,
    )
    max_age = max(0, int((issued.session_expires_at - c.now()).total_seconds()))
    resp.set_cookie(
        c.settings.refresh_cookie_name,
        issued.refresh_secret,
        max_age=max_age,
        expires=issued.session_expires_at,  # never later than the session
        path="/",
        secure=c.settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return resp


def _clear_cookie(c: Container, resp: Response) -> None:
    resp.delete_cookie(c.settings.refresh_cookie_name, path="/", secure=c.settings.cookie_secure, httponly=True, samesite="strict")


@router.post("/login")
async def login(request: Request) -> Response:
    c = _c(request)
    body = await _guarded_body(request, LoginIn)
    issued = await AuthService(c).login(body.email, body.password, client_key(request, c.settings))
    return _session_response(c, issued)


@router.post("/refresh")
async def refresh(request: Request) -> Response:
    c = _c(request)
    await _guarded_body(request, EmptyIn)
    try:
        # Fail closed if Redis is unavailable (shared refresh limit).
        await c.limiter.window_check(f"rl:refresh:ip:{c.limiter.opaque(client_key(request, c.settings))}", 60, 60, increment=True)
        issued = await AuthService(c).refresh(request.cookies.get(c.settings.refresh_cookie_name))
    except AppError as exc:
        resp = error_response(exc)
        if exc.http_status == 401:
            _clear_cookie(c, resp)
        return resp
    return _session_response(c, issued)


@router.post("/logout")
async def logout(request: Request) -> Response:
    c = _c(request)
    await _guarded_body(request, EmptyIn)
    auth = AuthService(c)
    principal = None
    if request.headers.get("authorization"):
        try:
            principal = await auth.authenticate_bearer(request.headers.get("authorization"))
        except AppError:
            principal = None
    await auth.logout(request.cookies.get(c.settings.refresh_cookie_name), principal)
    resp = Response(status_code=204, headers=PRIVATE_HEADERS)
    _clear_cookie(c, resp)
    return resp


@router.post("/password")
async def change_password(request: Request) -> Response:
    c = _c(request)
    body = await _guarded_body(request, ChangePasswordIn)
    auth = AuthService(c)
    principal = await auth.authenticate_bearer(request.headers.get("authorization"))
    await auth.change_password(principal, body.currentPassword, body.newPassword, client_key(request, c.settings))
    return Response(status_code=204, headers=PRIVATE_HEADERS)


@router.post("/reauthenticate")
async def reauthenticate(request: Request) -> Response:
    c = _c(request)
    body = await _guarded_body(request, PasswordIn)
    auth = AuthService(c)
    principal = await auth.authenticate_bearer(request.headers.get("authorization"))
    at = await auth.reauthenticate(principal, body.password, client_key(request, c.settings))
    return JSONResponse({"reauthenticatedUntil": _iso(at + REAUTH_WINDOW)}, headers=PRIVATE_HEADERS)
