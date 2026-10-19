"""Statement timeouts, overall request timeout and SQL cancellation on request cancellation."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from pulseops.container import Container
from pulseops.db import classify_db_error
from pulseops.errors import Timeout
from pulseops.services.ingest import INGEST_STATEMENT_TIMEOUT_MS
from pulseops.services.metrics import METRICS_STATEMENT_TIMEOUT_MS

pytestmark = pytest.mark.integration


def test_configured_statement_timeouts() -> None:
    assert (METRICS_STATEMENT_TIMEOUT_MS, INGEST_STATEMENT_TIMEOUT_MS) == (2000, 3000)


async def test_statement_timeout_maps_to_safe_503(container: Container) -> None:
    with pytest.raises(DBAPIError) as ei:
        async with container.db.transaction(statement_timeout_ms=100) as s:
            await s.execute(text("SELECT pg_sleep(2)"))
    mapped = classify_db_error(ei.value)
    assert isinstance(mapped, Timeout) and mapped.http_status == 503


async def test_cancelled_request_cancels_server_side_query(container: Container) -> None:
    async def slow() -> None:
        async with container.db.session() as s:
            await s.execute(text("SELECT pg_sleep(10) /* cancel-probe */"))

    task = asyncio.create_task(slow())
    await asyncio.sleep(0.5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)
    async with container.db.session() as s:  # same role can see its own sessions' queries
        active = (
            await s.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE query LIKE '%cancel-probe%' AND state = 'active' AND pid <> pg_backend_pid()"
                )
            )
        ).scalar_one()
    assert active == 0
