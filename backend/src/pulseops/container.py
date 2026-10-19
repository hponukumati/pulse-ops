"""Process-wide dependencies and the per-request correlation ID."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime

from redis.asyncio import Redis

from pulseops.config import Settings
from pulseops.db import Database
from pulseops.ratelimit import LocalFallbackLimiter, RateLimiter, make_redis
from pulseops.security.passwords import PasswordHasher
from pulseops.security.tokens import JwtService

# Server-generated correlation ID; never derived from client input or telemetry.
request_id_var: ContextVar[uuid.UUID | None] = ContextVar("request_id", default=None)


def current_request_id() -> uuid.UUID | None:
    return request_id_var.get()


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Container:
    settings: Settings
    db: Database
    redis: Redis
    limiter: RateLimiter
    jwt: JwtService
    hasher: PasswordHasher
    local_limiter: LocalFallbackLimiter = field(default_factory=LocalFallbackLimiter)
    clock: Callable[[], datetime] = utcnow
    maint_db: Database | None = None

    def now(self) -> datetime:
        return self.clock()

    async def close(self) -> None:
        await self.db.dispose()
        if self.maint_db is not None:
            await self.maint_db.dispose()
        await self.redis.aclose()


def build_container(settings: Settings, *, application_name: str = "pulseops-api", with_maint: bool = False) -> Container:
    db = Database(
        settings.runtime_db_url(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        use_ssl=settings.db_ssl,
        application_name=application_name,
    )
    maint_db = (
        Database(
            settings.maint_db_url(), pool_size=1, max_overflow=0, use_ssl=settings.db_ssl, application_name=f"{application_name}-maint"
        )
        if with_maint
        else None
    )
    redis = make_redis(settings.redis_host, settings.redis_port, settings.redis_user, settings.redis_password, use_ssl=settings.redis_ssl)
    return Container(
        settings=settings,
        db=db,
        maint_db=maint_db,
        redis=redis,
        limiter=RateLimiter(redis, settings.rate_limit_key),
        jwt=JwtService(
            settings.jwt_private_key_pem,
            settings.jwt_kid,
            settings.jwt_issuer,
            settings.jwt_audience,
            settings.jwt_previous_public_key_pem,
            settings.jwt_previous_kid,
        ),
        hasher=PasswordHasher(
            settings.argon2_memory_kib, settings.argon2_time_cost, settings.argon2_parallelism, settings.hash_concurrency
        ),
    )
