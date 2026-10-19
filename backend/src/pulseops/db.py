"""Database engine/session management and DB error classification."""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import exc as sa_exc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from pulseops.errors import AppError, DependencyUnavailable, Timeout


class Database:
    """Owns one engine. Callers open one AsyncSession per independent unit of work."""

    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 10,
        max_overflow: int = 5,
        pool_timeout: float = 2.0,
        use_ssl: bool = False,
        application_name: str = "pulseops",
    ) -> None:
        connect_args: dict[str, Any] = {"server_settings": {"application_name": application_name}, "command_timeout": 10}
        if use_ssl:
            connect_args["ssl"] = ssl.create_default_context()  # verifies certificates and hostnames
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_pre_ping=True,
            hide_parameters=True,  # bind values never appear in exception text
            connect_args=connect_args,
        )
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as session:
            yield session

    @asynccontextmanager
    async def transaction(self, statement_timeout_ms: int | None = None) -> AsyncIterator[AsyncSession]:
        """Explicit transaction; commits on success, rolls back on any exception."""
        async with self.sessionmaker() as session, session.begin():
            if statement_timeout_ms is not None:
                await session.execute(text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"))
            yield session

    async def ping(self) -> bool:
        try:
            async with self.session() as s:
                await s.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()


_TIMEOUT_SQLSTATES = {"57014"}  # query_canceled (statement_timeout)
_RETRYABLE_SQLSTATES = {"40P01", "40001"}  # deadlock_detected, serialization_failure
_UNAVAILABLE_SQLSTATES_PREFIX = ("08", "53", "57P")  # connection, resources, admin shutdown


def classify_db_error(exc: BaseException) -> AppError | None:
    """Map infrastructure DB failures to safe 503 errors; return None for other errors."""
    if isinstance(exc, sa_exc.TimeoutError):  # pool exhausted
        return DependencyUnavailable()
    if isinstance(exc, (ConnectionError, OSError)):
        return DependencyUnavailable()
    if isinstance(exc, sa_exc.DBAPIError):
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None) or getattr(getattr(orig, "__cause__", None), "sqlstate", None)
        if sqlstate in _TIMEOUT_SQLSTATES:
            return Timeout()
        if sqlstate in _RETRYABLE_SQLSTATES:
            return DependencyUnavailable()  # transient: clients retry with the same event IDs
        if sqlstate and sqlstate.startswith(_UNAVAILABLE_SQLSTATES_PREFIX):
            return DependencyUnavailable()
        if isinstance(exc, (sa_exc.OperationalError, sa_exc.InterfaceError)) or exc.connection_invalidated:
            return DependencyUnavailable()
    return None
