"""Alembic environment. Runs with the migration credential (schema owner), never the runtime role."""

import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from pulseops.config import get_settings
from pulseops.models import Base

target_metadata = Base.metadata


def _url() -> str:
    url = context.config.attributes.get("url") or context.config.get_main_option("sqlalchemy.url")
    return url or get_settings().migrator_db_url()


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def _main() -> None:
    engine = create_async_engine(_url(), hide_parameters=True)
    async with engine.connect() as conn:
        await conn.run_sync(_run)
        await conn.commit()
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported")
asyncio.run(_main())
