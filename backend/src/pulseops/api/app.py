"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from pulseops.api import auth_routes, graphql_http, ingest_routes, ws
from pulseops.api.http import PRIVATE_HEADERS, error_response
from pulseops.api.middleware import RequestContextMiddleware
from pulseops.config import Settings, get_settings, validate_for_startup
from pulseops.container import Container, build_container
from pulseops.db import classify_db_error
from pulseops.errors import AppError, BadRequest, DependencyUnavailable, NotFound
from pulseops.logging import configure_logging, log_event


def create_app(container: Container | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or (container.settings if container else get_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if container is None:
            configure_logging()
            validate_for_startup(settings)
            app.state.container = build_container(settings)
        else:
            app.state.container = container
        hub = ws.IncidentHub(app.state.container)
        app.state.hub = hub
        hub.start()
        log_event("api.started")
        try:
            yield
        finally:
            await hub.stop()
            if container is None:
                await app.state.container.close()
            log_event("api.stopped")

    # No public OpenAPI/docs/GraphiQL surfaces.
    app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["POST"],
        allow_headers=["Content-Type", "Authorization", "X-PulseOps-CSRF"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, __: RequestValidationError) -> JSONResponse:
        return error_response(BadRequest())

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return error_response(NotFound())
        body = {"error": {"code": "METHOD_NOT_ALLOWED" if exc.status_code == 405 else "BAD_REQUEST", "message": "Request not allowed"}}
        return JSONResponse(body, status_code=exc.status_code, headers=PRIVATE_HEADERS)

    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        mapped = classify_db_error(exc) or (DependencyUnavailable() if isinstance(exc, (RedisError, OSError)) else None)
        if mapped is not None:
            log_event("http.dependency_error", code=mapped.code, error_class=type(exc).__name__)
            return error_response(mapped)
        log_event("http.unexpected_error", error_class=type(exc).__name__)
        return error_response(AppError())

    # Infrastructure failures are handled by the exception middleware (clean 503, no re-raise);
    # the catch-all covers anything else with a generic 500 and no details.
    for exc_type in (OSError, SQLAlchemyError, RedisError, Exception):
        app.add_exception_handler(exc_type, _unexpected)  # type: ignore[arg-type]

    @app.get("/health/live")
    async def live() -> JSONResponse:
        return JSONResponse({"status": "ok"}, headers=PRIVATE_HEADERS)

    @app.get("/health/ready")
    async def ready(request: Request) -> JSONResponse:
        c: Container = request.app.state.container
        db_ok = await c.db.ping()
        try:
            redis_ok = bool(await c.redis.ping())
        except (RedisError, OSError):
            redis_ok = False
        ok = db_ok and redis_ok
        return JSONResponse({"status": "ready" if ok else "not_ready"}, status_code=200 if ok else 503, headers=PRIVATE_HEADERS)

    app.include_router(auth_routes.router)
    app.include_router(ingest_routes.router)
    app.include_router(graphql_http.router)
    app.include_router(ws.router)
    return app
