"""Integration fixtures: reset PostgreSQL, Redis and the injectable clock before each test."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from pulseops.container import Container, utcnow
from tests.conftest import APP_TABLES


@pytest.fixture(autouse=True)
async def _clean_state(container: Container, migrator_engine: object) -> AsyncIterator[None]:
    async with migrator_engine.begin() as conn:  # type: ignore[attr-defined]
        await conn.execute(text(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY CASCADE"))
        await conn.execute(text("UPDATE ingest_quota SET used = 0 WHERE id = 1"))
        await conn.execute(text("UPDATE app_settings SET event_retention_days = 7 WHERE id = 1"))
    await container.redis.flushall()
    container.clock = utcnow
    yield
    container.clock = utcnow
